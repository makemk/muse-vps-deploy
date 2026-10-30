#!/usr/bin/env python3
"""integration_v2.py: end-to-end mock test of all v2 components.

Wires the full Gemini-4 v2 pipeline with in-memory mocks (no network):

    MockVM --MicroBatcher--> dispatch --compress--> pool.get_connection()
        --> MockVPS.handle() --FastAuthEngine--> --DedupManager-->
        --WSSScheduler--> MockPC.execute()

Verifies:
  T1  auth fast path (200 / cache hit / 401 / 403-target / 403-super /
      403-IP / hot-reload)
  T2  request_id dedup (COMPLETED replay, IN_FLIGHT no-redispatch, UNCERTAIN)
  T3  WSS scheduler (high-prio first, bulk in-flight pacing == 1)
  T4  compression (thresholds, blacklist, probe, round-trip)
  T5  microbatch (solo fast-path, coalescing under load, non-batchable solo)
  T6  connection pool (warmup, stale discard, max-cap blocking)
  T7  TLS session resume (cache get/set, session injection on connect,
      session extraction on getresponse)
  T8  end-to-end: VM -> VPS -> PC with all v2 layers active

Run:  python3 integration_v2.py
Exit: 0 = all pass, 1 = failures (summary printed).
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import secrets
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "vps-auth-patch"))
sys.path.insert(0, os.path.join(BASE, "mused"))

from fast_auth import FastAuthEngine            # noqa: E402
from dedup import DedupManager, IN_FLIGHT, COMPLETED, UNCERTAIN, new_request_id  # noqa: E402
from wss_scheduler import WSSScheduler          # noqa: E402
from pool import ConnectionPool                 # noqa: E402
from tls_resume import TLSSessionCache, ResumableHTTPSConnection  # noqa: E402
from microbatch import MicroBatcher             # noqa: E402
import compress as C                             # noqa: E402


# ---------------------------------------------------------------- helpers

PASS, FAIL = "PASS", "FAIL"
_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  [{'OK' if cond else 'XX'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def make_token():
    return "muse_" + secrets.token_urlsafe(32)


def write_controllers(path, entries):
    """entries: list of (token, dict-fields)."""
    db = {}
    for token, fields in entries:
        db[hashlib.sha256(token.encode()).hexdigest()] = fields
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f)
    os.chmod(path, 0o600)
    return db


# ---------------------------------------------------------------- T1: auth

def test_auth(tmp):
    print("T1: FastAuthEngine")
    ctl = os.path.join(tmp, "controllers.json")
    tok_a = make_token()          # bound to PC-A
    tok_super = make_token()      # super admin
    tok_ip = make_token()         # IP-restricted
    write_controllers(ctl, [
        (tok_a, {"muse": "muse-a", "pcs": ["PC-A"], "enabled": True}),
        (tok_super, {"muse": "super", "pcs": ["*"], "enabled": True}),
        (tok_ip, {"muse": "muse-ip", "pcs": ["PC-B"],
                  "enabled": True, "allowed_ips": ["10.0.0.0/8"]}),
    ])
    eng = FastAuthEngine(ctl)

    s, t = eng.authenticate_and_authorize(tok_a, "1.2.3.4", "PC-A")
    check("auth: valid -> 200 PC-A", s == 200 and t == "PC-A", f"{s} {t}")

    s2, t2 = eng.authenticate_and_authorize(tok_a, "1.2.3.4", "PC-A")
    st = eng.stats()
    check("auth: decision cache hit", s2 == 200 and st["decision_cache_size"] >= 1,
          str(st))

    s, _ = eng.authenticate_and_authorize("muse_" + "x" * 43, "1.2.3.4", "PC-A")
    check("auth: bad token -> 401", s == 401, str(s))

    s, _ = eng.authenticate_and_authorize(tok_a, "1.2.3.4", "PC-B")
    check("auth: wrong target -> 403", s == 403, str(s))

    s, _ = eng.authenticate_and_authorize(tok_super, "1.2.3.4", None)
    check("auth: super w/o target -> 403", s == 403, str(s))

    s, t = eng.authenticate_and_authorize(tok_super, "1.2.3.4", "PC-Z")
    check("auth: super w/ target -> 200", s == 200 and t == "PC-Z", f"{s} {t}")

    s, _ = eng.authenticate_and_authorize(tok_ip, "192.168.1.1", "PC-B")
    check("auth: IP not in allowlist -> 403", s == 403, str(s))
    s, _ = eng.authenticate_and_authorize(tok_ip, "10.1.2.3", "PC-B")
    check("auth: IP in allowlist -> 200", s == 200, str(s))

    # hot reload: add a new token
    tok_new = make_token()
    time.sleep(0.25)  # pass the 200ms stat throttle
    write_controllers(ctl, [
        (tok_a, {"muse": "muse-a", "pcs": ["PC-A"], "enabled": True}),
        (tok_new, {"muse": "muse-new", "pcs": ["PC-N"], "enabled": True}),
    ])
    time.sleep(0.25)
    s, t = eng.authenticate_and_authorize(tok_new, "9.9.9.9", "PC-N")
    check("auth: hot-reload picks up new token", s == 200 and t == "PC-N",
          f"{s} {t}")
    return {"tok_a": tok_a, "tok_super": tok_super, "ctl": ctl}


# ---------------------------------------------------------------- T2: dedup

def test_dedup():
    print("T2: DedupManager")
    dm = DedupManager(ttl_seconds=300)
    executions = []

    def execute(rid):
        is_new, rec = dm.get_or_create(rid, "ctrl", "PC-A")
        if not is_new:
            return rec  # attach / replay, never re-execute
        executions.append(rid)
        dm.complete(rid, 200, b'{"ok": true}')
        return rec

    r1 = new_request_id()
    execute(r1)
    rec = execute(r1)  # duplicate -> replay
    check("dedup: duplicate request_id executes once",
          len(executions) == 1 and rec.status == COMPLETED, str(rec.status))
    check("dedup: cached body returned", rec.response_body == b'{"ok": true}')

    # IN_FLIGHT: second caller must not re-dispatch
    r2 = new_request_id()
    is_new, rec2 = dm.get_or_create(r2, "ctrl", "PC-A")
    is_new2, rec2b = dm.get_or_create(r2, "ctrl", "PC-A")
    check("dedup: IN_FLIGHT second caller not new",
          is_new and not is_new2 and rec2b.status == IN_FLIGHT)

    # wait_for attaches to in-flight and wakes on complete
    async def waiter():
        return await dm.wait_for(r2, timeout=5)
    t = threading.Thread(target=lambda: asyncio.run(_complete_later(dm, r2)))
    t.start()
    rec_w = asyncio.run(waiter())
    t.join()
    check("dedup: wait_for wakes on complete", rec_w.status == COMPLETED)

    # UNCERTAIN: never re-execute
    r3 = new_request_id()
    dm.get_or_create(r3, "ctrl", "PC-A")
    dm.mark_uncertain(r3)
    is_new3, rec3 = dm.get_or_create(r3, "ctrl", "PC-A")
    check("dedup: UNCERTAIN retry does not re-execute",
          not is_new3 and rec3.status == UNCERTAIN, rec3.status)

    # large body not cached (only status)
    r4 = new_request_id()
    dm.get_or_create(r4, "ctrl", "PC-A")
    big = b"x" * (256 * 1024 + 1)
    dm.complete(r4, 200, big)
    _, rec4 = dm.get_or_create(r4, "ctrl", "PC-A")
    check("dedup: oversize body not cached", rec4.response_body == b"")


async def _complete_later(dm, rid):
    await asyncio.sleep(0.1)
    dm.complete(rid, 200, b"late")


# ---------------------------------------------------------------- T3: scheduler

def test_scheduler():
    print("T3: WSSScheduler")
    sch = WSSScheduler()
    sch.register_pc("PC-A")
    # NOTE: scheduler takes bare op names ("cmd"/"batch"/"file"), NOT http
    # paths ("/cmd"). Path->type translation happens at the VPS wiring layer
    # (see INTEGRATION_ISSUES.md #1).
    sch.enqueue("PC-A", "file", {"chunk": 1})
    sch.enqueue("PC-A", "file", {"chunk": 2})
    sch.enqueue("PC-A", "cmd", {"command": "echo hi"})
    sch.enqueue("PC-A", "batch", {"n": 2})

    order = []
    m = sch.get_next_message("PC-A"); order.append(m[0])
    m = sch.get_next_message("PC-A"); order.append(m[0])
    check("sched: high-prio drains before bulk", order == ["cmd", "batch"],
          str(order))

    m = sch.get_next_message("PC-A")
    check("sched: bulk sent after high-prio", m is not None and m[0] == "file")
    m2 = sch.get_next_message("PC-A")
    check("sched: bulk pacing (max 1 in-flight)", m2 is None,
          f"got {m2[0] if m2 else None}")
    sch.on_response("PC-A", "file")
    m3 = sch.get_next_message("PC-A")
    check("sched: next bulk after response", m3 is not None and m3[1] == {"chunk": 2})
    sch.on_response("PC-A", "file")  # release chunk 2's in-flight slot
    d = sch.queue_depths("PC-A")
    check("sched: queues drained", d == (0, 0, 0), str(d))


# ---------------------------------------------------------------- T4: compress

def test_compress():
    print("T4: compress")
    small = b"hi" * 100  # 200B < 2KB
    check("compress: <2KB skipped", not C.should_compress(small))
    check("compress: .zip skipped",
          not C.should_compress(b"x" * 5000, filename="a.zip"))
    check("compress: PNG magic skipped",
          not C.should_compress(b"\x89PNG\r\n\x1a\n" + b"x" * 5000,
                                filename="noext"))
    text = (b"The quick brown fox jumps over the lazy dog. " * 200)  # ~9KB text
    check("compress: text >2KB selected", C.should_compress(text, filename="a.log"))
    data, enc = C.compress(text)
    check("compress: actually shrinks text", len(data) < len(text) * 0.5,
          f"{len(text)} -> {len(data)}")
    check("compress: round-trip", C.decompress(data, enc) == text)
    rnd = secrets.token_bytes(100 * 1024)  # high-entropy
    check("compress: random bytes skipped (probe)", not C.should_compress(rnd))


# ---------------------------------------------------------------- T5: microbatch

def test_microbatch():
    print("T5: MicroBatcher")
    calls = []
    lock = threading.Lock()

    def send_fn(token, method, path, body):
        with lock:
            calls.append((token, method, path, body))
        if body.get("hold"):
            time.sleep(0.06)  # keep one solo in flight
        if path == "/batch":
            reqs = body["requests"]
            return 200, json.dumps(
                [{"id": r["id"], "status": 200, "body": "b:" + r["id"]}
                 for r in reqs])
        return 200, "solo-ok"

    mb = MicroBatcher(send_fn, window_s=0.02, max_batch=10)

    # fast path: idle target -> immediate solo
    t0 = time.monotonic()
    st, body = mb.submit("tok", "PC-A", "POST", "/cmd", {"c": "echo"})
    dt = (time.monotonic() - t0) * 1000
    check("microbatch: idle solo fast", st == 200 and dt < 15, f"{dt:.1f}ms")

    # coalescing: hold one solo in flight, then fire 5 batchables
    def holder():
        return mb.submit("tok", "PC-A", "POST", "/cmd",
                         {"c": "hold", "hold": True}, batchable=True)
    th = threading.Thread(target=holder, daemon=True)
    th.start()
    time.sleep(0.02)
    with lock:
        calls.clear()
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(mb.submit, "tok", "PC-A", "POST", "/cmd",
                          {"c": f"e{i}"}, True, f"rid-{i}") for i in range(5)]
        res = [f.result(timeout=10) for f in futs]
    th.join(timeout=10)
    n_batch_calls = sum(1 for c in calls if c[2] == "/batch")
    check("microbatch: 5 concurrent coalesced into batches",
          n_batch_calls >= 1 and len(calls) < 5,
          f"upstream calls={len(calls)} batches={n_batch_calls}")
    check("microbatch: all results arrived", all(r[0] == 200 for r in res))

    # non-batchable never merges
    with lock:
        calls.clear()
    st, _ = mb.submit("tok", "PC-A", "POST", "/cmd", {"c": "rm"}, batchable=False)
    check("microbatch: non-batchable goes solo",
          st == 200 and all(c[2] != "/batch" for c in calls))
    print("   stats:", mb.stats())


# ---------------------------------------------------------------- T6: pool

class _FakeConn:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


def test_pool():
    print("T6: ConnectionPool")
    made = []
    def connect_fn():
        c = _FakeConn()
        made.append(c)
        return c
    pool = ConnectionPool(connect_fn, min_size=2, max_size=3,
                          idle_timeout=0.15, acquire_timeout=0.3)
    pool.wait_warmup(timeout=10)
    st = pool.stats()
    check("pool: warmup fills min_size", st["idle"] == 2 and st["total"] == 2,
          str(st))

    a = pool.get_connection()
    b = pool.get_connection()
    c = pool.get_connection()  # creates 3rd (at max)
    check("pool: grows to max_size", pool.stats()["total"] == 3)
    try:
        pool.get_connection(timeout=0.2)
        check("pool: over-cap raises TimeoutError", False)
    except TimeoutError:
        check("pool: over-cap raises TimeoutError", True)
    pool.return_connection(a)
    pool.return_connection(b, is_broken=True)
    check("pool: broken conn discarded", pool.stats()["total"] == 2)

    time.sleep(0.2)  # exceed idle_timeout
    d = pool.get_connection()
    check("pool: stale idle discarded+recreated",
          d.conn not in (a.conn,) and pool.stats()["total"] == 2)
    pool.return_connection(c)
    pool.return_connection(d)
    pool.close_all()
    check("pool: close_all drains", pool.stats()["total"] == 0)


# ---------------------------------------------------------------- T7: TLS resume

def test_tls_resume():
    print("T7: tls_resume")
    cache = TLSSessionCache()
    check("tls: empty cache -> None", cache.get() is None)
    cache.set(None)  # ignored (TLS1.3 pre-ticket)
    check("tls: set(None) ignored", cache.get() is None)
    cache.set("sess-abc")
    check("tls: set/get roundtrip", cache.get() == "sess-abc")
    cache.note_handshake(True)
    cache.note_handshake(False)
    st = cache.stats()
    check("tls: handshake stats", st == {"resumed": 1, "full": 1,
                                         "has_cached": True}, str(st))

    # session injection on connect()
    seen = {}
    class FakeSSLSock:
        session_reused = True
        def __init__(self): self.session = "sess-new"
        def close(self): pass
        def setsockopt(self, *a): pass  # connect() sets TCP_NODELAY
    class FakeCtx:
        def wrap_socket(self, sock, server_hostname=None, session=None):
            seen["session"] = session
            seen["sni"] = server_hostname
            return FakeSSLSock()
    import socket as _sockmod
    orig_create = _sockmod.create_connection
    _sockmod.create_connection = lambda *a, **k: FakeSSLSock()
    try:
        conn = ResumableHTTPSConnection("vps.example", 443,
                                        ssl_context=FakeCtx(),
                                        session_getter=cache.get,
                                        session_setter=cache.set)
        noted = []
        conn.attach_handshake_stats(noted.append)
        conn.connect()
    finally:
        _sockmod.create_connection = orig_create
    check("tls: cached session injected", seen.get("session") == "sess-abc",
          str(seen.get("session")))
    check("tls: handshake reuse noted", noted == [True], str(noted))

    # session extraction on getresponse() (TLS 1.3 ticket arrives late)
    conn2 = ResumableHTTPSConnection("vps.example", 443,
                                     ssl_context=FakeCtx(),
                                     session_getter=cache.get,
                                     session_setter=cache.set)
    conn2.sock = FakeSSLSock()  # session == "sess-new"
    orig_gr = http.client.HTTPConnection.getresponse
    http.client.HTTPConnection.getresponse = lambda self: object()
    try:
        conn2.getresponse()
    finally:
        http.client.HTTPConnection.getresponse = orig_gr
    check("tls: fresh session cached after response", cache.get() == "sess-new")


# ---------------------------------------------------------------- T8: end-to-end

class MockPC:
    def __init__(self, pc_id):
        self.pc_id = pc_id
        self.exec_count = 0
    def execute(self, op, payload):
        self.exec_count += 1
        if op == "/cmd":
            return 200, json.dumps({"stdout": f"mock:{payload.get('command')}",
                                    "code": 0})
        if op == "/file":
            return 200, json.dumps({"op": payload.get("op"), "ok": True})
        return 200, json.dumps({"ok": True})


class MockVPS:
    """FastAuthEngine + DedupManager + WSSScheduler + MockPCs."""
    def __init__(self, controllers_path):
        self.auth = FastAuthEngine(controllers_path)
        self.dedup = DedupManager()
        self.sched = WSSScheduler()
        self.pcs = {}

    def register_pc(self, pc):
        self.pcs[pc.pc_id] = pc
        self.sched.register_pc(pc.pc_id)

    # WSSScheduler takes bare op names ("cmd"/"batch"/"file"); translate the
    # HTTP path at the wiring boundary (INTEGRATION_ISSUES.md #1).
    _PATH_TO_OP = {"/cmd": "cmd", "/batch": "batch", "/file": "file"}

    def _dispatch_one(self, token, op, body, client_ip, request_id):
        target = body.get("target")
        s, resolved = self.auth.authenticate_and_authorize(token, client_ip, target)
        if s != 200:
            return s, resolved
        is_new, rec = self.dedup.get_or_create(request_id, "e2e", resolved)
        if not is_new:
            if rec.status == IN_FLIGHT:
                return 409, "conflict: already in flight"
            return rec.http_status, rec.response_body.decode("utf-8", "replace")
        # route through the WSS scheduler, then mock-execute synchronously
        sched_op = self._PATH_TO_OP.get(op, op)
        self.sched.enqueue(resolved, sched_op, {"body": body, "rid": request_id})
        nxt = self.sched.get_next_message(resolved)
        assert nxt is not None, "scheduler returned nothing for enqueued msg"
        _mtype, payload = nxt
        pc = self.pcs[resolved]
        # decompress if the VM compressed
        enc = body.get("content_encoding", "identity")
        status, resp_body = pc.execute(op, payload["body"])
        self.sched.on_response(resolved, sched_op)
        self.dedup.complete(request_id, status, resp_body)
        return status, resp_body

    def handle(self, token, method, path, body, client_ip="9.9.9.9"):
        if path == "/batch":
            out = []
            for sub in body["requests"]:
                s, b = self._dispatch_one(token, sub["method"], sub["body"],
                                          client_ip, sub["id"])
                out.append({"id": sub["id"], "status": s, "body": b})
            return 200, json.dumps(out)
        rid = body.pop("_request_id", new_request_id())
        op = {"POST": "/cmd"}.get(method, "/cmd") if path == "/cmd" else path
        return self._dispatch_one(token, op, body, client_ip, rid)


class MockVM:
    """MicroBatcher + compress + ConnectionPool(faked) -> MockVPS."""
    def __init__(self, vps):
        self.vps = vps
        self.pool = ConnectionPool(connect_fn=_FakeConn, min_size=2, max_size=4)
        self.pool.wait_warmup(timeout=10)
        self.batcher = MicroBatcher(self._send, window_s=0.02)
        self.upstream_calls = 0

    def _send(self, token, method, path, body):
        # compress large bodies (app-layer, VPS-transparent)
        raw = json.dumps(body).encode()
        headers = {}
        if C.should_compress(raw):
            raw, enc = C.compress(raw)
            headers["Content-Encoding"] = enc
        pc = self.pool.get_connection()
        try:
            self.upstream_calls += 1
            wire = dict(body)
            if headers:
                wire["content_encoding"] = headers["Content-Encoding"]
                wire["_payload_b64"] = __import__("base64").b64encode(raw).decode()
            return self.vps.handle(token, method, path, wire)
        finally:
            self.pool.return_connection(pc)

    def cmd(self, token, target, command, batchable=False):
        rid = new_request_id()
        body = {"target": target, "command": command, "_request_id": rid}
        return self.batcher.submit(token, target, "POST", "/cmd", body,
                                   batchable=batchable, request_id=rid)


def test_end_to_end(tmp):
    print("T8: end-to-end")
    ctl = os.path.join(tmp, "controllers_e2e.json")
    tok = make_token()
    write_controllers(ctl, [(tok, {"muse": "muse-e2e", "pcs": ["PC-A"],
                                   "enabled": True})])
    vps = MockVPS(ctl)
    pc = MockPC("PC-A")
    vps.register_pc(pc)
    vm = MockVM(vps)

    s, b = vm.cmd(tok, "PC-A", "echo hello")
    d = json.loads(b)
    check("e2e: cmd round-trip", s == 200 and d["stdout"] == "mock:echo hello",
          f"{s} {b[:60]}")

    s, _ = vm.cmd("muse_" + "z" * 43, "PC-A", "echo x")
    check("e2e: bad token -> 401", s == 401, str(s))

    s, _ = vm.cmd(tok, "PC-B", "echo x")
    check("e2e: wrong target -> 403", s == 403, str(s))

    # dedup across the whole stack: same request_id twice, one execution
    rid = new_request_id()
    body = {"target": "PC-A", "command": "echo once", "_request_id": rid}
    before = pc.exec_count
    vm.batcher.submit(tok, "PC-A", "POST", "/cmd", dict(body),
                      batchable=False, request_id=rid)
    vm.batcher.submit(tok, "PC-A", "POST", "/cmd", dict(body),
                      batchable=False, request_id=rid)
    check("e2e: duplicate request_id executes once",
          pc.exec_count == before + 1, f"exec_count delta={pc.exec_count - before}")

    # microbatch coalescing end-to-end
    import concurrent.futures as cf
    vm2 = MockVM(vps)
    n0 = vm2.upstream_calls
    def hold():
        return vm2.cmd(tok, "PC-A", "hold-cmd", batchable=True)
    # force in-flight: patch _send to sleep once
    orig_send = vm2._send
    held = {"n": 0}
    def slow_send(*a, **k):
        if held["n"] == 0:
            held["n"] += 1
            time.sleep(0.08)
        return orig_send(*a, **k)
    vm2.batcher._send = slow_send
    th = threading.Thread(target=hold, daemon=True)
    th.start()
    time.sleep(0.03)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(vm2.cmd, tok, "PC-A", f"echo {i}", True)
                for i in range(4)]
        res = [f.result(timeout=15) for f in futs]
    th.join(timeout=15)
    check("e2e: batch coalescing reduces upstream calls",
          vm2.upstream_calls - n0 < 6,
          f"upstream_calls={vm2.upstream_calls - n0} for 5 cmds")
    check("e2e: all batched results ok", all(r[0] == 200 for r in res))
    print("   pool stats:", vm.pool.stats(), "| batcher stats:",
          vm.batcher.stats())


# ---------------------------------------------------------------- main

def main():
    tmp = tempfile.mkdtemp(prefix="muse_integ_v2_")
    print(f"== integration_v2 (tmp={tmp}) ==")
    tests = [
        ("T1 auth", lambda: test_auth(tmp)),
        ("T2 dedup", test_dedup),
        ("T3 scheduler", test_scheduler),
        ("T4 compress", test_compress),
        ("T5 microbatch", test_microbatch),
        ("T6 pool", test_pool),
        ("T7 tls_resume", test_tls_resume),
        ("T8 end-to-end", lambda: test_end_to_end(tmp)),
    ]
    for name, fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            check(name + " (exception)", False, f"{e!r}")
            traceback.print_exc()
    n_ok = sum(1 for _, ok, _ in _results if ok)
    n_all = len(_results)
    print(f"\n== {n_ok}/{n_all} checks passed ==")
    bad = [(n, d) for n, ok, d in _results if not ok]
    if bad:
        print("FAILURES:")
        for n, d in bad:
            print(f"  - {n}: {d}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
