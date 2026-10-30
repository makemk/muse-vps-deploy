#!/usr/bin/env python3
"""test_pool.py: unit tests for mused/pool.py ConnectionPool.

Covers (per task spec):
  1. warmup: min_size connections pre-warmed in background at startup
  2. scale-up: pool grows on demand when idle is empty and total < max
  3. 45s idle discard: stale connections are closed, never reused
  4. exhausted pool: get_connection blocks up to timeout, then TimeoutError
  5. broken return: is_broken=True closes and drops the connection
  6. thread safety: concurrent get/return never exceeds max_size, no errors

No network. Uses FakeConn + injectable timeouts for speed.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mused"))

from pool import ConnectionPool  # noqa: E402


class FakeConn:
    _ids = 0
    _lock = threading.Lock()

    def __init__(self):
        with FakeConn._lock:
            FakeConn._ids += 1
            self.n = FakeConn._ids
        self.closed = False

    def close(self):
        self.closed = True

    def __repr__(self):
        return f"<FakeConn #{self.n} closed={self.closed}>"


def test_warmup():
    created = []

    def mk():
        c = FakeConn()
        created.append(c)
        return c

    pool = ConnectionPool(mk, min_size=4, max_size=16)
    pool.wait_warmup(timeout=10)
    st = pool.stats()
    assert st["idle"] == 4, st
    assert st["total"] == 4, st
    assert len(created) == 4
    pool.close_all()
    assert all(c.closed for c in created)
    print("PASS warmup")


def test_scale_up():
    created = []

    def mk():
        c = FakeConn()
        created.append(c)
        return c

    pool = ConnectionPool(mk, min_size=2, max_size=16)
    pool.wait_warmup(timeout=10)
    assert pool.stats()["total"] == 2
    pcs = [pool.get_connection() for _ in range(6)]
    assert pool.stats()["total"] == 6, pool.stats()
    assert len(created) == 6
    for pc in pcs:
        pool.return_connection(pc)
    assert pool.stats()["idle"] == 6
    pool.close_all()
    print("PASS scale_up")


def test_idle_discard_45s():
    created = []

    def mk():
        c = FakeConn()
        created.append(c)
        return c

    pool = ConnectionPool(mk, min_size=0, max_size=16, idle_timeout=45.0)
    pc1 = pool.get_connection()
    old = pc1.conn
    assert len(created) == 1
    pool.return_connection(pc1)
    # simulate 46s of idleness on the queued object
    pc1.last_used_at = time.monotonic() - 46.0
    pc2 = pool.get_connection()
    assert len(created) == 2, "stale conn must trigger a fresh connect"
    assert old.closed, "stale conn must be closed"
    assert pc2.conn is not old
    pool.return_connection(pc2)
    pool.close_all()
    print("PASS idle_discard_45s")


def test_idle_fresh_reused():
    """A recently-used connection must be reused, not discarded."""
    created = []

    def mk():
        c = FakeConn()
        created.append(c)
        return c

    pool = ConnectionPool(mk, min_size=0, max_size=16, idle_timeout=45.0)
    pc1 = pool.get_connection()
    pool.return_connection(pc1)
    pc2 = pool.get_connection()
    assert pc2.conn is pc1.conn, "fresh conn must be reused"
    assert len(created) == 1
    pool.return_connection(pc2)
    pool.close_all()
    print("PASS idle_fresh_reused")


def test_exhausted_timeout():
    def mk():
        return FakeConn()

    pool = ConnectionPool(mk, min_size=0, max_size=4, acquire_timeout=0.5)
    held = [pool.get_connection() for _ in range(4)]
    assert pool.stats()["total"] == 4
    t0 = time.monotonic()
    try:
        pool.get_connection(timeout=0.5)
    except TimeoutError:
        elapsed = time.monotonic() - t0
        assert 0.4 < elapsed < 2.0, f"waited {elapsed:.2f}s"
    else:
        raise AssertionError("expected TimeoutError on exhausted pool")
    # return one -> next acquire succeeds immediately
    pool.return_connection(held.pop())
    pc = pool.get_connection(timeout=2.0)
    assert pc is not None
    held.append(pc)
    for pc in held:
        pool.return_connection(pc)
    pool.close_all()
    print("PASS exhausted_timeout")


def test_broken_return():
    def mk():
        return FakeConn()

    pool = ConnectionPool(mk, min_size=1, max_size=16)
    pool.wait_warmup(timeout=10)
    pc = pool.get_connection()
    raw = pc.conn
    pool.return_connection(pc, is_broken=True)
    assert raw.closed, "broken conn must be closed"
    st = pool.stats()
    assert st["total"] == 0, st
    assert st["idle"] == 0, st
    pool.close_all()
    print("PASS broken_return")


def test_warmup_failure_tolerated():
    """If connect_fn fails during warmup, pool still works on demand."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise ConnectionError("boom")
        return FakeConn()

    pool = ConnectionPool(flaky, min_size=4, max_size=16)
    pool.wait_warmup(timeout=10)
    # 2 failed, 2 succeeded
    assert pool.stats()["total"] == 2, pool.stats()
    pc = pool.get_connection(timeout=5)
    assert pc is not None
    pool.return_connection(pc)
    pool.close_all()
    print("PASS warmup_failure_tolerated")


def test_thread_safety():
    def mk():
        return FakeConn()

    pool = ConnectionPool(mk, min_size=4, max_size=16)
    pool.wait_warmup(timeout=10)
    errors = []
    peak = [0]
    peak_lock = threading.Lock()
    in_use = [0]

    def worker():
        try:
            for _ in range(60):
                pc = pool.get_connection(timeout=10)
                with peak_lock:
                    in_use[0] += 1
                    peak[0] = max(peak[0], in_use[0])
                time.sleep(0.001)
                with peak_lock:
                    in_use[0] -= 1
                # randomly mark some broken
                pool.return_connection(pc, is_broken=(pc.conn.n % 37 == 0))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "worker thread hung"
    assert not errors, errors[:3]
    st = pool.stats()
    assert st["total"] <= 16, st
    assert peak[0] > 4, f"expected real concurrency, peak={peak[0]}"
    pool.close_all()
    print(f"PASS thread_safety (peak concurrent={peak[0]})")


def test_close_all_idempotent():
    pool = ConnectionPool(lambda: FakeConn(), min_size=2, max_size=8)
    pool.wait_warmup(timeout=10)
    pool.close_all()
    pool.close_all()  # must not raise
    try:
        pool.get_connection(timeout=0.5)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError after close_all")
    print("PASS close_all_idempotent")


if __name__ == "__main__":
    test_warmup()
    test_scale_up()
    test_idle_discard_45s()
    test_idle_fresh_reused()
    test_exhausted_timeout()
    test_broken_return()
    test_warmup_failure_tolerated()
    test_thread_safety()
    test_close_all_idempotent()
    print("ALL TESTS PASSED")
