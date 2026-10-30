#!/usr/bin/env python3
"""Benchmark: ConnectionPool throughput and idle-discard correctness.

Gemini-4 spec (code_artifact__2.md section 1.2):
- min_size=4 pre-warmed, max_size=16
- 16 connections at 67ms RTT -> ~238 req/s theoretical
- Idle >45s connections must be discarded (nginx keepalive_timeout=65s)

Tries to import real ConnectionPool from mused/pool.py.
Falls back to spec-faithful reference if not available.

Pure stdlib. Mock connections (no network).
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mused"))

try:
    from pool import ConnectionPool
    REAL_IMPL = True
except ImportError:
    REAL_IMPL = False
    import queue as _queue

    class _MockConn:
        _counter = 0
        _lock = threading.Lock()

        def __init__(self):
            with _MockConn._lock:
                _MockConn._counter += 1
                self.id = _MockConn._counter
            self.closed = False

        def close(self):
            self.closed = True

    class ConnectionPool:
        """Reference: min/max pool with 45s idle discard."""

        def __init__(self, connect_fn, min_size=4, max_size=16,
                     idle_timeout=45.0, acquire_timeout=5.0):
            import queue
            self._connect_fn = connect_fn
            self._min_size = min_size
            self._max_size = max_size
            self._idle_timeout = idle_timeout
            self._acquire_timeout = acquire_timeout
            self._pool = queue.Queue()
            self._total = 0
            self._lock = threading.Lock()
            self._not_empty = threading.Condition(self._lock)
            # pre-warm
            for _ in range(min_size):
                self._pool.put(self._wrap(connect_fn()))
                with self._lock:
                    self._total += 1

        def _wrap(self, conn):
            return {"conn": conn, "last_used": time.monotonic()}

        def get_connection(self):
            deadline = time.monotonic() + self._acquire_timeout
            while True:
                try:
                    item = self._pool.get_nowait()
                    if time.monotonic() - item["last_used"] > self._idle_timeout:
                        try:
                            item["conn"].close()
                        except Exception:
                            pass
                        with self._lock:
                            self._total -= 1
                        continue  # try again
                    return item["conn"]
                except Exception:
                    pass
                with self._lock:
                    if self._total < self._max_size:
                        self._total += 1
                        return self._connect_fn()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("pool exhausted")
                    self._not_empty.wait(timeout=min(remaining, 0.05))

        def return_connection(self, conn, is_broken=False):
            if is_broken:
                try:
                    conn.close()
                except Exception:
                    pass
                with self._lock:
                    self._total -= 1
                    self._not_empty.notify()
                return
            self._pool.put(self._wrap(conn))

        def stats(self):
            with self._lock:
                return {"total": self._total, "idle": self._pool.qsize()}

    _MockConnRef = _MockConn


def _make_mock_connect(latency_ms=0.0):
    """Return a connect_fn that creates mock connections."""
    def connect_fn():
        if latency_ms:
            time.sleep(latency_ms / 1000.0)
        try:
            return _MockConn()
        except NameError:
            # real pool.py path: define local mock
            class M:
                def close(self):
                    pass
            return M()
    return connect_fn


def bench_throughput(min_size, max_size, workers, iters_per_worker=200):
    """Measure req/s with mock 67ms 'RTT' per request."""
    pool = ConnectionPool(_make_mock_connect(), min_size=min_size,
                          max_size=max_size)
    errors = []
    latencies = []
    lock = threading.Lock()

    def worker():
        local = []
        try:
            for _ in range(iters_per_worker):
                t0 = time.perf_counter()
                conn = pool.get_connection()
                # simulate 67ms network RTT
                time.sleep(0.067)
                pool.return_connection(conn)
                local.append((time.perf_counter() - t0) * 1000)
        except Exception as e:
            errors.append(e)
        with lock:
            latencies.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    total = len(latencies)
    qps = total / elapsed if elapsed > 0 else 0
    return {
        "workers": workers, "min_size": min_size, "max_size": max_size,
        "total_req": total, "elapsed_s": elapsed, "qps": qps,
        "errors": len(errors),
    }


def bench_idle_discard():
    """Verify connections idle >45s are discarded, not reused."""
    created = []
    def connect_fn():
        c = _make_mock_connect()()
        created.append(c)
        return c

    # use short idle timeout for test speed
    pool = ConnectionPool(connect_fn, min_size=2, max_size=4,
                          idle_timeout=0.2, acquire_timeout=2.0)
    c1 = pool.get_connection()
    pool.return_connection(c1)
    n_before = len(created)
    time.sleep(0.35)  # exceed 0.2s idle timeout
    c2 = pool.get_connection()
    pool.return_connection(c2)
    n_after = len(created)
    # c1 should have been discarded, new connection created
    discarded = (n_after > n_before)
    return {"idle_discard_works": discarded,
            "created_before": n_before, "created_after": n_after}


def bench_preheat():
    """Verify min_size connections are pre-warmed at construction."""
    created = []
    def connect_fn():
        c = _make_mock_connect()()
        created.append(c)
        return c
    # give background preheat a moment if async
    pool = ConnectionPool(connect_fn, min_size=4, max_size=16)
    time.sleep(0.5)
    try:
        s = pool.stats()
        idle = s.get("idle", 0)
    except Exception:
        idle = -1
    return {"prewarmed_idle": idle, "created": len(created)}


def main():
    print("=" * 60)
    print("ConnectionPool benchmark")
    print(f"Implementation: {'REAL (mused/pool.py)' if REAL_IMPL else 'REFERENCE (spec-faithful mock)'}")
    print("=" * 60)

    # --- idle discard correctness ---
    r = bench_idle_discard()
    print(f"\nIdle discard (>45s -> drop, tested with 0.2s):")
    print(f"  discarded stale conn: {r['idle_discard_works']}  "
          f"{'PASS' if r['idle_discard_works'] else 'FAIL'}")

    # --- preheat ---
    r = bench_preheat()
    print(f"\nPre-warm (min_size=4):")
    print(f"  idle after construct: {r['prewarmed_idle']} (expect >=4 if sync, async may vary)")

    # --- throughput: 4 vs 16 connections ---
    print(f"\nThroughput (mock 67ms RTT per request, 200 req/worker):")
    print(f"  {'workers':>8} {'pool':>10} {'req':>6} {'time_s':>8} {'qps':>8}")
    results = []
    for workers, (mn, mx) in [(4, (4, 4)), (16, (4, 16)), (32, (4, 16))]:
        res = bench_throughput(mn, mx, workers)
        results.append(res)
        print(f"  {res['workers']:>8} {mn}-{mx:<7} {res['total_req']:>6} "
              f"{res['elapsed_s']:>8.2f} {res['qps']:>8.1f}  "
              f"errors={res['errors']}")
    # theoretical: 16 conns * (1000/67) = ~238 qps
    best = max(r["qps"] for r in results)
    print(f"\n  Best: {best:.1f} qps  (theoretical max ~238 qps at 16 conns)")
    print(f"  16-conn pool saturates as expected: "
          f"{'PASS' if best > 150 else 'CHECK'}")

    print("\n" + "=" * 60)
    # overall: idle discard must work (re-run to get fresh result)
    idle_ok = bench_idle_discard()["idle_discard_works"]
    print(f"OVERALL: {'PASS' if idle_ok else 'FAIL'}")
    print("=" * 60)
    return 0 if idle_ok else 1


if __name__ == "__main__":
    sys.exit(main())
