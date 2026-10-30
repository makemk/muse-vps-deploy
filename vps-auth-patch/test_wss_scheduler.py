"""Tests for wss_scheduler.py — Gemini-4 §4.2 dual-priority WSS scheduler."""

import asyncio

import pytest

from wss_scheduler import WSSScheduler


@pytest.fixture()
def sched():
    s = WSSScheduler()
    s.register_pc("PC-A")
    return s


def test_priority_cmd_before_file(sched):
    """A queued /cmd must be sent before a queued /file."""
    sched.enqueue("PC-A", "file", {"id": "f1"})
    sched.enqueue("PC-A", "cmd", {"id": "c1"})
    t, p = sched.get_next_message("PC-A")
    assert t == "cmd" and p == {"id": "c1"}
    t, p = sched.get_next_message("PC-A")
    assert t == "file" and p == {"id": "f1"}


def test_batch_is_high_priority(sched):
    """/batch joins /cmd in high_prio, ahead of /file."""
    sched.enqueue("PC-A", "file", {"id": "f1"})
    sched.enqueue("PC-A", "batch", {"id": "b1"})
    t, _ = sched.get_next_message("PC-A")
    assert t == "batch"


def test_unknown_type_defaults_to_high(sched):
    """Unrecognized types must not be stuck behind bulk traffic."""
    sched.enqueue("PC-A", "file", {"id": "f1"})
    sched.enqueue("PC-A", "weird_new_op", {"id": "w1"})
    t, _ = sched.get_next_message("PC-A")
    assert t == "weird_new_op"


def test_bulk_in_flight_limit(sched):
    """At most 1 /file chunk IN_FLIGHT per PC."""
    sched.enqueue("PC-A", "file", {"id": "f1"})
    sched.enqueue("PC-A", "file", {"id": "f2"})
    sched.enqueue("PC-A", "file", {"id": "f3"})

    t, p = sched.get_next_message("PC-A")
    assert (t, p["id"]) == ("file", "f1")
    assert sched.queue_depths("PC-A") == (0, 2, 1)

    # Second bulk chunk must NOT be sent while f1 is in flight.
    assert sched.get_next_message("PC-A") is None

    # f1's response releases the slot -> f2 becomes sendable.
    sched.on_response("PC-A", "file")
    assert sched.queue_depths("PC-A") == (0, 2, 0)
    t, p = sched.get_next_message("PC-A")
    assert (t, p["id"]) == ("file", "f2")


def test_cmd_preempts_during_bulk_in_flight(sched):
    """A /cmd is sent IMMEDIATELY even while a /file chunk is in flight.

    High-prio messages are small control frames; emitting one does not bloat
    the kernel TCP buffer. Only bulk chunks are paced by the in-flight window.
    """
    sched.enqueue("PC-A", "file", {"id": "f1"})
    assert sched.get_next_message("PC-A")[1] == {"id": "f1"}  # f1 in flight

    sched.enqueue("PC-A", "file", {"id": "f2"})
    sched.enqueue("PC-A", "cmd", {"id": "c1"})
    sched.enqueue("PC-A", "file", {"id": "f3"})

    # c1 jumps the queue instantly — zero wait behind f1's 64KB.
    assert sched.get_next_message("PC-A") == ("cmd", {"id": "c1"})
    # f2 still blocked by the in-flight window.
    assert sched.get_next_message("PC-A") is None

    sched.on_response("PC-A", "file")  # f1 done
    assert sched.get_next_message("PC-A") == ("file", {"id": "f2"})
    sched.on_response("PC-A", "file")
    assert sched.get_next_message("PC-A") == ("file", {"id": "f3"})
    assert sched.get_next_message("PC-A") is None


def test_high_prio_has_no_in_flight_limit(sched):
    """/cmd messages are never throttled by the bulk in-flight window."""
    for i in range(5):
        sched.enqueue("PC-A", "cmd", {"id": f"c{i}"})
    for i in range(5):
        item = sched.get_next_message("PC-A")
        assert item is not None and item[1] == {"id": f"c{i}"}
    assert sched.get_next_message("PC-A") is None


def test_per_pc_isolation():
    """PC-A's bulk in-flight window must not block PC-B."""
    s = WSSScheduler()
    s.register_pc("PC-A")
    s.register_pc("PC-B")
    s.enqueue("PC-A", "file", {"id": "a1"})
    s.enqueue("PC-B", "file", {"id": "b1"})
    s.enqueue("PC-A", "file", {"id": "a2"})

    assert s.get_next_message("PC-A")[1] == {"id": "a1"}  # A in flight
    assert s.get_next_message("PC-A") is None            # A blocked
    assert s.get_next_message("PC-B")[1] == {"id": "b1"}  # B unaffected


def test_on_response_for_unknown_pc_is_noop(sched):
    sched.on_response("PC-GHOST", "file")  # must not raise
    assert sched.get_next_message("PC-GHOST") is None


def test_enqueue_unknown_pc_raises(sched):
    with pytest.raises(KeyError):
        sched.enqueue("PC-GHOST", "cmd", {})


def test_unregister_drops_queues():
    s = WSSScheduler()
    s.register_pc("PC-A")
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    s.enqueue("PC-A", "file", {"id": "f1"})
    dropped = s.unregister_pc("PC-A")
    assert dropped == 2
    assert not s.is_registered("PC-A")
    assert s.queue_depths("PC-A") is None
    assert s.unregister_pc("PC-A") == 0  # idempotent


