#!/usr/bin/env python3
"""Tests for the VPS-side multiplex /batch handler (batch_multiplex.py).

Covers: multiplex/legacy dispatch discrimination, happy path, partial
failure isolation, dedup replay + granularity, UNCERTAIN stickiness,
IN_FLIGHT attach, overload shedding, and envelope errors.

Run:  python3 -m pytest test_batch_multiplex.py test_dedup.py -q
(plain pytest; async parts are driven with asyncio.run, no pytest-asyncio)
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dedup import DedupManager, OverloadedError, batch_sub_key
from batch_multiplex import (
    MAX_SUB_REQUESTS,
    is_multiplex_batch,
    is_legacy_shell_batch,
    handle_batch_multiplex,
)


def run(coro):
    return asyncio.run(coro)


def make_batch(batch_id="b1", n=3):
    return {
        "batch_id": batch_id,
        "requests": [
            {"id": f"s{i}", "method": "POST", "path": "/cmd",
             "body": {"n": i}}
            for i in range(n)
        ],
    }


class Recorder:
    """Fake execute(): records calls; configurable per-sub behavior."""

    def __init__(self):
        self.calls = []          # (sub_id, composite_id, method, path, body)
        self.fail_for = set()    # sub_ids that raise
        self.blockers = {}       # sub_id -> asyncio.Event to wait on

    async def __call__(self, sub_id, composite_id, method, path, body):
        self.calls.append((sub_id, composite_id, method, path, body))
        ev = self.blockers.get(sub_id)
        if ev is not None:
            await ev.wait()
        if sub_id in self.fail_for:
            raise RuntimeError("boom")
        return 200, f"ok-{sub_id}".encode()


# ---------------------------------------------------------------------------
# 1. Dispatch discrimination
# ---------------------------------------------------------------------------
def test_is_multiplex_batch():
    assert is_multiplex_batch({"batch_id": "x", "requests": []}) is True
    assert is_multiplex_batch({"batch_id": "x",
                               "requests": [{"id": 1}]}) is True
    # legacy shape -> not multiplex
    assert is_multiplex_batch({"commands": ["a", "b"]}) is False
    # garbage -> False, not an exception
    for bad in (None, [], "str", 42, {"requests": "nope"},
                {"requests": None}, {"nope": 1}):
        assert is_multiplex_batch(bad) is False


def test_is_legacy_shell_batch():
    assert is_legacy_shell_batch({"commands": ["a"]}) is True
    assert is_legacy_shell_batch({"requests": []}) is False
    for bad in (None, [], "str", 42, {"commands": "nope"}, {}):
        assert is_legacy_shell_batch(bad) is False


def test_multiplex_wins_over_legacy_when_both_present():
    both = {"requests": [], "commands": []}
    assert is_multiplex_batch(both) is True


def test_batch_sub_key_format():
    assert batch_sub_key("b1", "s2") == "batch:b1:s2"


# ---------------------------------------------------------------------------
# 2. Happy path
# ---------------------------------------------------------------------------
def test_happy_path_order_and_types():
    mgr = DedupManager()
    ex = Recorder()
    status, items = run(handle_batch_multiplex(
        make_batch(), controller="c", target_pc="PC-A",
        dedup=mgr, execute=ex))
    assert status == 200
    assert len(items) == 3
    for i, item in enumerate(items):
        assert item["id"] == f"s{i}"
        assert item["status"] == 200
        assert item["body"] == f"ok-s{i}"
        assert isinstance(item["body"], str)
    assert json.dumps(items)  # whole array must be json-serializable
    assert [c[0] for c in ex.calls] == ["s0", "s1", "s2"]
    # execute got the composite request id + unchanged path
    assert ex.calls[0][1] == batch_sub_key("b1", "s0")
    assert ex.calls[0][2] == "POST" and ex.calls[0][3] == "/cmd"
    assert ex.calls[0][4] == {"n": 0}


def test_sub_defaults():
    mgr = DedupManager()
    ex = Recorder()
    status, items = run(handle_batch_multiplex(
        {"batch_id": "b", "requests": [{"id": "x", "path": "/status"}]},
        controller="c", target_pc="PC-A", dedup=mgr, execute=ex))
    assert status == 200
    assert items[0]["status"] == 200
    assert ex.calls[0][2] == "POST"   # method default
    assert ex.calls[0][4] == {}       # body default


# ---------------------------------------------------------------------------
# 3. Partial-failure isolation
# ---------------------------------------------------------------------------
def test_partial_failure_one_sub_raises():
    mgr = DedupManager()
    ex = Recorder()
    ex.fail_for = {"s1"}
    status, items = run(handle_batch_multiplex(
        make_batch(), controller="c", target_pc="PC-A",
        dedup=mgr, execute=ex))
    assert status == 200  # outer stays 200
    assert items[0]["status"] == 200 and items[0]["body"] == "ok-s0"
    assert items[1]["status"] == 504
    assert json.loads(items[1]["body"])["error"] == "EXECUTION_UNCERTAIN"
    assert items[2]["status"] == 200 and items[2]["body"] == "ok-s2"
    # s1's record is UNCERTAIN: a same-batch retry must NOT re-execute
    assert mgr.get(batch_sub_key("b1", "s1")).status == "UNCERTAIN"


def test_per_sub_validation_errors():
    mgr = DedupManager()
    ex = Recorder()
    body = {"batch_id": "b", "requests": [
        {"method": "POST", "path": "/cmd"},          # missing id
        {"id": "bad-path", "method": "POST"},        # missing path
        {"id": "ok", "path": "/status"},             # fine
    ]}
    status, items = run(handle_batch_multiplex(
        body, controller="c", target_pc="PC-A", dedup=mgr, execute=ex))
    assert status == 200
    assert items[0] == {"id": None, "status": 400,
                        "body": "missing sub-request id"}
    assert items[1]["id"] == "bad-path" and items[1]["status"] == 400
    assert items[2]["status"] == 200
    assert len(ex.calls) == 1 and ex.calls[0][0] == "ok"


# ---------------------------------------------------------------------------
# 4. Dedup replay + granularity
# ---------------------------------------------------------------------------
def test_dedup_replay_same_batch():
    mgr = DedupManager()
    ex = Recorder()
    kw = dict(controller="c", target_pc="PC-A", dedup=mgr, execute=ex)
    batch = make_batch(batch_id="b1", n=2)
    s1, items1 = run(handle_batch_multiplex(batch, **kw))
    s2, items2 = run(handle_batch_multiplex(batch, **kw))
    assert (s1, s2) == (200, 200)
    assert items1 == items2          # identical replay
    assert len(ex.calls) == 2        # execute ran ONCE per sub


def test_granularity_new_batch_id_re_executes():
    mgr = DedupManager()
    ex = Recorder()
    kw = dict(controller="c", target_pc="PC-A", dedup=mgr, execute=ex)
    run(handle_batch_multiplex(make_batch(batch_id="b1", n=1), **kw))
    run(handle_batch_multiplex(make_batch(batch_id="b2", n=1), **kw))
    # same sub_id "s0", different batch_id -> new execution
    assert len(ex.calls) == 2
    assert ex.calls[0][1] == batch_sub_key("b1", "s0")
    assert ex.calls[1][1] == batch_sub_key("b2", "s0")


def test_uncertain_stickiness():
    mgr = DedupManager()
    ex = Recorder()
    kw = dict(controller="c", target_pc="PC-A", dedup=mgr, execute=ex)
    # set up an UNCERTAIN record first (as if a previous attempt died)
    key = batch_sub_key("b1", "s0")
    mgr.get_or_create(key, "c", "PC-A")
    assert mgr.mark_uncertain(key) is True
    status, items = run(handle_batch_multiplex(
        make_batch(batch_id="b1", n=2), **kw))
    assert status == 200
    assert items[0]["status"] == 504      # stuck 504, no execute call
    assert items[1]["status"] == 200
    assert [c[0] for c in ex.calls] == ["s1"]


# ---------------------------------------------------------------------------
# 5. IN_FLIGHT attach (concurrent duplicate batch)
# ---------------------------------------------------------------------------
def test_in_flight_attach():
    mgr = DedupManager()
    ex = Recorder()
    ev = asyncio.Event()
    ex.blockers["s0"] = ev
    kw = dict(controller="c", target_pc="PC-A", dedup=mgr, execute=ex)

    async def main():
        t1 = asyncio.create_task(
            handle_batch_multiplex(make_batch(batch_id="b1", n=1), **kw))
        await asyncio.sleep(0.05)  # let t1 claim s0's record
        t2 = asyncio.create_task(
            handle_batch_multiplex(make_batch(batch_id="b1", n=1), **kw))
        ev.set()                   # release the blocked execute
        return await t1, await t2

    (s1, items1), (s2, items2) = run(main())
    assert (s1, s2) == (200, 200)
    assert items1[0]["status"] == 200 and items2[0]["status"] == 200
    assert items1[0]["body"] == items2[0]["body"] == "ok-s0"
    assert len(ex.calls) == 1  # execute ran exactly once


def test_wait_timeout_becomes_504():
    mgr = DedupManager()
    ex = Recorder()
    key = batch_sub_key("b1", "s0")
    mgr.get_or_create(key, "c", "PC-A")  # stuck IN_FLIGHT, nobody completes
    status, items = run(handle_batch_multiplex(
        make_batch(batch_id="b1", n=1), controller="c", target_pc="PC-A",
        dedup=mgr, execute=ex, timeout=0.05))
    assert status == 200
    assert items[0]["status"] == 504
    assert not ex.calls


# ---------------------------------------------------------------------------
# 6. Overload shedding
# ---------------------------------------------------------------------------
def test_overload_sub_gets_503_outer_stays_200():
    mgr = DedupManager(max_in_flight=1)
    mgr.get_or_create("stuck", "c", "PC-A")  # occupies the single slot
    ex = Recorder()
    status, items = run(handle_batch_multiplex(
        make_batch(batch_id="b9", n=2), controller="c", target_pc="PC-A",
        dedup=mgr, execute=ex))
    assert status == 200
    assert items[0] == {"id": "s0", "status": 503,
                        "body": "SERVER_OVERLOADED"}
    assert items[1] == {"id": "s1", "status": 503,
                        "body": "SERVER_OVERLOADED"}
    assert not ex.calls


# ---------------------------------------------------------------------------
# 7. Envelope errors
# ---------------------------------------------------------------------------
def test_envelope_errors():
    mgr = DedupManager()
    ex = Recorder()
    kw = dict(controller="c", target_pc="PC-A", dedup=mgr, execute=ex)

    status, items = run(handle_batch_multiplex("garbage", **kw))
    assert status == 400 and items == [{"error": "malformed batch: "
                                                 "body must be a dict"}]

    status, items = run(handle_batch_multiplex({"batch_id": "x"}, **kw))
    assert status == 400

    status, items = run(handle_batch_multiplex(
        {"batch_id": "x", "requests": "nope"}, **kw))
    assert status == 400

    big = {"batch_id": "x", "requests": [
        {"id": i, "path": "/cmd"} for i in range(MAX_SUB_REQUESTS + 1)]}
    status, items = run(handle_batch_multiplex(big, **kw))
    assert status == 400
    assert "too many sub-requests" in items[0]["error"]
    assert not ex.calls  # nothing executed on envelope rejection

    # exactly MAX_SUB_REQUESTS is accepted
    ok = {"batch_id": "x", "requests": [
        {"id": i, "path": "/cmd"} for i in range(MAX_SUB_REQUESTS)]}
    status, items = run(handle_batch_multiplex(ok, **kw))
    assert status == 200 and len(items) == MAX_SUB_REQUESTS


def test_empty_requests_is_200_empty():
    mgr = DedupManager()
    ex = Recorder()
    status, items = run(handle_batch_multiplex(
        {"batch_id": "b", "requests": []}, controller="c", target_pc="PC-A",
        dedup=mgr, execute=ex))
    assert (status, items) == (200, [])
    assert not ex.calls


def test_missing_batch_id_server_generates():
    mgr = DedupManager()
    ex = Recorder()
    status, items = run(handle_batch_multiplex(
        {"requests": [{"id": "s", "path": "/cmd"}]},
        controller="c", target_pc="PC-A", dedup=mgr, execute=ex))
    assert status == 200 and items[0]["status"] == 200
    composite = ex.calls[0][1]
    assert composite.startswith("batch:") and composite.endswith(":s")
    # same envelope twice -> same generated batch_id? No: each call
    # generates a fresh one, so a second call re-executes (fresh batch).
    run(handle_batch_multiplex(
        {"requests": [{"id": "s", "path": "/cmd"}]},
        controller="c", target_pc="PC-A", dedup=mgr, execute=ex))
    assert len(ex.calls) == 2


def test_str_body_passthrough_and_json_array():
    mgr = DedupManager()

    async def ex(sub_id, cid, method, path, body):
        return 200, "plain text"

    status, items = run(handle_batch_multiplex(
        make_batch(batch_id="b", n=1), controller="c", target_pc="PC-A",
        dedup=mgr, execute=ex))
    assert items[0]["body"] == "plain text"
    json.dumps(items)
