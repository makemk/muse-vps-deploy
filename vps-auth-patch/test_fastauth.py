#!/usr/bin/env python3
"""Tests for fast_auth.py (FastAuthEngine v2).

Run: python3 -m pytest test_fastauth.py -v
"""
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fast_auth import (
    FastAuthEngine,
    authenticate_and_authorize_fast,
    engine_fail_closed,
    reset_engine,
    verify_controller_token_fast,
)

FAST_AUTH_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fast_auth.py")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_token():
    t = "muse_" + secrets.token_urlsafe(32)
    return t, hashlib.sha256(t.encode("utf-8")).hexdigest()


def write_config(path, entries):
    """entries: list of dicts {digest, muse, pcs, enabled=True, allowed_ips?}."""
    data = {}
    for e in entries:
        rec = {
            "muse": e["muse"],
            "pcs": e["pcs"],
            "created": "2026-10-19T00:00:00Z",
            "label": "test",
            "enabled": e.get("enabled", True),
        }
        if "allowed_ips" in e:
            rec["allowed_ips"] = e["allowed_ips"]
        if "grace_until" in e:
            rec["grace_until"] = e["grace_until"]
        data[e["digest"]] = rec
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, str(path))  # atomic, like production writers


@pytest.fixture
def cfg(tmp_path):
    """A controllers.json with: normal 1:1, super-admin, multi-PC, IP-restricted, disabled."""
    p = tmp_path / "controllers.json"
    t_normal, d_normal = make_token()
    t_super, d_super = make_token()
    t_multi, d_multi = make_token()
    t_iprest, d_iprest = make_token()
    t_disabled, d_disabled = make_token()
    write_config(p, [
        {"digest": d_normal, "muse": "muse-a", "pcs": ["PC-A"]},
        {"digest": d_super, "muse": "super", "pcs": ["*"]},
        {"digest": d_multi, "muse": "muse-m", "pcs": ["PC-A", "PC-B"]},
        {"digest": d_iprest, "muse": "muse-ip", "pcs": ["PC-A"],
         "allowed_ips": ["10.0.0.0/8", "192.168.1.5/32"]},
        {"digest": d_disabled, "muse": "muse-dead", "pcs": ["PC-A"], "enabled": False},
    ])
    toks = {
        "normal": t_normal, "super": t_super, "multi": t_multi,
        "iprest": t_iprest, "disabled": t_disabled,
    }
    return str(p), toks


@pytest.fixture
def engine(cfg):
    path, _ = cfg
    return FastAuthEngine(path)


def force_reload(eng):
    """Bypass the 200ms throttle so tests see file changes immediately."""
    eng._last_check_mono = 0.0


# ---------------------------------------------------------------------------
# slow path -> fast path
# ---------------------------------------------------------------------------

def test_slow_path_then_fast_path_hit(engine, cfg):
    _, toks = cfg
    # first call: slow path (cache empty)
    assert len(engine._decision_cache) == 0
    st, tgt = engine.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")
    assert (st, tgt) == (200, "PC-A")
    assert len(engine._decision_cache) == 1
    # second identical call: fast path, same result
    st2, tgt2 = engine.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")
    assert (st2, tgt2) == (200, "PC-A")
    assert len(engine._decision_cache) == 1  # no new entry


def test_fast_path_key_includes_ip_and_target(engine, cfg):
    """Same token, different (ip, target) triples are distinct cache entries."""
    _, toks = cfg
    engine.authenticate_and_authorize(toks["multi"], "1.1.1.1", "PC-A")
    engine.authenticate_and_authorize(toks["multi"], "1.1.1.1", "PC-B")
    engine.authenticate_and_authorize(toks["multi"], "2.2.2.2", "PC-A")
    assert len(engine._decision_cache) == 3


# ---------------------------------------------------------------------------
# 200 paths
# ---------------------------------------------------------------------------

