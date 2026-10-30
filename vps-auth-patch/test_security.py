#!/usr/bin/env python3
"""Security hardening tests for the VPS controller-token auth patch.

Covers the H1-H7 hardenings from SECURITY_REVIEW.md:
- timing-safe compare, no token in logs, 401/403/503 semantics
- fail-closed on unreadable/unsafe/corrupt controllers.json
- strict entry validation, non-string/oversize/special-char tokens
- concurrent reads, Bearer parsing, log sanitizer

Run: python3 -m pytest test_security.py -v
"""
import hashlib
import hmac
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time

import pytest

PATCH_DIR = os.path.dirname(os.path.abspath(__file__))


def load_patch():
    """Import auth_patch.py fresh (it has module-level mutable globals)."""
    spec = importlib.util.spec_from_file_location(
        "auth_patch_h", os.path.join(PATCH_DIR, "auth_patch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ap():
    return load_patch()


@pytest.fixture()
def controllers_file(ap, tmp_path):
    """Create a valid controllers.json and point the module at it."""
    token = "muse_" + "a" * 43  # token_urlsafe(32)-shaped
    digest = hashlib.sha256(token.encode()).hexdigest()
    data = {
        digest: {"muse": "super", "pcs": ["*"], "created": "2026-10-19",
                 "label": "test-super", "enabled": True},
    }
    p = tmp_path / "controllers.json"
    p.write_text(json.dumps(data))
    os.chmod(p, 0o600)
    ap.CONTROLLERS_PATH = str(p)
    ap._controllers_cache = None
    ap._controllers_mtime = 0
    ap._controllers_fail_closed = False
    return p, token, digest


# --------------------------------------------------------------------------
# Token verification: types, sizes, special chars
# --------------------------------------------------------------------------

def test_empty_token_rejected(ap):
    assert ap.verify_controller_token("") is None
    assert ap.verify_controller_token(None) is None
    assert ap.verify_controller_token("   ") is None


def test_nonstring_token_rejected_no_exception(ap):
    # H4: a JSON body like {"token": 123} must not raise AttributeError
    for bad in (123, 4.5, True, {}, [], ("muse_x",), object()):
        assert ap.verify_controller_token(bad) is None


def test_oversize_token_fast_reject(ap, controllers_file):
    big = "muse_" + "x" * 1_000_000
    t0 = time.time()
    assert ap.verify_controller_token(big) is None
    assert time.time() - t0 < 1.0  # sha256 of 1MB is ms-scale


def test_special_char_token_no_exception(ap, controllers_file):
    for bad in ("muse_\x00\x01", "muse_é中", "muse_\n\r\n", "muse_\t "):
        assert ap.verify_controller_token(bad) is None


def test_wrong_prefix_rejected(ap, controllers_file):
    assert ap.verify_controller_token("Bearer muse_" + "a" * 43) is None
    assert ap.verify_controller_token("MUSE_" + "a" * 43) is None  # case matters


def test_valid_token_accepted(ap, controllers_file):
    _, token, _ = controllers_file
    assert ap.verify_controller_token(token) == ("super", ["*"])


def test_disabled_token_rejected(ap, tmp_path):
    token = "muse_" + "b" * 43
    digest = hashlib.sha256(token.encode()).hexdigest()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({digest: {"muse": "x", "pcs": ["PC-A"],
                                      "enabled": False}}))
    os.chmod(p, 0o600)
    ap.CONTROLLERS_PATH = str(p)
    # H2: disabled entries are skipped at load -> not verifiable
    assert ap.verify_controller_token(token) is None


def test_timing_safe_compare_used(ap):
    import inspect
    src = inspect.getsource(ap.verify_controller_token)
    assert "hmac.compare_digest" in src


# --------------------------------------------------------------------------
# H1/H3: fail-closed semantics
# --------------------------------------------------------------------------

def test_missing_file_not_fail_closed(ap, tmp_path):
    # Pre-migration: no file -> legacy token path must keep working
    ap.CONTROLLERS_PATH = str(tmp_path / "does-not-exist.json")
    assert ap.load_controllers() == {}
    assert ap.controllers_fail_closed() is False


def test_unsafe_permissions_fail_closed(ap, tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{}")
    os.chmod(p, 0o644)  # group/other readable -> unsafe
    ap.CONTROLLERS_PATH = str(p)
    assert ap.load_controllers() == {}
    assert ap.controllers_fail_closed() is True


def test_corrupt_json_no_cache_fail_closed(ap, tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not valid json")
    os.chmod(p, 0o600)
    ap.CONTROLLERS_PATH = str(p)
    ap._controllers_cache = None
    assert ap.load_controllers() == {}
    assert ap.controllers_fail_closed() is True


def test_corrupt_json_with_cache_keeps_serving(ap, controllers_file):
    p, token, _ = controllers_file
    assert ap.verify_controller_token(token) == ("super", ["*"])  # prime cache
    p.write_text("{corrupt")  # same mtime? force reload via mtime bump
    os.utime(p, (time.time() + 5, time.time() + 5))
    result = ap.load_controllers()
    assert result  # stale cache kept (availability for transient errors)
    assert ap.controllers_fail_closed() is False
    assert ap.verify_controller_token(token) == ("super", ["*"])


def test_non_object_json_fail_closed(ap, tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1,2,3]")
    os.chmod(p, 0o600)
    ap.CONTROLLERS_PATH = str(p)
    assert ap.load_controllers() == {}
    assert ap.controllers_fail_closed() is True


def test_recovery_clears_fail_closed(ap, tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{bad")
    os.chmod(p, 0o600)
    ap.CONTROLLERS_PATH = str(p)
    assert ap.controllers_fail_closed() is True or ap.load_controllers() == {}
    assert ap.controllers_fail_closed() is True
    # operator fixes the file -> next load clears the flag
    p.write_text("{}")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert ap.load_controllers() == {}
    assert ap.controllers_fail_closed() is False


# --------------------------------------------------------------------------
# H2: strict entry validation
# --------------------------------------------------------------------------

def _write(ap, tmp_path, data):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data))
    os.chmod(p, 0o600)
    ap.CONTROLLERS_PATH = str(p)
    ap._controllers_cache = None
    ap._controllers_mtime = 0
    return p


def test_enabled_string_true_is_rejected(ap, tmp_path):
    # "enabled": "false" (string) is truthy in Python - must NOT enable
    token = "muse_" + "c" * 43
    d = hashlib.sha256(token.encode()).hexdigest()
    _write(ap, tmp_path, {d: {"muse": "x", "pcs": ["PC-A"], "enabled": "true"}})
    assert ap.verify_controller_token(token) is None
    assert ap.load_controllers() == {}


def test_pcs_string_rejected(ap, tmp_path):
    token = "muse_" + "d" * 43
    d = hashlib.sha256(token.encode()).hexdigest()
    _write(ap, tmp_path, {d: {"muse": "x", "pcs": "PC-A", "enabled": True}})
    assert ap.verify_controller_token(token) is None


def test_missing_muse_rejected(ap, tmp_path):
    token = "muse_" + "e" * 43
    d = hashlib.sha256(token.encode()).hexdigest()
    _write(ap, tmp_path, {d: {"pcs": ["PC-A"], "enabled": True}})
    assert ap.verify_controller_token(token) is None


def test_malformed_digest_key_rejected(ap, tmp_path):
    token = "muse_" + "f" * 43
    _write(ap, tmp_path, {"short": {"muse": "x", "pcs": ["PC-A"], "enabled": True}})
    assert ap.verify_controller_token(token) is None


def test_mixed_valid_and_invalid_entries(ap, tmp_path):
    good_token = "muse_" + "g" * 43
    good_d = hashlib.sha256(good_token.encode()).hexdigest()
    bad_d = hashlib.sha256(b"bad").hexdigest()
    _write(ap, tmp_path, {
        good_d: {"muse": "good", "pcs": ["PC-A"], "enabled": True},
        bad_d: {"muse": "bad", "pcs": "PC-A", "enabled": True},  # invalid pcs
    })
    loaded = ap.load_controllers()
    assert list(loaded.keys()) == [good_d]
    assert ap.verify_controller_token(good_token) == ("good", ["PC-A"])


# --------------------------------------------------------------------------
# H5: target permission checks
# --------------------------------------------------------------------------

def test_401_vs_403_semantics(ap, controllers_file):
    # 401 path: token invalid -> verify returns None
    assert ap.verify_controller_token("wrong") is None
    # 403 path: token valid but target not permitted
    allowed, err = ap.check_target_permission(["PC-A"], "PC-B")
    assert allowed is False and err
    # allowed target
    assert ap.check_target_permission(["PC-A"], "PC-A") == (True, None)
    assert ap.check_target_permission(["PC-A"], "pc-a") == (True, None)  # case-insensitive


def test_403_message_does_not_leak_allowed_list(ap):
    allowed, err = ap.check_target_permission(["SECRET-PC-1", "SECRET-PC-2"], "OTHER")
    assert allowed is False
    assert "SECRET-PC-1" not in err and "SECRET-PC-2" not in err


def test_superadmin_target_mandatory(ap):
    allowed, _ = ap.check_target_permission(["*"], None)
    assert allowed is False
    allowed, _ = ap.check_target_permission(["*"], "")
    assert allowed is False
    assert ap.check_target_permission(["*"], "ANY-PC") == (True, None)


def test_single_pc_autoresolve(ap):
    assert ap.check_target_permission(["PC-A"], None) == (True, None)
    assert ap.resolve_target(["PC-A"], None) == "PC-A"


def test_nonstring_target_rejected(ap):
    for bad in (123, ["PC-A"], {"t": 1}):
        allowed, err = ap.check_target_permission(["PC-A"], bad)
        assert allowed is False


# --------------------------------------------------------------------------
# H6/H7: log sanitizer + Bearer parsing
# --------------------------------------------------------------------------

def test_log_safe_strips_newlines_and_truncates(ap):
    assert ap._log_safe("a\nb\rc") == "a b c"
    assert ap._log_safe("x" * 200) == "x" * 120
    assert ap._log_safe("x" * 200, maxlen=10) == "x" * 10
    assert ap._log_safe(None) == "None"
    assert ap._log_safe(123) == "123"


def test_bearer_prefix_only(ap):
    assert ap.extract_bearer_token("Bearer muse_abc") == "muse_abc"
    assert ap.extract_bearer_token("bearer muse_abc") == "muse_abc"  # case-insensitive
    assert ap.extract_bearer_token("BEARER muse_abc") == "muse_abc"
    # old .replace() bug: substring elsewhere must NOT be stripped
    assert ap.extract_bearer_token("muse_Bearer_abc") == ""
    assert ap.extract_bearer_token("Token muse_abc") == ""
    assert ap.extract_bearer_token("") == ""
    assert ap.extract_bearer_token(None) == ""
    assert ap.extract_bearer_token("Bearer") == ""  # no trailing space+token
    assert ap.extract_bearer_token("Bearer   spaced  ") == "spaced"


# --------------------------------------------------------------------------
# No token in logs (grep-level guarantee)
# --------------------------------------------------------------------------

def test_token_never_in_stderr(ap, controllers_file, capsys):
    _, token, _ = controllers_file
    ap.verify_controller_token(token)
    ap.verify_controller_token("muse_wrong" + "z" * 40)
    ap.load_controllers()
    ap.check_target_permission(["*"], "PC-X")
    out, err = capsys.readouterr()
    assert token not in out and token not in err
    # also the wrong token attempt must not be echoed
    assert "muse_wrong" not in err


def test_patch_files_contain_no_token_logging():
    """Static check: no print/log line interpolates the raw token variable."""
    import re
    for fname in ("auth_patch.py", "dispatch_patch.py"):
        src = open(os.path.join(PATCH_DIR, fname), encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), 1):
            if "print(" in line or "log" in line.lower():
                # token variable must never appear inside a logged f-string
                if re.search(r'print\(.*\{.*\btoken\b.*\}', line):
                    # allow: lines that log only a boolean/derived value
                    assert False, f"{fname}:{i}: possible token logging: {line.strip()}"


# --------------------------------------------------------------------------
# Concurrency: load_controllers under threads
# --------------------------------------------------------------------------

def test_concurrent_reads_no_crash(ap, controllers_file):
    _, token, _ = controllers_file
    errors = []

    def worker():
        try:
            for _ in range(200):
                d = ap.load_controllers()
                assert isinstance(d, dict)
                ap.verify_controller_token(token)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# --------------------------------------------------------------------------
# Hot reload still works
# --------------------------------------------------------------------------

def test_hot_reload_on_change(ap, controllers_file):
    p, token, _ = controllers_file
    assert ap.verify_controller_token(token) == ("super", ["*"])
    token2 = "muse_" + "h" * 43
    d2 = hashlib.sha256(token2.encode()).hexdigest()
    p.write_text(json.dumps({d2: {"muse": "two", "pcs": ["PC-B"], "enabled": True}}))
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert ap.verify_controller_token(token) is None  # old gone
    assert ap.verify_controller_token(token2) == ("two", ["PC-B"])