def test_stats(sched):
    sched.enqueue("PC-A", "cmd", {"id": "c1"})
    sched.enqueue("PC-A", "file", {"id": "f1"})
    sched.get_next_message("PC-A")  # c1
    sched.get_next_message("PC-A")  # f1 in flight
    st = sched.stats("PC-A")
    assert st["sent_high"] == 1
    assert st["sent_bulk"] == 1
    assert st["bulk_in_flight"] == 1
    assert st["high_queued"] == 0 and st["bulk_queued"] == 0


def test_wait_next_returns_message():
    async def main():
        s = WSSScheduler()
        s.register_pc("PC-A")

        async def producer():
            await asyncio.sleep(0.05)
            s.enqueue("PC-A", "cmd", {"id": "c1"})

        task = asyncio.create_task(producer())
        item = await asyncio.wait_for(s.wait_next("PC-A"), timeout=2.0)
        await task
        assert item == ("cmd", {"id": "c1"})

    asyncio.run(main())


def test_wait_next_unregistered_returns_none():
    async def main():
        s = WSSScheduler()
        s.register_pc("PC-A")

        async def unregister():
            await asyncio.sleep(0.05)
            s.unregister_pc("PC-A")

        task = asyncio.create_task(unregister())
        item = await asyncio.wait_for(s.wait_next("PC-A"), timeout=2.0)
        await task
        assert item is None

    asyncio.run(main())


def test_wait_next_bulk_pacing():
    """wait_next blocks for bulk until in-flight slot frees."""

    async def main():
        s = WSSScheduler()
        s.register_pc("PC-A")
        s.enqueue("PC-A", "file", {"id": "f1"})
        s.enqueue("PC-A", "file", {"id": "f2"})

        first = await asyncio.wait_for(s.wait_next("PC-A"), timeout=2.0)
        assert first[1] == {"id": "f1"}

        async def respond():
            await asyncio.sleep(0.05)
            s.on_response("PC-A", "file")

        task = asyncio.create_task(respond())
        second = await asyncio.wait_for(s.wait_next("PC-A"), timeout=2.0)
        await task
        assert second[1] == {"id": "f2"}

    asyncio.run(main())


# ---- P0-1: path-style msg_type normalization ----

def test_path_style_file_lands_in_bulk(sched):
    """enqueue(pc, "/file", ...) must land in bulk, not the high_prio fail-safe."""
    sched.enqueue("PC-A", "/file", {"id": "f1"})
    assert sched.queue_depths("PC-A") == (0, 1, 0)


def test_path_style_file_returns_normalized_name(sched):
    """get_next_message() returns the normalized bare name, not "/file",
    so the sender can match it against PC responses."""
    sched.enqueue("PC-A", "/file", {"id": "f1"})
    t, p = sched.get_next_message("PC-A")
    assert t == "file" and p == {"id": "f1"}


def test_path_style_cmd_and_batch_are_high_priority(sched):
    """/cmd and /batch join the high_prio queue, ahead of /file."""
    sched.enqueue("PC-A", "/file", {"id": "f1"})
    sched.enqueue("PC-A", "/batch", {"id": "b1"})
    sched.enqueue("PC-A", "/cmd", {"id": "c1"})
    t, _ = sched.get_next_message("PC-A")
    assert t == "batch"
    t, _ = sched.get_next_message("PC-A")
    assert t == "cmd"


def test_path_style_file_does_not_bypass_bulk_throttle(sched):
    """Enqueue /file first, then /cmd: the cmd still drains first and the
    second /file chunk is paced by the in-flight window."""
    sched.enqueue("PC-A", "/file", {"id": "f1"})
    sched.enqueue("PC-A", "/file", {"id": "f2"})
    sched.enqueue("PC-A", "/cmd", {"id": "c1"})

    assert sched.queue_depths("PC-A") == (1, 2, 0)  # 1 high, 2 bulk

    # high drains first even though /file was enqueued first
    assert sched.get_next_message("PC-A") == ("cmd", {"id": "c1"})
    # f1 becomes in flight; f2 is NOT sendable until f1's response
    assert sched.get_next_message("PC-A") == ("file", {"id": "f1"})
    assert sched.get_next_message("PC-A") is None


def test_path_style_response_releases_bulk_slot(sched):
    """on_response(pc, "/file") releases the bulk in-flight slot."""
    sched.enqueue("PC-A", "/file", {"id": "f1"})
    sched.enqueue("PC-A", "/file", {"id": "f2"})

    t, p = sched.get_next_message("PC-A")
    assert (t, p["id"]) == ("file", "f1")
    assert sched.get_next_message("PC-A") is None  # f2 blocked while f1 in flight

    sched.on_response("PC-A", "/file")
    assert sched.queue_depths("PC-A") == (0, 1, 0)
    t, p = sched.get_next_message("PC-A")
    assert (t, p["id"]) == ("file", "f2")


def test_normalize_edge_cases(sched):
    """'' stays unknown -> high_prio; non-string input passes through unchanged."""
    sched.enqueue("PC-A", "", {"id": "e1"})
    sched.enqueue("PC-A", None, {"id": "e2"})
    sched.enqueue("PC-A", "/file", {"id": "f1"})
    assert sched.queue_depths("PC-A") == (2, 1, 0)
    # both edge cases come back with their original type untouched
    t, _ = sched.get_next_message("PC-A")
    assert t == ""
    t, _ = sched.get_next_message("PC-A")
    assert t is None


def test_normalize_helper():
    from wss_scheduler import _normalize_msg_type
    assert _normalize_msg_type("/cmd") == "cmd"
    assert _normalize_msg_type("/batch") == "batch"
    assert _normalize_msg_type("/file") == "file"
    assert _normalize_msg_type("cmd") == "cmd"
    assert _normalize_msg_type("") == ""
    assert _normalize_msg_type(None) is None
    assert _normalize_msg_type(123) == 123
