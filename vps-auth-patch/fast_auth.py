#!/usr/bin/env python3
"""FastAuthEngine - high-performance controller-token auth for muse-bridge.py (v2).

Implements the design from the Gemini-4 second-round review (section 6.4):
composite decision cache that collapses the full auth pipeline
(token SHA-256 -> controller lookup -> allowed_ips CIDR check -> target
permission check) into a single dict lookup on the hot path.

Performance model:
  - Hot path: blake2b(token, 16B process salt) + tuple hash + dict.get()
    => < 1us measured (see test_fastauth.py::test_timing_fast_path)
  - Slow path (first sighting of a (token, ip, target) triple): full
    verification, then the decision is cached.

Security properties (inherited from the hardened v1, see SECURITY_REVIEW.md):
  - VPS memory NEVER holds plaintext tokens. The decision-cache key is
    blake2b(token, key=<16B per-process random salt>), irreversible and
    unusable for offline rainbow tables. Only SHA-256 hex digests are stored
    in the controller index (same as v1).
  - Only 200-OK decisions are cached. 401/403 results are NEVER cached,
    which makes random-token cache-pollution DoS impossible.
  - H1/H3 fail-closed: unreadable / unsafe-permission / corrupt-with-no-cache
    controllers.json denies ALL auth (503). Missing file (pre-migration)
    keeps legacy behavior (empty index, not fail-closed).
  - H2 strict entry validation is reused (see _valid_controller_entry below).
  - P0-2 token-rotation grace (grace_until) is honored on both layers with
    the same semantics as token-manager/common.py::is_entry_valid:
    malformed/expired grace entries are dropped at reload; expiry is
    wall-clock and does not change mtime, so it is re-checked per request
    in authenticate_and_authorize() and verify_controller_token_fast();
    grace-bounded 200s are deliberately NOT cached (a cached 200 would
    outlive the grace deadline with no reload to evict it).
  - Cache capacity 1024 entries; on overflow the whole cache is cleared
    (simplest correct eviction; legitimate (token,ip,target) triples are
    < 100 in practice).
  - controllers.json reload: throttled st_mtime_ns check (200ms), atomic
    dict swap (RCU), decision cache cleared on every config change so a
    revocation takes effect within ~200ms. Writers MUST use
    write-temp + os.replace() (see token-manager/common.py).

This file is SELF-CONTAINED: it does not import auth_patch at module load.
If auth_patch.py's functions were merged ahead of this file (i.e. the name
_valid_controller_entry already exists), the local copy is skipped.

Merge/deploy note: scripts/deploy-auth-patch.sh currently extracts only
auth_patch.py + dispatch_patch.py. To deploy v2, extend the merger to also
insert fast_auth.py's class + wrapper functions, OR replace the v1
verify_controller_token call sites in dispatch_patch.py with
authenticate_and_authorize_fast().
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Entry validation (H2). Reused from auth_patch.py when available.
# ---------------------------------------------------------------------------
try:
    _valid_controller_entry  # noqa: F821 - defined if auth_patch merged first
except NameError:
    def _valid_controller_entry(digest, entry):
        """Strict schema validation for one controllers.json entry.

        Returns True only if the entry is well-formed AND enabled.
        Local copy used when auth_patch.py was not merged ahead of this file.
        """
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return False
        if not isinstance(entry, dict):
            return False
        # enabled must be boolean True - a truthy string like "false" must NOT enable
        if entry.get("enabled") is not True:
            return False
        muse_id = entry.get("muse")
        if not isinstance(muse_id, str) or not muse_id.strip():
            return False
        pcs = entry.get("pcs")
        if not isinstance(pcs, list) or not pcs:
            return False
        if not all(isinstance(p, str) and p.strip() for p in pcs):
            return False
        return True


def _parse_grace_until(value):
    """Parse a grace_until ISO-8601 value into a POSIX timestamp (float).

    Mirrors the grace semantics of token-manager/common.py::is_entry_valid
    on the fast path: malformed values fail closed (return None and the
    caller skips the entry). Naive datetimes are assumed to be UTC; a
    trailing "Z" is normalized to "+00:00" for robustness across writers.

    Returns the deadline as a float timestamp, or None if malformed.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FastAuthEngine
