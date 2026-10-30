#!/usr/bin/env python3
"""Tests for the VPS 8471 request_id dedup state machine (dedup.py).

Covers: dedup/replay, concurrent waiter mount, LRU eviction, TTL expiry,
UNCERTAIN semantics, byte caps, and the /cmd integration pattern.

Run:  /tmp/dedup-venv/bin/python -m pytest test_dedup.py -v
(plain pytest; async parts are driven with asyncio.run, no pytest-asyncio)
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dedup import (
    DedupManager,
    RequestRecord,
    IN_FLIGHT,
    COMPLETED,
    UNCERTAIN,
    MAX_IN_FLIGHT_TOTAL,
    OverloadedError,
    new_request_id,
)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_mgr(**kw):
    kw.setdefault("clock", FakeClock())
    return DedupManager(**kw), kw["clock"]


# ---------------------------------------------------------------------------
# 1. Basic dedup / replay
# ---------------------------------------------------------------------------
def test_get_or_create_new_then_duplicate():
    mgr, _ = make_mgr()
    is_new, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is True
    assert rec.status == IN_FLIGHT
    assert rec.controller == "muse-a" and rec.target_pc == "PC-A"

    is_new2, rec2 = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new2 is False
    assert rec2 is rec  # same record object


def test_complete_then_replay_returns_cached():
    mgr, _ = make_mgr()
    mgr.get_or_create("r1", "muse-a", "PC-A")
    assert mgr.complete("r1", 200, b'{"ok": true}') is True

    is_new, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is False
    assert rec.status == COMPLETED
    assert rec.http_status == 200
    assert rec.response_body == b'{"ok": true}'


def test_complete_unknown_or_twice_is_safe():
    mgr, _ = make_mgr()
    assert mgr.complete("nope", 200, b"x") is False  # unknown id
    mgr.get_or_create("r1", "muse-a", "PC-A")
    assert mgr.complete("r1", 200, b"first") is True
    # second complete must NOT clobber the settled record
    assert mgr.complete("r1", 500, b"second") is False
    _, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert rec.http_status == 200 and rec.response_body == b"first"


def test_complete_accepts_str_body():
    mgr, _ = make_mgr()
    mgr.get_or_create("r1", "muse-a", "PC-A")
    assert mgr.complete("r1", 200, "text-body") is True
    _, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert rec.response_body == b"text-body"


def test_large_body_not_cached_only_status():
    mgr, clock = make_mgr(max_single_payload_bytes=1024)
    mgr.get_or_create("r1", "muse-a", "PC-A")
    big = b"x" * 2048
    assert mgr.complete("r1", 200, big) is True
    _, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert rec.status == COMPLETED
    assert rec.http_status == 200
    assert rec.response_body == b""  # dropped, only status kept
    assert mgr.stats()["total_bytes"] == 0


def test_new_request_id_unique():
    ids = {new_request_id() for _ in range(1000)}
    assert len(ids) == 1000


# ---------------------------------------------------------------------------
# 2. Concurrent waiter mount (asyncio)
# ---------------------------------------------------------------------------
def test_concurrent_waiters_all_get_completed_result():
    async def scenario():
        mgr = DedupManager()
        is_new, _ = mgr.get_or_create("r1", "muse-a", "PC-A")
        assert is_new

        results = []

        async def waiter(i):
            rec = await mgr.wait_for("r1", timeout=5.0)
            results.append((i, rec.status, rec.response_body))

        waiters = [asyncio.create_task(waiter(i)) for i in range(10)]
        await asyncio.sleep(0.05)  # let all waiters attach
        assert mgr.complete("r1", 200, b"shared-result") is True
        await asyncio.gather(*waiters)

        assert len(results) == 10
        for _, status, body in results:
            assert status == COMPLETED
            assert body == b"shared-result"
        # done_event stays set: late waiter returns immediately
        rec = await mgr.wait_for("r1", timeout=1.0)
        assert rec.status == COMPLETED

    asyncio.run(scenario())


def test_wait_for_unknown_raises_keyerror():
    async def scenario():
        mgr = DedupManager()
        try:
            await mgr.wait_for("ghost", timeout=1.0)
        except KeyError:
            return
        raise AssertionError("expected KeyError")

    asyncio.run(scenario())


def test_wait_for_timeout_raises():
    async def scenario():
        mgr = DedupManager()
        mgr.get_or_create("r1", "muse-a", "PC-A")
        try:
            await mgr.wait_for("r1", timeout=0.05)
        except asyncio.TimeoutError:
            return
        raise AssertionError("expected asyncio.TimeoutError")

    asyncio.run(scenario())


def test_wait_for_already_settled_returns_immediately():
    async def scenario():
        mgr = DedupManager()
        mgr.get_or_create("r1", "muse-a", "PC-A")
        mgr.complete("r1", 200, b"done")
        rec = await asyncio.wait_for(mgr.wait_for("r1", timeout=5.0), timeout=1.0)
        assert rec.status == COMPLETED

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3. LRU eviction
# ---------------------------------------------------------------------------
def test_lru_evicts_oldest_completed_first():
    mgr, clock = make_mgr(max_entries=3)
    for i in range(3):
        mgr.get_or_create(f"r{i}", "muse-a", "PC-A")
        mgr.complete(f"r{i}", 200, b"x")
        clock.advance(1)
    assert mgr.stats()["settled"] == 3

    mgr.get_or_create("r3", "muse-a", "PC-A")
    mgr.complete("r3", 200, b"x")
    # r0 (oldest completed) evicted, r1/r2/r3 remain
    assert mgr.get("r0") is None
    assert mgr.get("r1") is not None
    assert mgr.get("r2") is not None
    assert mgr.get("r3") is not None
    assert mgr.stats()["settled"] == 3


def test_in_flight_never_evicted():
    mgr, clock = make_mgr(max_entries=2)
    # fill cache with 2 completed
    for i in range(2):
        mgr.get_or_create(f"c{i}", "muse-a", "PC-A")
        mgr.complete(f"c{i}", 200, b"x")
        clock.advance(1)
    # add an in-flight record (would exceed entry cap if counted)
    is_new, _ = mgr.get_or_create("live", "muse-a", "PC-A")
    assert is_new
    # force more completions -> evictions must skip the in-flight record
    for i in range(2, 5):
        mgr.get_or_create(f"c{i}", "muse-a", "PC-A")
        mgr.complete(f"c{i}", 200, b"x")
        clock.advance(1)
    rec = mgr.get("live")
    assert rec is not None and rec.status == IN_FLIGHT
    # still works after the storm
    assert mgr.complete("live", 200, b"ok") is True


def test_byte_cap_evicts_oldest():
    mgr, clock = make_mgr(max_total_bytes=100, max_single_payload_bytes=1000)
    mgr.get_or_create("big1", "muse-a", "PC-A")
    mgr.complete("big1", 200, b"a" * 80)
    clock.advance(1)
    mgr.get_or_create("big2", "muse-a", "PC-A")
    mgr.complete("big2", 200, b"b" * 80)  # total 160 > 100 -> evict big1
    assert mgr.get("big1") is None
    assert mgr.get("big2") is not None
    assert mgr.stats()["total_bytes"] == 80


def test_byte_accounting_on_ttl_purge():
    mgr, clock = make_mgr(ttl_seconds=10)
    mgr.get_or_create("r1", "muse-a", "PC-A")
    mgr.complete("r1", 200, b"y" * 50)
    assert mgr.stats()["total_bytes"] == 50
    clock.advance(11)
    mgr.get_or_create("r2", "muse-a", "PC-A")  # triggers purge
    assert mgr.get("r1") is None
    assert mgr.stats()["total_bytes"] == 0


# ---------------------------------------------------------------------------
# 4. TTL expiry
# ---------------------------------------------------------------------------
def test_ttl_expiry_allows_reexecution():
    mgr, clock = make_mgr(ttl_seconds=300)
    mgr.get_or_create("r1", "muse-a", "PC-A")
    mgr.complete("r1", 200, b"old")
    clock.advance(299)
    is_new, _ = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is False  # still within TTL
    clock.advance(2)  # now 301 > 300
    is_new, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is True  # expired -> treated as new, may re-execute
    assert rec.status == IN_FLIGHT
    assert rec.response_body == b""


def test_ttl_measured_from_completion_not_creation():
    mgr, clock = make_mgr(ttl_seconds=100)
    mgr.get_or_create("r1", "muse-a", "PC-A")
    clock.advance(90)  # long execution, still in-flight
    mgr.complete("r1", 200, b"done")
    clock.advance(90)  # 90s since completion < 100
    is_new, _ = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is False
    clock.advance(20)  # 110s since completion > 100
    is_new, _ = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is True


# ---------------------------------------------------------------------------
# 5. UNCERTAIN semantics
# ---------------------------------------------------------------------------
def test_mark_uncertain_wakes_waiters_and_sticks():
    async def scenario():
        mgr = DedupManager()
        mgr.get_or_create("r1", "muse-a", "PC-A")

        seen = []

        async def waiter():
            rec = await mgr.wait_for("r1", timeout=5.0)
            seen.append(rec.status)

        t = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert mgr.mark_uncertain("r1") is True
        await t
        assert seen == [UNCERTAIN]

        # retry with same request_id must NOT create a new execution:
        # it keeps returning UNCERTAIN (caller answers 504) until TTL.
        is_new, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
        assert is_new is False
        assert rec.status == UNCERTAIN
        assert rec.http_status == 504

    asyncio.run(scenario())


def test_mark_uncertain_unknown_or_settled_is_noop():
    mgr, _ = make_mgr()
    assert mgr.mark_uncertain("ghost") is False
    mgr.get_or_create("r1", "muse-a", "PC-A")
    mgr.complete("r1", 200, b"ok")
    assert mgr.mark_uncertain("r1") is False  # already settled: never clobber
    _, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert rec.status == COMPLETED


def test_mark_uncertain_for_target():
    mgr, _ = make_mgr()
    mgr.get_or_create("a1", "muse-a", "PC-A")
    mgr.get_or_create("a2", "muse-a", "PC-A")
    mgr.get_or_create("b1", "muse-b", "PC-B")
    affected = mgr.mark_uncertain_for_target("PC-A")
    assert sorted(affected) == ["a1", "a2"]
    assert mgr.get("a1").status == UNCERTAIN
    assert mgr.get("a2").status == UNCERTAIN
    assert mgr.get("b1").status == IN_FLIGHT  # other PC untouched


def test_uncertain_expires_after_ttl():
    mgr, clock = make_mgr(ttl_seconds=60)
    mgr.get_or_create("r1", "muse-a", "PC-A")
    mgr.mark_uncertain("r1")
    clock.advance(61)
    is_new, rec = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is True  # fresh request_id cycle may proceed
    assert rec.status == IN_FLIGHT


# ---------------------------------------------------------------------------
# 6. /cmd integration pattern (from module docstring)
# ---------------------------------------------------------------------------
def test_integration_first_call_executes_second_replays():
    async def scenario():
        mgr = DedupManager()
        calls = []

        async def dispatch_to_pc(rid):
            calls.append(rid)
            await asyncio.sleep(0.01)
            return 200, b"pc-output"

        async def on_cmd(request_id, controller, target):
            is_new, rec = mgr.get_or_create(request_id, controller, target)
            if not is_new:
                if rec.status == COMPLETED:
                    return rec.http_status, rec.response_body
                if rec.status == UNCERTAIN:
                    return 504, b"UNCERTAIN"
                try:
                    rec = await mgr.wait_for(request_id, timeout=5.0)
                except asyncio.TimeoutError:
                    return 504, b"UNCERTAIN"
                return (rec.http_status, rec.response_body) if rec.status == COMPLETED else (504, b"UNCERTAIN")
            try:
                st, body = await dispatch_to_pc(request_id)
            except Exception:
                mgr.mark_uncertain(request_id)
                return 504, b"UNCERTAIN"
            mgr.complete(request_id, st, body)
            return st, body

        rid = new_request_id()
        # two concurrent callers, same request_id (retry storm)
        r1, r2 = await asyncio.gather(
            on_cmd(rid, "muse-a", "PC-A"), on_cmd(rid, "muse-a", "PC-A")
        )
        assert r1 == (200, b"pc-output")
        assert r2 == (200, b"pc-output")
        assert calls == [rid]  # PC executed exactly ONCE (at-most-once)

        # later retry: served from cache, PC not touched again
        r3 = await on_cmd(rid, "muse-a", "PC-A")
        assert r3 == (200, b"pc-output")
        assert calls == [rid]

    asyncio.run(scenario())


def test_integration_wss_drop_returns_504_not_reexecute():
    async def scenario():
        mgr = DedupManager()
        calls = []

        async def dispatch_to_pc(rid):
            calls.append(rid)
            await asyncio.sleep(10)  # PC never answers in this test
            return 200, b"never"

        async def on_cmd(request_id):
            is_new, rec = mgr.get_or_create(request_id, "muse-a", "PC-A")
            if not is_new:
                if rec.status == UNCERTAIN:
                    return 504
                return rec.http_status
            task = asyncio.create_task(dispatch_to_pc(request_id))
            await asyncio.sleep(0.05)
            # WSS drops mid-flight:
            mgr.mark_uncertain_for_target("PC-A")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return 504

        rid = new_request_id()
        assert await on_cmd(rid) == 504
        # retry with same id: still 504, PC executed at most once
        is_new, rec = mgr.get_or_create(rid, "muse-a", "PC-A")
        assert is_new is False and rec.status == UNCERTAIN
        assert calls == [rid]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# D1. IN_FLIGHT 上限 (SECURITY_V2.md 中危)：防内存耗尽 DoS
# ---------------------------------------------------------------------------
def test_in_flight_cap_raises_overloaded():
    mgr, _ = make_mgr(max_in_flight=3)
    for i in range(3):
        is_new, _ = mgr.get_or_create(f"r{i}", "muse-a", "PC-A")
        assert is_new is True
    try:
        mgr.get_or_create("r3", "muse-a", "PC-A")
    except OverloadedError as e:
        assert "too many in-flight" in str(e)
    else:
        raise AssertionError("OverloadedError not raised at cap")


def test_in_flight_cap_retry_exempt():
    # 已存在的 request_id (重试/去重命中) 不计入上限，永远允许
    mgr, _ = make_mgr(max_in_flight=2)
    mgr.get_or_create("r0", "muse-a", "PC-A")
    mgr.get_or_create("r1", "muse-a", "PC-A")
    # 重试已存在的 IN_FLIGHT：不抛异常
    is_new, rec = mgr.get_or_create("r0", "muse-a", "PC-A")
    assert is_new is False and rec.status == IN_FLIGHT
    # 新 id 仍被拒绝
    try:
        mgr.get_or_create("r2", "muse-a", "PC-A")
        raise AssertionError("should have raised")
    except OverloadedError:
        pass


def test_in_flight_cap_completed_retry_exempt():
    # COMPLETED 缓存命中也不计入上限
    mgr, _ = make_mgr(max_in_flight=1)
    mgr.get_or_create("r0", "muse-a", "PC-A")
    mgr.complete("r0", 200, b"ok")
    # 缓存命中：允许
    is_new, rec = mgr.get_or_create("r0", "muse-a", "PC-A")
    assert is_new is False and rec.status == COMPLETED
    # 新 id：IN_FLIGHT 已空 (completed 不占)，允许创建
    is_new, _ = mgr.get_or_create("r1", "muse-a", "PC-A")
    assert is_new is True


def test_in_flight_counter_releases_on_complete():
    mgr, _ = make_mgr(max_in_flight=2)
    mgr.get_or_create("r0", "muse-a", "PC-A")
    mgr.get_or_create("r1", "muse-a", "PC-A")
    assert mgr.stats()["in_flight"] == 2
    mgr.complete("r0", 200, b"ok")
    assert mgr.stats()["in_flight"] == 1
    is_new, _ = mgr.get_or_create("r2", "muse-a", "PC-A")
    assert is_new is True
    assert mgr.stats()["in_flight"] == 2


def test_in_flight_counter_releases_on_uncertain():
    mgr, _ = make_mgr(max_in_flight=2)
    mgr.get_or_create("r0", "muse-a", "PC-A")
    mgr.get_or_create("r1", "muse-a", "PC-A")
    mgr.mark_uncertain("r0")
    assert mgr.stats()["in_flight"] == 1
    is_new, _ = mgr.get_or_create("r2", "muse-a", "PC-A")
    assert is_new is True


def test_in_flight_counter_consistency_under_churn():
    # 混合操作后计数器必须与实际一致
    mgr, _ = make_mgr(max_in_flight=100)
    for i in range(50):
        mgr.get_or_create(f"r{i}", "muse-a", "PC-A")
    for i in range(0, 50, 2):
        mgr.complete(f"r{i}", 200, b"x")
    for i in range(1, 50, 4):
        mgr.mark_uncertain(f"r{i}")
    st = mgr.stats()
    real = sum(1 for r in mgr._records.values() if r.status == IN_FLIGHT)
    assert st["in_flight"] == real, f"counter={st['in_flight']} real={real}"
    # 25 complete + 13 uncertain = 38 retired, 12 in flight
    assert st["in_flight"] == 12, st["in_flight"]


def test_in_flight_default_limit_value():
    assert MAX_IN_FLIGHT_TOTAL == 10000
    mgr, _ = make_mgr()
    assert mgr.stats()["in_flight_limit"] == 10000


def test_overloaded_error_is_runtime_error():
    assert issubclass(OverloadedError, RuntimeError)
