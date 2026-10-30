#!/usr/bin/env python3
"""ConnectionPool: thread-safe HTTP/1.1 keep-alive connection pool (stdlib only).

Per Gemini-4 review spec:
- min_size connections pre-warmed at startup in a background thread
  (each: TCP CONNECT + TLS handshake + one verification request).
- max_size caps total connections; callers block up to acquire_timeout
  (default 5s) when the pool is exhausted.
- Idle connections older than idle_timeout (default 45s) are discarded
  instead of reused -- nginx keepalive_timeout is 65s, so reusing a
  connection that nginx is about to FIN would race into
  RemoteDisconnected / BrokenPipeError.
- Pure stdlib: queue.Queue + threading. No third-party deps.

The pool is transport-agnostic: callers supply a `connect_fn()` that
returns a *connected* connection object (must have .close()).
"""
import logging
import queue
import threading
import time

log = logging.getLogger("mused.pool")

IDLE_TIMEOUT = 45.0     # discard connections idle longer than this
ACQUIRE_TIMEOUT = 5.0   # max seconds to wait when pool is exhausted


class PooledConnection:
    """Thin wrapper carrying a last-used timestamp (monotonic clock)."""
    __slots__ = ("conn", "last_used_at", "created_at")

    def __init__(self, conn):
        self.conn = conn
        now = time.monotonic()
        self.last_used_at = now
        self.created_at = now


class ConnectionPool:
    def __init__(self, connect_fn, min_size=4, max_size=16,
                 idle_timeout=IDLE_TIMEOUT,
                 acquire_timeout=ACQUIRE_TIMEOUT):
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("require 0 <= min_size <= max_size, max_size >= 1")
        self._connect_fn = connect_fn
        self._min_size = min_size
        self._max_size = max_size
        self._idle_timeout = idle_timeout
        self._acquire_timeout = acquire_timeout
        self._idle = queue.Queue()
        self._total = 0
        self._total_lock = threading.Lock()
        self._closed = False
        self._warmup_thread = threading.Thread(
            target=self._warmup, daemon=True, name="pool-warmup")
        self._warmup_thread.start()

    # ---------------------------------------------------------- internals

    def _discard(self, pc):
        """Close a connection and decrement the total. Thread-safe."""
        try:
            pc.conn.close()
        except Exception:
            pass
        with self._total_lock:
            self._total -= 1

    def _is_stale(self, pc):
        return (time.monotonic() - pc.last_used_at) > self._idle_timeout

    def _warmup(self):
        for _ in range(self._min_size):
            if self._closed:
                break
            try:
                conn = self._connect_fn()
            except Exception as e:
                log.warning("pool warmup failed: %s", e)
                continue
            with self._total_lock:
                self._total += 1
            pc = PooledConnection(conn)
            pc.last_used_at = time.monotonic()
            self._idle.put(pc)
        log.info("pool warmup done (idle=%d total=%d)",
                 self._idle.qsize(), self._total)

    # ---------------------------------------------------------- public API

    def wait_warmup(self, timeout=30):
        """Block until the background pre-warm finishes (for tests/startup)."""
        self._warmup_thread.join(timeout=timeout)

    def get_connection(self, timeout=None):
        """Acquire a connection.

        Returns a PooledConnection. Raises TimeoutError if no connection
        becomes available within `timeout` seconds (default ACQUIRE_TIMEOUT).
        Raises RuntimeError if the pool is closed.
        """
        if timeout is None:
            timeout = self._acquire_timeout
        if self._closed:
            raise RuntimeError("connection pool is closed")
        deadline = time.monotonic() + timeout
        while True:
            # 1. fast path: idle queue
            try:
                pc = self._idle.get_nowait()
            except queue.Empty:
                pc = None
            if pc is not None:
                if self._is_stale(pc):
                    log.debug("discarding stale idle connection")
                    self._discard(pc)
                    continue
                return pc
            # 2. create new (check-and-increment under lock: no overshoot)
            with self._total_lock:
                if self._total < self._max_size and not self._closed:
                    self._total += 1
                    creating = True
                else:
                    creating = False
            if creating:
                try:
                    conn = self._connect_fn()
                except Exception:
                    with self._total_lock:
                        self._total -= 1
                    raise
                return PooledConnection(conn)
            # 3. at cap: wait for a return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no pooled connection available after {timeout}s "
                    f"(total={self._total} max={self._max_size})")
            try:
                pc = self._idle.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(
                    f"no pooled connection available after {timeout}s "
                    f"(total={self._total} max={self._max_size})")
            if self._is_stale(pc):
                log.debug("discarding stale idle connection")
                self._discard(pc)
                continue
            return pc

    def return_connection(self, pc, is_broken=False):
        """Return a connection to the pool.

        If is_broken (or the pool is closed), the connection is closed and
        dropped instead of being reused. Always safe to call exactly once
        per get_connection().
        """
        if self._closed or is_broken:
            self._discard(pc)
            return
        pc.last_used_at = time.monotonic()
        self._idle.put(pc)

    def close_all(self):
        """Shut down: close every idle connection. In-use ones are dropped
        on return."""
        self._closed = True
        while True:
            try:
                pc = self._idle.get_nowait()
            except queue.Empty:
                break
            self._discard(pc)

    def stats(self):
        with self._total_lock:
            total = self._total
        return {
            "idle": self._idle.qsize(),
            "total": total,
            "min_size": self._min_size,
            "max_size": self._max_size,
        }
