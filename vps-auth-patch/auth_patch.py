#!/usr/bin/env python3
"""Controller token authentication patch for muse-bridge.py.

This is a PATCH FILE - do not run directly. It contains the new functions
to be merged into /usr/local/bin/muse-bridge.py.

Design (from 12-agent research):
- Token format: `muse_` + secrets.token_urlsafe(32)
- Storage: /etc/muse-bridge/controllers.json (600), SHA-256 hex keys, no plaintext
- Schema: {"<sha256hex>": {"muse": "...", "pcs": [...], "created": "...", "label": "...", "enabled": bool}}
- Super-admin: single token with pcs=["*"], target mandatory (no default)
- PC clients (WSS) continue using the existing global CONFIGURED_TOKEN (client token)
- Controllers (HTTP API) use per-controller tokens from controllers.json

SECURITY HARDENING (2026-10-19 review, see SECURITY_REVIEW.md):
- H1: fail-closed flag. controllers.json exists but unreadable / unsafe perms /
      corrupt with no valid cache  -> deny ALL auth (controller + legacy), 503.
      Missing file (pre-migration) -> legacy still allowed, NOT fail-closed.
- H2: strict entry validation. enabled must be boolean True (not truthy);
      digest keys must be 64-hex; pcs must be non-empty list of non-empty str;
      muse must be non-empty str. Malformed entries are skipped with a warning.
- H3: unsafe file permissions (any group/other bit) -> fail-closed.
- H4: verify_controller_token rejects non-string input (no AttributeError -> 500).
- H5: check_target_permission rejects non-string target; 403 messages are
      generic (no allowed-PC list disclosure).
- H6: _log_safe() sanitizes values for single-line stderr audit logging
      (strips CR/LF to prevent log injection, truncates).
- H7: extract_bearer_token() parses Authorization header prefix-only and
      case-insensitively (old .replace("Bearer ", "") mangled tokens
      containing that substring).

Merge instructions are at the bottom of this file.
"""

# Self-contained imports: harmless if merged into a file that already has them
# (re-import is a no-op), and required for standalone testing.
import hashlib
import hmac
import json
import os
import re
import sys


# ============================================================================
# NEW GLOBALS (add near line 49, after CONFIGURED_TOKEN definition)
# ============================================================================

CONTROLLERS_PATH = "/etc/muse-bridge/controllers.json"
_controllers_cache = None      # parsed JSON dict (validated entries only)
_controllers_mtime = 0          # for hot-reload without restart
_controllers_fail_closed = False  # H1: True -> deny ALL auth (controller + legacy)


# ============================================================================
# NEW FUNCTIONS (add after check_auth, ~line 88)
# ============================================================================

def _log_safe(value, maxlen=120):
    """H6: Make a value safe for single-line stderr audit logging.

    Strips CR/LF (log injection) and truncates. Never raises.
    """
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        s = "<unprintable>"
    return s.replace("\r", " ").replace("\n", " ")[:maxlen]


