# OPT-03 daemon-side unit tests: mused DedupCache + _read_timeout_for,
# musepcd build_mused_request idem_key handling.
import importlib.util
import sys
import threading
import time

def load(path, name):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    with open(path, "rb") as f:
        code = compile(f.read(), path, "exec")
    mod.__dict__["__name__"] = name
    exec(code, mod.__dict__)
    return mod

# mused imports microbatch/tls_resume/pool/compress from ~/.local/lib/mused
sys.path.insert(0, "/home/hatch/.local/lib/mused")
mused = load("/home/hatch/.local/bin/mused", "mused_under_test")
musepcd = load("/home/hatch/.local/bin/musepcd", "musepcd_under_test")

passed = 0
def ok(name):
    global passed
    passed += 1
    print(f"  PASS {name}")

# ---- _read_timeout_for ----
assert mused._read_timeout_for(None) == 10
assert mused._read_timeout_for({}, "/status") == 10
assert mused._read_timeout_for({}, "/cmd") == 65
assert mused._read_timeout_for({}, "/file") == 65
assert mused._read_timeout_for({}, "/batch") == 65
assert mused._read_timeout_for({"timeout": 30}, "/cmd") == 35
assert mused._read_timeout_for({"timeout": 99999}, "/cmd") == 605
assert mused._read_timeout_for({"timeout": "abc"}, "/cmd") == 65
ok("_read_timeout_for defaults + explicit")

# ---- _idem_key_for ----
assert mused._idem_key_for("POST", "/cmd", {"idem_key": "123e4567-e89b-12d3-a456-426614174000"}) is not None
assert mused._idem_key_for("POST", "/cmd", {}) is None
assert mused._idem_key_for("POST", "/cmd", {"idem_key": "x"}) is None
assert mused._idem_key_for("GET", "/status", {"idem_key": "123e4567-e89b-12d3-a456-426614174000"}) is None
assert mused._idem_key_for("POST", "/status", {"idem_key": "123e4567-e89b-12d3-a456-426614174000"}) is None
ok("_idem_key_for gating")

# ---- DedupCache: same key twice -> fn once ----
c = mused.DedupCache()
calls = []
def fn():
    calls.append(1)
    return 200, '{"ok":true}'
assert c.execute("k1-00000001", fn) == (200, '{"ok":true}')
assert c.execute("k1-00000001", fn) == (200, '{"ok":true}')
assert len(calls) == 1
ok("dedup same key sequential")

# ---- DedupCache: concurrent same key -> fn once, both get result ----
c = mused.DedupCache()
calls = []
def slow_fn():
    time.sleep(0.3)
    calls.append(1)
    return 200, "slow-result"
results = {}
def worker(i):
    results[i] = c.execute("k2-00000002", slow_fn)
ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
[t.start() for t in ts]
[t.join() for t in ts]
assert len(calls) == 1, calls
assert all(v == (200, "slow-result") for v in results.values())
ok("dedup concurrent single-flight x3")

# ---- DedupCache: different keys independent ----
c = mused.DedupCache()
n = [0]
def counting():
    n[0] += 1
    return 200, f"r{n[0]}"
c.execute("k3-00000003", counting)
c.execute("k4-00000004", counting)
assert n[0] == 2
ok("different keys independent")

# ---- DedupCache: exception clears entry, next call re-executes ----
c = mused.DedupCache()
attempts = [0]
def flaky():
    attempts[0] += 1
    if attempts[0] == 1:
        raise ConnectionError("boom")
    return 200, "recovered"
try:
    c.execute("k5-00000005", flaky)
    raise AssertionError("should have raised")
except ConnectionError:
    pass
assert c.execute("k5-00000005", flaky) == (200, "recovered")
assert attempts[0] == 2
ok("exception clears entry, waiter/next re-executes")

# ---- DedupCache: exception wakes concurrent waiter, one takes over ----
c = mused.DedupCache()
attempts = [0]
lock = threading.Lock()
def flaky2():
    with lock:
        attempts[0] += 1
        first = attempts[0] == 1
    time.sleep(0.1)
    if first:
        raise ConnectionError("boom")
    return 200, "second-owner"
outcomes = {}
def w(i):
    try:
        outcomes[i] = c.execute("k6-00000006", flaky2)
    except ConnectionError:
        outcomes[i] = "raised"
ts = [threading.Thread(target=w, args=(i,)) for i in range(2)]
[t.start() for t in ts]
[t.join() for t in ts]
vals = sorted(str(v) for v in outcomes.values())
assert attempts[0] == 2, attempts
assert "(200, 'second-owner')" in vals, outcomes
ok("waiter takes over after owner exception")

# ---- DedupCache: TTL expiry re-executes ----
c = mused.DedupCache()
orig_ttl = mused.IDEM_TTL
mused.IDEM_TTL = 0.2
try:
    n2 = [0]
    def f2():
        n2[0] += 1
        return 200, "x"
    c.execute("k7-00000007", f2)
    c.execute("k7-00000007", f2)
    assert n2[0] == 1
    time.sleep(0.3)
    c.execute("k7-00000007", f2)
    assert n2[0] == 2
finally:
    mused.IDEM_TTL = orig_ttl
ok("TTL expiry re-executes")

# ---- musepcd build_mused_request: idem passthrough + automint ----
req, to = musepcd.build_mused_request(
    {"MODE": "cmd", "TARGET": "RD-928", "COMMAND": "echo hi",
     "IDEM-KEY": "123e4567-e89b-12d3-a456-426614174000"}, b"")
assert req["body"]["idem_key"] == "123e4567-e89b-12d3-a456-426614174000", req
req2, _ = musepcd.build_mused_request(
    {"MODE": "cmd", "TARGET": "RD-928", "COMMAND": "echo hi"}, b"")
assert len(req2["body"]["idem_key"]) == 36 and req2["body"]["idem_key"].count("-") == 4
req3, _ = musepcd.build_mused_request(
    {"MODE": "cmd", "TARGET": "RD-928", "COMMAND": "echo hi",
     "IDEM-KEY": "bad key!"}, b"")
assert req3["body"]["idem_key"] != "bad key!" and len(req3["body"]["idem_key"]) == 36
req4, _ = musepcd.build_mused_request(
    {"MODE": "write", "TARGET": "RD-928", "PATH": "C:\\x.txt",
     "CONTENT-LENGTH": "3", "IDEM-KEY": "123e4567-e89b-12d3-a456-426614174000"}, b"abc")
assert req4["body"]["idem_key"] == "123e4567-e89b-12d3-a456-426614174000"
assert req4["body"]["content"] == "abc"
ok("musepcd idem_key passthrough + automint (cmd/write)")

print(f"\nALL DAEMON TESTS PASSED ({passed} groups)")