def test_200_single_pc_auto_resolve(engine, cfg):
    _, toks = cfg
    st, tgt = engine.authenticate_and_authorize(toks["normal"], "9.9.9.9", None)
    assert (st, tgt) == (200, "PC-A")
    st, tgt = engine.authenticate_and_authorize(toks["normal"], "9.9.9.9", "")
    assert (st, tgt) == (200, "PC-A")


def test_200_super_with_target(engine, cfg):
    _, toks = cfg
    st, tgt = engine.authenticate_and_authorize(toks["super"], "9.9.9.9", "PC-ZZZ")
    assert (st, tgt) == (200, "PC-ZZZ")  # super may address any target


def test_200_ip_restricted_ok(engine, cfg):
    _, toks = cfg
    st, tgt = engine.authenticate_and_authorize(toks["iprest"], "10.5.6.7", "PC-A")
    assert (st, tgt) == (200, "PC-A")
    st, tgt = engine.authenticate_and_authorize(toks["iprest"], "192.168.1.5", "PC-A")
    assert (st, tgt) == (200, "PC-A")


def test_200_no_ip_restriction_any_ip(engine, cfg):
    _, toks = cfg
    st, _ = engine.authenticate_and_authorize(toks["normal"], "203.0.113.77", "PC-A")
    assert st == 200


# ---------------------------------------------------------------------------
# 401 paths (never cached)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", None, 123, b"muse_x", "wrong",
                                 "muse_", "muse_short", "Bearer muse_" + "x" * 40])
def test_401_bad_format(engine, bad):
    st, _ = engine.authenticate_and_authorize(bad, "1.2.3.4", "PC-A")
    assert st == 401


def test_401_unknown_token(engine):
    t, _ = make_token()  # well-formed but not in config
    st, _ = engine.authenticate_and_authorize(t, "1.2.3.4", "PC-A")
    assert st == 401
    assert len(engine._decision_cache) == 0  # 401 never cached


def test_401_disabled_token(engine, cfg):
    _, toks = cfg
    st, _ = engine.authenticate_and_authorize(toks["disabled"], "1.2.3.4", "PC-A")
    assert st == 401


def test_401_not_cached_repeated(engine):
    t, _ = make_token()
    for _ in range(3):
        st, _ = engine.authenticate_and_authorize(t, "1.2.3.4", "PC-A")
        assert st == 401
    assert len(engine._decision_cache) == 0


# ---------------------------------------------------------------------------
# 403 paths (never cached)
# ---------------------------------------------------------------------------

def test_403_ip_not_allowed(engine, cfg):
    _, toks = cfg
    st, msg = engine.authenticate_and_authorize(toks["iprest"], "8.8.8.8", "PC-A")
    assert st == 403
    assert "PC-A" not in msg  # no allow-list disclosure
    assert len(engine._decision_cache) == 0


def test_403_ip_garbage(engine, cfg):
    _, toks = cfg
    st, _ = engine.authenticate_and_authorize(toks["iprest"], "not-an-ip", "PC-A")
    assert st == 403


def test_403_super_without_target(engine, cfg):
    _, toks = cfg
    for bad_target in (None, ""):
        st, _ = engine.authenticate_and_authorize(toks["super"], "1.2.3.4", bad_target)
        assert st == 403


def test_403_wrong_target(engine, cfg):
    _, toks = cfg
    st, _ = engine.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-B")
    assert st == 403


def test_403_multi_pc_no_target(engine, cfg):
    _, toks = cfg
    st, _ = engine.authenticate_and_authorize(toks["multi"], "1.2.3.4", None)
    assert st == 403


def test_403_not_cached(engine, cfg):
    _, toks = cfg
    st, _ = engine.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-B")
    assert st == 403
    assert len(engine._decision_cache) == 0


# ---------------------------------------------------------------------------
# hot reload: mtime change clears decision cache, revocation takes effect
# ---------------------------------------------------------------------------