def _valid_controller_entry(digest, entry):
    """H2: Strict schema validation for one controllers.json entry.

    Returns True only if the entry is well-formed AND enabled.
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


def load_controllers():
    """Load controllers.json with mtime-based hot reload. Returns dict (never None).

    H1/H3 fail-closed semantics:
    - File missing            -> {} (pre-migration; legacy token still allowed)
    - Unsafe permissions      -> {} + fail-closed (deny everything)
    - Unreadable / corrupt    -> keep last good cache if any (transient);
                                 otherwise {} + fail-closed
    - Valid load              -> validated entries, fail-closed cleared
    """
    global _controllers_cache, _controllers_mtime, _controllers_fail_closed
    try:
        st = os.stat(CONTROLLERS_PATH)
    except OSError:
        # File doesn't exist yet (pre-migration) - return empty, don't crash.
        # This is NOT a failure: legacy global token remains the auth method.
        _controllers_fail_closed = False
        return {}
    # H3: refuse to trust a file readable by group/other. The digests inside
    # are not directly brute-forceable, but a world-readable auth config is
    # never acceptable - fail closed so the operator must fix permissions.
    if st.st_mode & 0o077:
        print(f"[bridge] CRITICAL: {CONTROLLERS_PATH} has unsafe permissions "
              f"({oct(st.st_mode & 0o777)}), refusing to load - FAIL CLOSED",
              file=sys.stderr)
        _controllers_fail_closed = True
        return {}
    mtime = st.st_mtime
    if _controllers_cache is not None and mtime == _controllers_mtime:
        return _controllers_cache
    try:
        with open(CONTROLLERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # H1: corrupt/unreadable. A transient error (e.g. mid-write) must not
        # take down auth if we have a last good cache; but if we never loaded
        # successfully, fail closed instead of silently running with no data.
        print(f"[bridge] Warning reading {CONTROLLERS_PATH}: {_log_safe(e)}",
              file=sys.stderr)
        if _controllers_cache is not None:
            return _controllers_cache
        _controllers_fail_closed = True
        return {}
    if not isinstance(data, dict):
        print(f"[bridge] Warning: {CONTROLLERS_PATH} is not a JSON object, ignoring",
              file=sys.stderr)
        if _controllers_cache is not None:
            return _controllers_cache
        _controllers_fail_closed = True
        return {}
    # H2: validate every entry; skip malformed ones loudly
    clean = {}
    for digest, entry in data.items():
        if _valid_controller_entry(digest, entry):
            clean[digest] = entry
        else:
            print(f"[bridge] Warning: skipping malformed controller entry "
                  f"(digest {_log_safe(digest, 16)}...)", file=sys.stderr)
    _controllers_cache = clean
    _controllers_mtime = mtime
    _controllers_fail_closed = False
    return clean


def controllers_fail_closed():
    """H1: True when the auth subsystem is misconfigured and ALL auth must be denied."""
    return _controllers_fail_closed


def extract_bearer_token(authorization_header):
    """H7: Extract token from an Authorization header.

    Prefix-only ("Bearer " at position 0), case-insensitive scheme.
    The old code used .replace("Bearer ", "") which corrupted any token
    containing that substring anywhere (e.g. "muse_Bearer_x" -> "muse_x").
    """
    if not isinstance(authorization_header, str):
        return ""
    if authorization_header[:7].lower() == "bearer ":
        return authorization_header[7:].strip()
    return ""


def verify_controller_token(provided_token):
    """Verify a controller token against controllers.json.

    Returns (muse_id, allowed_pcs) on success, None on failure.
    Uses hmac.compare_digest for timing-safe comparison of the SHA-256 hex
    digests (fixed 64-char length, constant-time per comparison).

    allowed_pcs: list of PC hostnames this controller may target.
                 ["*"] means super-admin (all PCs).

    H4: non-string input is rejected (never raises).
    """
    if not isinstance(provided_token, str):  # H4
        return None
    if not provided_token:
        return None
    token = provided_token.strip()
    if not token:
        return None
    # Only controller tokens have the muse_ prefix - fast path reject
    if not token.startswith("muse_"):
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    controllers = load_controllers()
    for stored_digest, entry in controllers.items():
        # entries are pre-validated by load_controllers (H2), so enabled is True
        if hmac.compare_digest(stored_digest, digest):
            muse_id = entry.get("muse", "?")
            allowed_pcs = entry.get("pcs", [])
            if not isinstance(allowed_pcs, list):
                return None
            return (muse_id, allowed_pcs)
    return None


def check_target_permission(allowed_pcs, target):
    """Check if a controller may target the given PC hostname.

    Returns (allowed: bool, error_msg: str or None).

    Rules:
    - Super-admin (pcs=["*"]): target is MANDATORY (no silent default).
      Prevents accidental commands to the wrong machine.
    - Normal controller: if len(pcs)==1 and no target given, auto-resolve
      to their single PC (convenience). Otherwise target must be in pcs.

    H5: non-string target is rejected; error messages are generic and never
    disclose the controller's allowed-PC list.
    """
    if target is not None and not isinstance(target, str):  # H5
        return (False, "Invalid target parameter")
    if "*" in allowed_pcs:
        # Super-admin: target required, no default
        if not target:
            return (False, "Super-admin must specify target explicitly (no default)")
        return (True, None)
    # Normal 1:1 (or 1:N) controller
    if not target:
        if len(allowed_pcs) == 1:
            return (True, None)  # auto-resolve to the single bound PC
        return (False, "Multiple PCs bound to this controller, specify target explicitly")
    # Case-insensitive match (consistent with pick_client's fallback)
    target_lower = target.lower()
    for pc in allowed_pcs:
        if isinstance(pc, str) and pc.lower() == target_lower:
            return (True, None)
    return (False, "Target not authorized for this controller")


def resolve_target(allowed_pcs, target):
    """Resolve the effective target hostname after permission check.

    Returns the target to pass to pick_client/dispatch_request_to_pc.
    For single-PC controllers with no target given, returns their PC.
    """
    if not target and "*" not in allowed_pcs and len(allowed_pcs) == 1:
        return allowed_pcs[0]
    return target


# ============================================================================
# MERGE INSTRUCTIONS
# ============================================================================
"""
1. Add the CONTROLLERS_PATH/_controllers_cache/_controllers_mtime/
   _controllers_fail_closed globals after line 49 (CONFIGURED_TOKEN = ...).

2. Add _log_safe(), _valid_controller_entry(), load_controllers(),
   controllers_fail_closed(), extract_bearer_token(), verify_controller_token(),
   check_target_permission(), resolve_target() after check_auth() (after line 88).

3. Required imports: auth_patch.py is now self-contained (hashlib, hmac,
   json, os, re, sys at its top). When merged into muse-bridge.py the
   duplicate imports are harmless no-ops.

4. See dispatch_patch.py for the handler modifications.

5. File permissions: /etc/muse-bridge/controllers.json must be 600 root:root,
   /etc/muse-bridge/ should be 700 root:root. The bridge now FAILS CLOSED
   (denies ALL auth, 503) if the file has unsafe permissions or is corrupt
   with no valid cache - see SECURITY_REVIEW.md H1/H3.
"""