# ---------------------------------------------------------------------------

# Capacity of the composite decision cache. Legitimate (token, ip, target)
# triples are < 100 in practice; 1024 with clear-on-overflow is the simplest
# correct eviction policy.
_DECISION_CACHE_CAP = 1024

# Hot-reload throttle: at most one os.stat() per 200ms regardless of QPS.
_RELOAD_THROTTLE_S = 0.2


class FastAuthEngine:
    """High-performance controller-token authentication engine.

    Usage:
        engine = FastAuthEngine("/etc/muse-bridge/controllers.json")
        status, payload = engine.authenticate_and_authorize(token, client_ip, target)
        # (200, "PC-A")  -> authorized, resolved target
        # (401, msg)     -> bad/unknown/revoked token
        # (403, msg)     -> IP not allowed / target not permitted / super w/o target
        # (503, msg)     -> auth subsystem misconfigured (fail-closed)
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self._last_check_mono = 0.0
        self._last_mtime_ns = 0
        # Per-process random salt: decision-cache keys cannot be reversed
        # offline and no plaintext token ever lives in VPS memory.
        self._mem_key = secrets.token_bytes(16)
        # Index: sha256_hex(lower) -> precompiled controller record
        #   {"muse": str, "pcs": set, "pcs_list": list, "default_pc": str|None,
        #    "networks": [IPv4Network|IPv6Network], "sha256_hex": str,
        #    "grace_until_ts": float|None}  # P0-2: rotation grace deadline
        #    as POSIX ts, None when the entry has no grace window.
        self._controllers_by_sha256 = {}
        # Composite decision cache: (fast_token_tag, client_ip, target) -> resolved_pc
        self._decision_cache = {}
        # H1/H3 fail-closed flag
        self._fail_closed = False

    # -- hot reload ------------------------------------------------------
    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._last_check_mono < _RELOAD_THROTTLE_S:
            return
        self._last_check_mono = now
        self._reload()

    def _reload(self) -> None:
        """Unthrottled config reload. Caller must have observed the throttle."""
        try:
            st = os.stat(self.config_path)
        except OSError:
            # File missing: pre-migration state. Empty index, NOT fail-closed
            # (legacy global token remains the auth method).
            self._fail_closed = False
            return
        # H3: refuse a file readable by group/other - fail closed.
        if st.st_mode & 0o077:
            self._fail_closed = True
            return
        if st.st_mtime_ns == self._last_mtime_ns:
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception:
            # H1: corrupt/unreadable. Keep serving from the last good index
            # if we have one; otherwise fail closed.
            if not self._controllers_by_sha256:
                self._fail_closed = True
            return
        if not isinstance(raw_data, dict):
            if not self._controllers_by_sha256:
                self._fail_closed = True
            return
        new_index = {}
        for sha256_hex, info in raw_data.items():
            if not _valid_controller_entry(sha256_hex, info):
                continue
            # Precompile allowed_ips CIDRs once at load, not per request.
            try:
                nets = [ipaddress.ip_network(c, strict=False)
                        for c in (info.get("allowed_ips") or [])]
            except ValueError:
                continue  # malformed CIDR -> skip entry loudly (fail safe)
            # P0-2: token-rotation grace period. A rotated-out entry keeps
            # enabled:true but gains grace_until; honor the same semantics as
            # token-manager/common.py::is_entry_valid:
            #   - malformed grace_until -> entry invalid, fail closed (skip)
            #   - deadline already past -> entry invalid even though enabled (skip)
            #   - future deadline -> keep, with the deadline precompiled.
            # Reload-time filtering alone is NOT sufficient: expiry is
            # wall-clock and does not change the file's mtime, so the
            # per-request re-check in authenticate_and_authorize() and
            # verify_controller_token_fast() is the other half of the fix.
            grace_until_ts = None
            grace_raw = info.get("grace_until")
            if grace_raw is not None:
                grace_until_ts = _parse_grace_until(grace_raw)
                if grace_until_ts is None:
                    continue  # malformed grace_until -> fail closed
                if time.time() >= grace_until_ts:
                    continue  # grace expired -> skip even though enabled:true
            key = sha256_hex.lower()
            pcs = info["pcs"]
            new_index[key] = {
                "muse": info["muse"],
                "pcs": set(pcs),
                "pcs_list": list(pcs),
                "default_pc": pcs[0] if len(pcs) == 1 and pcs[0] != "*" else None,
                "networks": nets,
                "sha256_hex": key,
                "grace_until_ts": grace_until_ts,
            }
        # RCU-style atomic swap of the index + clear decisions taken under
        # the old config (revocation takes effect within ~200ms).
        self._controllers_by_sha256 = new_index
        self._decision_cache.clear()
        self._last_mtime_ns = st.st_mtime_ns
        self._fail_closed = False

    def fail_closed(self) -> bool:
        """H1/H3: True when the auth subsystem is misconfigured."""
        self._maybe_reload()
        return self._fail_closed

    # -- main entry point ------------------------------------------------
    def authenticate_and_authorize(self, raw_token, client_ip, req_target):
        """Full auth pipeline with composite decision cache.

        Returns (http_status, payload):
          (200, resolved_target_pc)
          (401, error_message)   invalid / unknown / revoked token
          (403, error_message)   IP not allowed / target forbidden / super w/o target
          (503, error_message)   fail-closed (misconfigured controllers.json)
        """
        # ---- inline throttled reload check (hot path: no method call) ----
        _last = self._last_check_mono
        _now = time.monotonic()
        if _now - _last >= _RELOAD_THROTTLE_S:
            self._last_check_mono = _now
            self._reload()
        if self._fail_closed:
            return 503, "Auth subsystem misconfigured"
        # H4: non-string / empty input never raises, always 401.
        if not isinstance(raw_token, str):
            return 401, "Invalid token format"
        token = raw_token.strip()
        if not token or not token.startswith("muse_"):
            return 401, "Invalid token format"

        # Hot-path locals: avoid repeated attribute lookups.
        decision_cache = self._decision_cache
        mem_key = self._mem_key
        target_key = req_target if isinstance(req_target, str) else ""
        # Fast tag: irreversible 16B digest bound to this process's salt.
        # The raw token NEVER becomes a dict key or stored value.
        fast_tag = hashlib.blake2b(
            token.encode("ascii", errors="ignore"),
            key=mem_key, digest_size=16).digest()
        cache_key = (fast_tag, client_ip if isinstance(client_ip, str) else "", target_key)

        # ---- hot path: single dict lookup ----
        cached = decision_cache.get(cache_key)
        if cached is not None:
            return 200, cached

        # ---- slow path: full verification (first sighting of triple) ----
        token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
        record = self._controllers_by_sha256.get(token_sha256)
        if record is None or not hmac.compare_digest(token_sha256, record["sha256_hex"]):
            return 401, "Invalid or revoked token"

        # P0-2: rotation grace-period expiry is wall-clock; it does NOT
        # change the file's mtime, so reload-time filtering alone cannot
        # catch a deadline that passed since the last reload. Re-check here
        # on every slow-path request.
        grace_until_ts = record.get("grace_until_ts")
        if grace_until_ts is not None and time.time() >= grace_until_ts:
            return 401, "token expired (grace period over)"

        # allowed_ips check against precompiled networks (no per-request parse).
        if record["networks"]:
            try:
                ip_obj = ipaddress.ip_address(cache_key[1])
            except ValueError:
                return 403, "Invalid client IP"
            if not any(ip_obj in net for net in record["networks"]):
                return 403, "Source IP not allowed"

        # Target permission: super-admin (pcs=["*"]) MUST specify target.
        allowed_pcs = record["pcs"]
        if "*" in allowed_pcs:
            if not target_key:
                return 403, "Super-admin must specify target explicitly"
            resolved = target_key
        else:
            resolved = target_key if target_key else record["default_pc"]
            if not resolved or resolved not in allowed_pcs:
                return 403, "Target not authorized for this controller"

        # Cache ONLY the 200 decision (never 401/403: immune to cache pollution).
        # Exception to the "only 200s are cached" rule: a grace-bounded 200 is
        # NOT cached. A cached 200 would outlive the grace deadline, because
        # expiry is wall-clock and does not change controllers.json's mtime,
        # so no reload would ever evict it. The ~50us slow path on every
        # request during rotation windows is acceptable and obviously correct.
        if grace_until_ts is None:
            if len(decision_cache) >= _DECISION_CACHE_CAP:
                decision_cache.clear()
            decision_cache[cache_key] = resolved
        return 200, resolved

    # -- stats (for monitoring/tests) ------------------------------------
    def stats(self):
        return {
            "controllers": len(self._controllers_by_sha256),
            "decision_cache_size": len(self._decision_cache),
            "fail_closed": self._fail_closed,
            "config_path": self.config_path,
        }


# ---------------------------------------------------------------------------
# Module singleton + v1-compatible wrappers
# ---------------------------------------------------------------------------

_engine = None
_engine_path = None


def get_engine(config_path="/etc/muse-bridge/controllers.json"):
    """Return the process-wide FastAuthEngine singleton."""
    global _engine, _engine_path
    if _engine is None or _engine_path != config_path:
        _engine = FastAuthEngine(config_path)
        _engine_path = config_path
    return _engine


def reset_engine():
    """Drop the singleton (tests only)."""
    global _engine, _engine_path
    _engine = None
    _engine_path = None


def authenticate_and_authorize_fast(raw_token, client_ip, req_target,
                                    config_path="/etc/muse-bridge/controllers.json"):
    """v2 full-path entry: (status, resolved_or_msg). See FastAuthEngine."""
    return get_engine(config_path).authenticate_and_authorize(raw_token, client_ip, req_target)


def verify_controller_token_fast(provided_token, client_ip="",
                                 config_path="/etc/muse-bridge/controllers.json"):
    """Drop-in replacement for v1 verify_controller_token().

    Same contract: (muse_id, allowed_pcs_list) on success, None on failure.
    Difference from v1: when client_ip is provided (non-empty) and the entry
    configures allowed_ips, the IP is enforced. When client_ip is empty and
    the entry HAS an allowed_ips restriction, verification FAILS (fail safe:
    without source context we cannot confirm the IP) - callers that know the
    client IP must use authenticate_and_authorize_fast() instead.
    """
    engine = get_engine(config_path)
    # Mirror v1 input handling (H4): non-string/empty -> None, never raises.
    if not isinstance(provided_token, str):
        return None
    token = provided_token.strip()
    if not token or not token.startswith("muse_"):
        return None
    engine._maybe_reload()
    if engine._fail_closed:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    record = engine._controllers_by_sha256.get(digest)
    if record is None or not hmac.compare_digest(digest, record["sha256_hex"]):
        return None
    # P0-2: same grace-period hole as authenticate_and_authorize()'s slow
    # path - wall-clock expiry without an mtime change must be re-checked.
    grace_until_ts = record.get("grace_until_ts")
    if grace_until_ts is not None and time.time() >= grace_until_ts:
        return None
    # IP dimension: enforce only when we actually know the client IP.
    if record["networks"]:
        if not client_ip:
            return None  # cannot confirm source -> deny
        try:
            ip_obj = ipaddress.ip_address(client_ip)
        except ValueError:
            return None
        if not any(ip_obj in net for net in record["networks"]):
            return None
    return (record["muse"], list(record["pcs_list"]))


def engine_fail_closed(config_path="/etc/muse-bridge/controllers.json"):
    """v1-compatible fail-closed accessor for the v2 engine."""
    return get_engine(config_path).fail_closed()
