"""Zero-dependency test runner for wss_scheduler (no pytest on this VM)."""
import asyncio
import sys
import traceback

sys.path.insert(0, ".")

from wss_scheduler import WSSScheduler, QueueFullError

passed = failed = 0


def check(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  PASS {name}")
    except Exception:
        failed += 1
        print(f"  FAIL {name}")
        traceback.print_exc()


def arun(coro):
    return asyncio.run(coro)


# ---- sync tests ----
def t_priority():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.enqueue("PC-A", "file", {"id": "f1"})
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    assert s.get_next_message("PC-A") == ("cmd", {"id": "c1"})
    assert s.get_next_message("PC-A") == ("file", {"id": "f1"})

def t_batch_high():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.enqueue("PC-A", "file", {"id": "f1"})
    s.enqueue("PC-A", "batch", {"id": "b1"})
    assert s.get_next_message("PC-A")[0] == "batch"

def t_unknown_high():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.enqueue("PC-A", "file", {"id": "f1"})
    s.enqueue("PC-A", "weird_new_op", {"id": "w1"})
    assert s.get_next_message("PC-A")[0] == "weird_new_op"

def t_in_flight():
    s = WSSScheduler(); s.register_pc("PC-A")
    for i in (1, 2, 3):
        s.enqueue("PC-A", "file", {"id": f"f{i}"})
    assert s.get_next_message("PC-A")[1] == {"id": "f1"}
    assert s.queue_depths("PC-A") == (0, 2, 1)
    assert s.get_next_message("PC-A") is None
    s.on_response("PC-A", "file")
    assert s.get_next_message("PC-A")[1] == {"id": "f2"}

def t_preempt():
    # /cmd preempts IMMEDIATELY even while a /file chunk is in flight:
    # high-prio messages are small control frames; sending one does not
    # bloat the kernel TCP buffer. Only bulk chunks are paced.
    s = WSSScheduler(); s.register_pc("PC-A")
    s.enqueue("PC-A", "file", {"id": "f1"})
    assert s.get_next_message("PC-A")[1] == {"id": "f1"}  # f1 in flight
    s.enqueue("PC-A", "file", {"id": "f2"})
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    s.enqueue("PC-A", "file", {"id": "f3"})
    # c1 jumps the queue instantly — zero wait behind f1's 64KB.
    assert s.get_next_message("PC-A") == ("cmd", {"id": "c1"})
    # f2 still blocked by in-flight window.
    assert s.get_next_message("PC-A") is None
    s.on_response("PC-A", "file")  # f1 done
    assert s.get_next_message("PC-A") == ("file", {"id": "f2"})
    s.on_response("PC-A", "file")
    assert s.get_next_message("PC-A") == ("file", {"id": "f3"})
    assert s.get_next_message("PC-A") is None

def t_high_no_limit():
    s = WSSScheduler(); s.register_pc("PC-A")
    for i in range(5):
        s.enqueue("PC-A", "cmd", {"id": f"c{i}"})
    for i in range(5):
        assert s.get_next_message("PC-A")[1] == {"id": f"c{i}"}
    assert s.get_next_message("PC-A") is None

def t_isolation():
    s = WSSScheduler()
    s.register_pc("PC-A"); s.register_pc("PC-B")
    s.enqueue("PC-A", "file", {"id": "a1"})
    s.enqueue("PC-B", "file", {"id": "b1"})
    s.enqueue("PC-A", "file", {"id": "a2"})
    assert s.get_next_message("PC-A")[1] == {"id": "a1"}
    assert s.get_next_message("PC-A") is None
    assert s.get_next_message("PC-B")[1] == {"id": "b1"}

def t_ghost_noop():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.on_response("PC-GHOST", "file")
    assert s.get_next_message("PC-GHOST") is None

def t_enqueue_ghost_raises():
    s = WSSScheduler(); s.register_pc("PC-A")
    try:
        s.enqueue("PC-GHOST", "cmd", {})
    except KeyError:
        return
    raise AssertionError("expected KeyError")

def t_unregister():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    s.enqueue("PC-A", "file", {"id": "f1"})
    assert s.unregister_pc("PC-A") == 2
    assert not s.is_registered("PC-A")
    assert s.unregister_pc("PC-A") == 0

def t_stats():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    s.enqueue("PC-A", "file", {"id": "f1"})
    s.get_next_message("PC-A")
    s.get_next_message("PC-A")
    st = s.stats("PC-A")
    assert st["sent_high"] == 1 and st["sent_bulk"] == 1
    assert st["bulk_in_flight"] == 1

# ---- S1. queue depth cap (SECURITY_V2.md 中危)：防内存耗尽 DoS ----
def t_queue_depth_high_limit():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.MAX_QUEUE_DEPTH = 3
    for i in range(3):
        s.enqueue("PC-A", "cmd", {"id": f"c{i}"})
    try:
        s.enqueue("PC-A", "cmd", {"id": "c3"})
        raise AssertionError("QueueFullError not raised")
    except QueueFullError as e:
        assert "PC-A" in str(e)

def t_queue_depth_bulk_limit():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.MAX_QUEUE_DEPTH = 2
    s.enqueue("PC-A", "file", {"id": "f1"})
    s.enqueue("PC-A", "file", {"id": "f2"})
    try:
        s.enqueue("PC-A", "file", {"id": "f3"})
        raise AssertionError("QueueFullError not raised for bulk")
    except QueueFullError:
        pass

def t_queue_depth_per_queue():
    # 上限是 per-queue：high 满了不影响 bulk
    s = WSSScheduler(); s.register_pc("PC-A")
    s.MAX_QUEUE_DEPTH = 2
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    s.enqueue("PC-A", "cmd", {"id": "c2"})
    s.enqueue("PC-A", "file", {"id": "f1"})  # bulk 队列独立，不抛
    assert s.queue_depths("PC-A") == (2, 1, 0)

def t_queue_depth_per_pc_isolation():
    s = WSSScheduler(); s.register_pc("PC-A"); s.register_pc("PC-B")
    s.MAX_QUEUE_DEPTH = 2
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    s.enqueue("PC-A", "cmd", {"id": "c2"})
    s.enqueue("PC-B", "cmd", {"id": "c1"})  # 另一台 PC 不受影响
    assert s.queue_depths("PC-B") == (1, 0, 0)

def t_queue_depth_recover_after_drain():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.MAX_QUEUE_DEPTH = 2
    s.enqueue("PC-A", "cmd", {"id": "c1"})
    s.enqueue("PC-A", "cmd", {"id": "c2"})
    s.get_next_message("PC-A")  # 取走一条
    s.enqueue("PC-A", "cmd", {"id": "c3"})  # 恢复可用
    assert s.queue_depths("PC-A")[0] == 2

def t_queue_depth_default_value():
    assert WSSScheduler.MAX_QUEUE_DEPTH == 10000

def t_queue_full_error_is_runtime_error():
    assert issubclass(QueueFullError, RuntimeError)

# ---- async tests ----
async def a_wait_msg():
    s = WSSScheduler(); s.register_pc("PC-A")
    async def prod():
        await asyncio.sleep(0.05)
        s.enqueue("PC-A", "cmd", {"id": "c1"})
    t = asyncio.create_task(prod())
    item = await asyncio.wait_for(s.wait_next("PC-A"), 2.0)
    await t
    assert item == ("cmd", {"id": "c1"})

async def a_wait_unreg():
    s = WSSScheduler(); s.register_pc("PC-A")
    async def unreg():
        await asyncio.sleep(0.05)
        s.unregister_pc("PC-A")
    t = asyncio.create_task(unreg())
    item = await asyncio.wait_for(s.wait_next("PC-A"), 2.0)
    await t
    assert item is None

async def a_wait_pacing():
    s = WSSScheduler(); s.register_pc("PC-A")
    s.enqueue("PC-A", "file", {"id": "f1"})
    s.enqueue("PC-A", "file", {"id": "f2"})
    first = await asyncio.wait_for(s.wait_next("PC-A"), 2.0)
    assert first[1] == {"id": "f1"}
    async def resp():
        await asyncio.sleep(0.05)
        s.on_response("PC-A", "file")
    t = asyncio.create_task(resp())
    second = await asyncio.wait_for(s.wait_next("PC-A"), 2.0)
    await t
    assert second[1] == {"id": "f2"}

for name, fn in [
    ("priority_cmd_before_file", t_priority),
    ("batch_is_high_priority", t_batch_high),
    ("unknown_type_defaults_high", t_unknown_high),
    ("bulk_in_flight_limit", t_in_flight),
    ("cmd_preempts_after_bulk_response", t_preempt),
    ("high_prio_no_in_flight_limit", t_high_no_limit),
    ("per_pc_isolation", t_isolation),
    ("ghost_response_noop", t_ghost_noop),
    ("enqueue_ghost_raises", t_enqueue_ghost_raises),
    ("unregister_drops_queues", t_unregister),
    ("stats", t_stats),
    ("queue_depth_high_limit", t_queue_depth_high_limit),
    ("queue_depth_bulk_limit", t_queue_depth_bulk_limit),
    ("queue_depth_per_queue", t_queue_depth_per_queue),
    ("queue_depth_per_pc_isolation", t_queue_depth_per_pc_isolation),
    ("queue_depth_recover_after_drain", t_queue_depth_recover_after_drain),
    ("queue_depth_default_value", t_queue_depth_default_value),
    ("queue_full_error_is_runtime_error", t_queue_full_error_is_runtime_error),
]:
    check(name, fn)

for name, coro in [
    ("wait_next_returns_message", a_wait_msg),
    ("wait_next_unregistered_none", a_wait_unreg),
    ("wait_next_bulk_pacing", a_wait_pacing),
]:
    check(name, lambda c=coro: arun(c()))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
