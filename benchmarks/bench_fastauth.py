#!/usr/bin/env python3
"""Benchmark: FastAuthEngine fast path vs slow path.

Gemini-4 targets (code_artifact__2.md section 6):
- Fast path (cache hit): <0.8 microseconds
- Slow path (full verify): <100 microseconds

Tries to import the real FastAuthEngine from vps-auth-patch/fast_auth.py.
Falls back to a spec-faithful reference implementation if not yet available.

Pure stdlib. No network. 100k iterations for stable percentiles.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import statistics
import sys
import tempfile
import time

# ---------------------------------------------------------------- setup
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vps-auth-patch"))

try:
    from fast_auth import FastAuthEngine
    REAL_IMPL = True
except ImportError:
    REAL_IMPL = False

    class FastAuthEngine:
        """Reference implementation per Gemini-4 spec (code_artifact__2.md 6.4)."""

        def __init__(self, config_path):
            self.config_path = config_path
            self._last_check_mono = 0.0
            self._last_mtime_ns = 0
            self._mem_key = secrets.token_bytes(16)
            # Pre-create base blake2b for fast copy+update (0.75us vs 1.15us)
            self._blake_base = hashlib.blake2b(key=self._mem_key,
                                               digest_size=16)
            self._controllers_by_sha256 = {}
            self._decision_cache = {}
            self._maybe_reload(force=True)

        def _fast_tag(self, token_bytes: bytes) -> bytes:
            h = self._blake_base.copy()
            h.update(token_bytes)
            return h.digest()

        def _maybe_reload(self, force=False):
            now = time.monotonic()
            if not force and now - self._last_check_mono < 0.2:
                return
            self._last_check_mono = now
            try:
                st = os.stat(self.config_path)
                if force or st.st_mtime_ns != self._last_mtime_ns:
                    with open(self.config_path) as f:
                        raw = json.load(f)
                    new_index = {}
                    for sha_hex, info in raw.items():
                        if not info.get("enabled", False):
                            continue
                        nets = [ipaddress.ip_network(c, strict=False)
                                for c in info.get("allowed_ips", [])]
                        pcs = info.get("pcs", [])
                        new_index[sha_hex.lower()] = {
                            "muse": info["muse"],
                            "pcs": set(pcs),
                            "default_pc": (pcs[0] if len(pcs) == 1
                                           and pcs[0] != "*" else None),
                            "networks": nets,
                            "sha256_hex": sha_hex.lower(),
                        }
                    self._controllers_by_sha256 = new_index
                    self._decision_cache.clear()
                    self._last_mtime_ns = st.st_mtime_ns
            except Exception:
                pass

        def authenticate_and_authorize(self, raw_token, client_ip, req_target):
            self._maybe_reload()
            if not raw_token or not raw_token.startswith("muse_"):
                return 401, "Invalid token format"
            target_key = req_target or ""
            # Fast tag via pre-created blake2b copy (0.75us)
            token_b = raw_token.encode("ascii", errors="ignore")
            fast_tag = self._fast_tag(token_b)
            cache_key = (fast_tag, client_ip, target_key)
            cached = self._decision_cache.get(cache_key)
            if cached is not None:
                return 200, cached
            # slow path
            token_sha256 = hashlib.sha256(raw_token.encode()).hexdigest()
            record = self._controllers_by_sha256.get(token_sha256)
            if record is None or not hmac.compare_digest(
                    token_sha256, record["sha256_hex"]):
                return 401, "Invalid or revoked token"
            if record["networks"]:
                try:
                    ip_obj = ipaddress.ip_address(client_ip)
                    if not any(ip_obj in net for net in record["networks"]):
                        return 403, "Source IP not allowed"
                except ValueError:
                    return 403, "Invalid client IP"
            allowed = record["pcs"]
            if "*" in allowed:
                if not target_key:
                    return 403, "Super controller must specify target"
                resolved = target_key
            else:
                resolved = target_key if target_key else record["default_pc"]
                if not resolved or resolved not in allowed:
                    return 403, "Target PC access denied"
            if len(self._decision_cache) >= 1024:
                self._decision_cache.clear()
            self._decision_cache[cache_key] = resolved
            return 200, resolved


# ---------------------------------------------------------------- fixtures
def make_controllers_db(path, n_controllers=10):
    """Write a controllers.json with n controllers, return {muse: raw_token}."""
    db = {}
    tokens = {}
    for i in range(n_controllers):
        raw = "muse_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        db[digest] = {
            "muse": f"controller-{i}",
            "pcs": [f"PC-{i}"],
            "created": "2026-10-19T00:00:00Z",
            "label": f"bench-{i}",
            "enabled": True,
            "allowed_ips": [],
        }
        tokens[f"controller-{i}"] = raw
    # super admin with IP restriction
    raw = "muse_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    db[digest] = {
        "muse": "super",
        "pcs": ["*"],
        "created": "2026-10-19T00:00:00Z",
        "label": "super-admin",
        "enabled": True,
        "allowed_ips": ["10.0.0.0/8"],
    }
    tokens["super"] = raw
    with open(path, "w") as f:
        json.dump(db, f)
    os.chmod(path, 0o600)
    return tokens


def percentile(data, p):
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# ---------------------------------------------------------------- benchmark
def bench_fast_path(engine, token, ip, target, iters=100_000):
    """Fast path: same (token, ip, target) -> decision cache hit."""
    # warm the cache
    st, _ = engine.authenticate_and_authorize(token, ip, target)
    assert st == 200, f"warmup failed: {st}"
    # prevent reload during bench (mtime unchanged anyway)
    times = []
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        st, res = engine.authenticate_and_authorize(token, ip, target)
        t1 = time.perf_counter_ns()
        assert st == 200
        times.append((t1 - t0) / 1000.0)  # microseconds
    return times


def bench_slow_path(engine, tokens_db, ip, iters=10_000):
    """Slow path: unique (token, ip) combos -> full SHA-256 + compare each time.

    Uses distinct IPs per iteration to defeat the decision cache,
    forcing the full verification path.
    """
    names = list(tokens_db.keys())
    times = []
    for i in range(iters):
        name = names[i % len(names)]
        token = tokens_db[name]
        # unique IP per iteration defeats cache -> slow path
        fake_ip = f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}"
        target = f"PC-{i % 10}" if name != "super" else "PC-X"
        t0 = time.perf_counter_ns()
        st, _ = engine.authenticate_and_authorize(token, fake_ip, target)
        t1 = time.perf_counter_ns()
        # super token with 10.x IP should pass (allowed_ips 10.0.0.0/8)
        times.append((t1 - t0) / 1000.0)
    return times


def main():
    print("=" * 60)
    print("FastAuthEngine benchmark")
    print(f"Implementation: {'REAL (vps-auth-patch/fast_auth.py)' if REAL_IMPL else 'REFERENCE (spec-faithful mock)'}")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "controllers.json")
        tokens = make_controllers_db(db_path)
        engine = FastAuthEngine(db_path)

        # --- fast path ---
        token = tokens["controller-0"]
        fast = bench_fast_path(engine, token, "192.168.1.1", "PC-0")
        fp50 = percentile(fast, 50)
        fp95 = percentile(fast, 95)
        fp99 = percentile(fast, 99)
        print(f"\nFast path (cache hit, n={len(fast)}):")
        print(f"  p50: {fp50:.3f} us   target: <0.8 us   {'PASS' if fp50 < 0.8 else 'FAIL'}")
        print(f"  p95: {fp95:.3f} us")
        print(f"  p99: {fp99:.3f} us")
        print(f"  min: {min(fast):.3f} us   max: {max(fast):.3f} us")

        # --- slow path ---
        slow = bench_slow_path(engine, tokens, "10.0.0.1")
        sp50 = percentile(slow, 50)
        sp95 = percentile(slow, 95)
        sp99 = percentile(slow, 99)
        print(f"\nSlow path (full verify, n={len(slow)}):")
        print(f"  p50: {sp50:.3f} us   target: <100 us   {'PASS' if sp50 < 100 else 'FAIL'}")
        print(f"  p95: {sp95:.3f} us")
        print(f"  p99: {sp99:.3f} us")
        print(f"  min: {min(slow):.3f} us   max: {max(slow):.3f} us")

        # --- negative cases (should be fast rejects, no cache pollution) ---
        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            st, _ = engine.authenticate_and_authorize(
                "muse_" + "x" * 43, "1.2.3.4", "PC-0")
            assert st == 401
        t1 = time.perf_counter_ns()
        avg_reject = (t1 - t0) / 10_000 / 1000.0
        print(f"\nReject path (bad token, 401, n=10000):")
        print(f"  avg: {avg_reject:.3f} us   (not cached by design)")

        # --- summary ---
        print("\n" + "=" * 60)
        print("SUMMARY vs Gemini-4 targets:")
        print(f"  Fast path p50 {fp50:.3f} us  < 0.8 us : {'PASS' if fp50 < 0.8 else 'FAIL'}")
        print(f"  Slow path p50 {sp50:.3f} us < 100 us  : {'PASS' if sp50 < 100 else 'FAIL'}")
        ok = fp50 < 0.8 and sp50 < 100
        print(f"  OVERALL: {'PASS' if ok else 'FAIL'}")
        print("=" * 60)
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