def test_mtime_change_clears_cache_and_revokes(cfg):
    path, toks = cfg
    eng = FastAuthEngine(path)
    st, _ = eng.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")
    assert st == 200
    assert len(eng._decision_cache) == 1

    # rewrite config WITHOUT the normal token (revoked), atomic replace
    t_super, d_super = toks["super"], None
    # rebuild: keep only super (need its digest -> re-derive is impossible;
    # instead write a config with just an unrelated entry)
    t_other, d_other = make_token()
    write_config(path, [{"digest": d_other, "muse": "other", "pcs": ["PC-A"]}])
    force_reload(eng)

    st, _ = eng.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")
    assert st == 401  # revoked token now rejected
    assert len(eng._decision_cache) == 0  # cache was cleared on reload


def test_throttle_no_reload_within_200ms(cfg):
    path, toks = cfg
    eng = FastAuthEngine(path)
    assert eng.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")[0] == 200
    # rewrite WITHOUT forcing reload bypass: throttle should suppress it
    t_other, d_other = make_token()
    write_config(path, [{"digest": d_other, "muse": "other", "pcs": ["PC-A"]}])
    # do NOT call force_reload: still inside the 200ms throttle window
    st, _ = eng.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")
    assert st == 200  # old config still served


# ---------------------------------------------------------------------------
# cache capacity: 1024 cap with clear-on-overflow
# ---------------------------------------------------------------------------

def test_decision_cache_cap_1024(tmp_path):
    p = tmp_path / "controllers.json"
    t, d = make_token()
    write_config(str(p), [{"digest": d, "muse": "super", "pcs": ["*"]}])
    eng = FastAuthEngine(str(p))
    # 1024 distinct triples, all 200
    for i in range(1024):
        ip = f"10.{(i >> 8) & 0xFF}.{i & 0xFF}.1"
        st, _ = eng.authenticate_and_authorize(t, ip, f"PC-{i}")
        assert st == 200
    assert len(eng._decision_cache) == 1024
    # 1025th distinct triple -> clear-on-overflow, then insert => size 1
    st, _ = eng.authenticate_and_authorize(t, "11.0.0.1", "PC-overflow")
    assert st == 200
    assert len(eng._decision_cache) == 1


# ---------------------------------------------------------------------------
# fail-closed (H1/H3)
# ---------------------------------------------------------------------------

def test_fail_closed_unsafe_permissions(tmp_path):
    p = tmp_path / "controllers.json"
    t, d = make_token()
    write_config(str(p), [{"digest": d, "muse": "m", "pcs": ["PC-A"]}])
    os.chmod(str(p), 0o644)  # unsafe
    eng = FastAuthEngine(str(p))
    st, _ = eng.authenticate_and_authorize(t, "1.2.3.4", "PC-A")
    assert st == 503
    assert eng.fail_closed() is True


def test_fail_closed_corrupt_no_cache(tmp_path):
    p = tmp_path / "controllers.json"
    p.write_text("{not valid json")
    os.chmod(str(p), 0o600)
    eng = FastAuthEngine(str(p))
    t, _ = make_token()
    st, _ = eng.authenticate_and_authorize(t, "1.2.3.4", "PC-A")
    assert st == 503


def test_corrupt_keeps_serving_old_cache(cfg):
    path, toks = cfg
    eng = FastAuthEngine(path)
    assert eng.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")[0] == 200
    # corrupt the file (atomic replace so mtime changes)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("{broken")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    force_reload(eng)
    st, _ = eng.authenticate_and_authorize(toks["normal"], "1.2.3.4", "PC-A")
    assert st == 200  # last good config still served
    assert eng.fail_closed() is False


def test_missing_file_not_fail_closed(tmp_path):
    eng = FastAuthEngine(str(tmp_path / "no-such-file.json"))
    t, _ = make_token()
    st, _ = eng.authenticate_and_authorize(t, "1.2.3.4", "PC-A")
    assert st == 401  # empty index, not 503
    assert eng.fail_closed() is False


# ---------------------------------------------------------------------------
# no plaintext tokens in memory (runtime introspection + source grep)
# ---------------------------------------------------------------------------

def test_no_plaintext_token_in_memory(engine, cfg):
    _, toks = cfg
    for name, tok in toks.items():
        if name == "disabled":
            continue
        engine.authenticate_and_authorize(tok, "10.9.9.9", "PC-A")
    engine.authenticate_and_authorize(toks["super"], "10.9.9.9", "PC-X")
    blob = repr(engine._decision_cache) + repr(engine._controllers_by_sha256)
    for name, tok in toks.items():
        assert tok not in blob, f"plaintext token '{name}' leaked into engine memory"
    # salts/digests present, raw tokens absent
    assert len(engine._decision_cache) > 0


def test_source_never_keys_cache_by_raw_token():
    src = open(FAST_AUTH_SRC, encoding="utf-8").read()
    # the decision cache must only ever be keyed by the blake2b fast_tag
    assert "_decision_cache[raw_token" not in src
    assert "_decision_cache[token]" not in src
    assert "cache_key = (fast_tag" in src
    # no dict keyed by plaintext token anywhere
    assert "[provided_token]" not in src or "_decision_cache" not in src.split("[provided_token]")[0][-200:]


# ---------------------------------------------------------------------------
# v1-compatible wrapper
# ---------------------------------------------------------------------------

def test_wrapper_valid_token(cfg):
    reset_engine()
    path, toks = cfg
    out = verify_controller_token_fast(toks["normal"], config_path=path)
    assert out is not None
    muse_id, pcs = out
    assert muse_id == "muse-a"
    assert pcs == ["PC-A"] and isinstance(pcs, list)
    reset_engine()


def test_wrapper_super(cfg):
    reset_engine()
    path, toks = cfg
    muse_id, pcs = verify_controller_token_fast(toks["super"], config_path=path)
    assert (muse_id, pcs) == ("super", ["*"])
    reset_engine()


def test_wrapper_invalid(cfg):
    reset_engine()
    path, toks = cfg
    assert verify_controller_token_fast("muse_" + "z" * 43, config_path=path) is None
    assert verify_controller_token_fast("", config_path=path) is None
    assert verify_controller_token_fast(None, config_path=path) is None
    assert verify_controller_token_fast(12345, config_path=path) is None
    assert verify_controller_token_fast(toks["disabled"], config_path=path) is None
    reset_engine()


def test_wrapper_ip_restricted_needs_client_ip(cfg):
    reset_engine()
    path, toks = cfg
    # no client_ip context -> deny (fail safe)
    assert verify_controller_token_fast(toks["iprest"], config_path=path) is None
    # correct IP -> ok
    out = verify_controller_token_fast(toks["iprest"], client_ip="10.5.6.7", config_path=path)
    assert out[0] == "muse-ip"
    # wrong IP -> deny
    assert verify_controller_token_fast(toks["iprest"], client_ip="8.8.8.8",
                                        config_path=path) is None
    reset_engine()


def test_wrapper_fail_closed_returns_none(tmp_path):
    reset_engine()
    p = tmp_path / "controllers.json"
    t, d = make_token()
    write_config(str(p), [{"digest": d, "muse": "m", "pcs": ["PC-A"]}])
    os.chmod(str(p), 0o644)
    assert verify_controller_token_fast(t, config_path=str(p)) is None
    assert engine_fail_closed(config_path=str(p)) is True
    reset_engine()


def test_module_singleton_entry(cfg):
    reset_engine()
    path, toks = cfg
    st, tgt = authenticate_and_authorize_fast(toks["normal"], "1.2.3.4", "PC-A",
                                              config_path=path)
    assert (st, tgt) == (200, "PC-A")
    reset_engine()


# ---------------------------------------------------------------------------
# malformed entries / CIDR handling
# ---------------------------------------------------------------------------

def test_malformed_entries_skipped(tmp_path):
    p = tmp_path / "controllers.json"
    t_ok, d_ok = make_token()
    data = {
        d_ok: {"muse": "ok", "pcs": ["PC-A"], "enabled": True},
        "not-hex": {"muse": "bad", "pcs": ["PC-A"], "enabled": True},
        "f" * 64: {"muse": "bad2", "pcs": ["PC-A"], "enabled": "false"},  # truthy str
        "e" * 64: {"muse": "bad3", "pcs": [], "enabled": True},           # empty pcs
        "d" * 64: {"muse": "bad4", "pcs": ["PC-A"], "enabled": True,
                   "allowed_ips": ["999.999.0.0/16"]},                    # bad CIDR
    }
    tmp = str(p) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, str(p))
    eng = FastAuthEngine(str(p))
    assert eng.authenticate_and_authorize(t_ok, "1.2.3.4", "PC-A") == (200, "PC-A")
    assert eng.stats()["controllers"] == 1


# ---------------------------------------------------------------------------
# grace_until: token-rotation grace-period semantics (P0-2)
# ---------------------------------------------------------------------------

def grace_iso(delta_seconds, z_suffix=False, naive=False):
    """ISO-8601 grace_until timestamp, delta_seconds from now."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    if naive:
        dt = dt.replace(tzinfo=None)
    s = dt.isoformat()
    if z_suffix:
        s = s.replace("+00:00", "Z")
    return s


def grace_config(tmp_path, old_grace):
    """controllers.json with a rotated-out (old, grace-bounded) token and a fresh token."""
    p = tmp_path / "controllers.json"
    t_old, d_old = make_token()
    t_new, d_new = make_token()
    write_config(str(p), [
        {"digest": d_old, "muse": "muse-old", "pcs": ["PC-A"],
         "grace_until": old_grace},
        {"digest": d_new, "muse": "muse-new", "pcs": ["PC-A"]},
    ])
    return str(p), t_old, t_new


def fast_tag(eng, tok, ip, target):
    """Recompute the composite decision-cache key for one triple."""
    tag = hashlib.blake2b(tok.encode("ascii", errors="ignore"),
                          key=eng._mem_key, digest_size=16).digest()
    return (tag, ip, target)


def test_grace_in_future_returns_200(tmp_path):
    path, t_old, _ = grace_config(tmp_path, grace_iso(3600))
    eng = FastAuthEngine(path)
    st, tgt = eng.authenticate_and_authorize(t_old, "1.2.3.4", "PC-A")
    assert (st, tgt) == (200, "PC-A")


def test_grace_iso_variants_accepted(tmp_path):
    """Trailing 'Z' and naive datetimes (assumed UTC) must parse."""
    for variant in (grace_iso(3600, z_suffix=True), grace_iso(3600, naive=True)):
        path, t_old, _ = grace_config(tmp_path, variant)
        eng = FastAuthEngine(path)
        st, _ = eng.authenticate_and_authorize(t_old, "1.2.3.4", "PC-A")
        assert st == 200, variant


def test_grace_expired_returns_401(tmp_path):
    path, t_old, _ = grace_config(tmp_path, grace_iso(-3600))
    eng = FastAuthEngine(path)
    st, _ = eng.authenticate_and_authorize(t_old, "1.2.3.4", "PC-A")
    assert st == 401
    assert len(eng._decision_cache) == 0


def test_grace_malformed_returns_401_fail_closed(tmp_path):
    """Malformed grace_until -> entry invalid, fail-closed (matches common.py)."""
    for bad in ("not-a-date", "", 1234567, "2026-13-99T99:99:99"):
        path, t_old, _ = grace_config(tmp_path, bad)
        eng = FastAuthEngine(path)
        st, _ = eng.authenticate_and_authorize(t_old, "1.2.3.4", "PC-A")
        assert st == 401, bad


def test_new_token_no_grace_is_cached(tmp_path):
    """Existing hot-path behavior preserved: non-grace 200s ARE cached."""
    path, _, t_new = grace_config(tmp_path, grace_iso(3600))
    eng = FastAuthEngine(path)
    st, tgt = eng.authenticate_and_authorize(t_new, "1.2.3.4", "PC-A")
    assert (st, tgt) == (200, "PC-A")
    assert fast_tag(eng, t_new, "1.2.3.4", "PC-A") in eng._decision_cache


def test_grace_200_not_cached(tmp_path):
    """Grace-bounded 200s are never cached: a cached 200 would outlive the
    deadline (expiry is wall-clock, no mtime change to evict it)."""
    path, t_old, _ = grace_config(tmp_path, grace_iso(3600))
    eng = FastAuthEngine(path)
    st, _ = eng.authenticate_and_authorize(t_old, "1.2.3.4", "PC-A")
    assert st == 200
    assert fast_tag(eng, t_old, "1.2.3.4", "PC-A") not in eng._decision_cache
    assert len(eng._decision_cache) == 0


def test_verify_wrapper_grace(tmp_path):
    reset_engine()
    path, t_old, _ = grace_config(tmp_path, grace_iso(3600))
    out = verify_controller_token_fast(t_old, config_path=path)
    assert out is not None and out[0] == "muse-old" and out[1] == ["PC-A"]
    reset_engine()
    # expired grace -> None even though enabled:true
    path, t_old, _ = grace_config(tmp_path, grace_iso(-3600))
    assert verify_controller_token_fast(t_old, config_path=path) is None
    reset_engine()
    # malformed grace -> None (fail-closed)
    path, t_old, _ = grace_config(tmp_path, "not-a-date")
    assert verify_controller_token_fast(t_old, config_path=path) is None
    reset_engine()


def test_grace_wall_clock_expiry_without_file_change(tmp_path):
    """The critical regression: the deadline passes with controllers.json
    untouched (mtime unchanged). The per-request check must flip 200->401;
    reload-time filtering alone cannot see this."""
    path, t_old, _ = grace_config(tmp_path, grace_iso(0.3))
    eng = FastAuthEngine(path)
    st, _ = eng.authenticate_and_authorize(t_old, "1.2.3.4", "PC-A")
    assert st == 200
    mtime_before = os.stat(path).st_mtime_ns
    time.sleep(0.5)  # cross the deadline; file untouched
    assert os.stat(path).st_mtime_ns == mtime_before  # mtime unchanged
    st, msg = eng.authenticate_and_authorize(t_old, "1.2.3.4", "PC-A")
    assert st == 401
    assert "grace" in msg


# ---------------------------------------------------------------------------
# performance: fast-path timing
# ---------------------------------------------------------------------------

def test_timing_fast_path(engine, cfg, capsys):
    _, toks = cfg
    tok, ip, tgt = toks["normal"], "1.2.3.4", "PC-A"
    # warm the cache
    for _ in range(2000):
        engine.authenticate_and_authorize(tok, ip, tgt)
    n = 200_000
    t0 = time.perf_counter_ns()
    for _ in range(n):
        engine.authenticate_and_authorize(tok, ip, tgt)
    dt_ns = time.perf_counter_ns() - t0
    mean_us = dt_ns / n / 1000.0
    with capsys.disabled():
        print(f"\n[perf] fast path: {mean_us:.3f} us/op mean over {n} ops "
              f"(Gemini-4 raw-dict estimate was <0.8us; full method incl. "
              f"blake2b tag + reload check)")
    # Bound is generous for CI stability across machines; the number above
    # is the honest measurement. Either way this is ~10x faster than the
    # ~50us uncached path and negligible vs the 27ms request budget.
    assert mean_us < 10.0


def test_timing_slow_path(tmp_path, capsys):
    p = tmp_path / "controllers.json"
    t, d = make_token()
    write_config(str(p), [{"digest": d, "muse": "super", "pcs": ["*"]}])
    eng = FastAuthEngine(str(p))
    n = 20_000
    t0 = time.perf_counter_ns()
    for i in range(n):
        # distinct triple each time -> slow path (all 200 via super token)
        ip = f"10.{(i >> 8) & 0xFF}.{i & 0xFF}.9"
        eng.authenticate_and_authorize(t, ip, f"PC-{i}")
    dt_ns = time.perf_counter_ns() - t0
    mean_us = dt_ns / n / 1000.0
    with capsys.disabled():
        print(f"\n[perf] slow path (full verify, 200): {mean_us:.3f} us/op mean over {n} ops")
    assert mean_us < 500.0
