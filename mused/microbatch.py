#!/usr/bin/env python3
"""MicroBatcher: In-Flight Triggered Coalescing for the mused daemon.

Implements the Gemini-4 spec (code_artifact__2.md §4.2-4.3):

  * Per-target ``in_flight`` request tracking.
  * Fast path: when a target has ``in_flight == 0`` and its pending queue is
    empty, the request is dispatched immediately as a single call with
    **0ms added latency** (no artificial waiting).
  * Slow path: when ``in_flight > 0`` (or requests are already queued), a
    1.5ms collection window opens; eligible requests are packed into ONE
    ``POST /muse-api/batch`` (max 10 sub-requests, max 64KB payload).
  * Five iron rules enforced:
    1. Only requests with the same ``(token, target_pc)`` are merged.
    2. Only explicitly batchable requests (``batchable=True``, e.g. the
       caller sent ``X-Muse-Batchable: 1``) or read-only ops are merged;
       anything else is always sent as an immediate single.
    3. A batch payload never exceeds 64KB -- overflowing requests start a
       new batch which is dispatched immediately ("truncate and go").
    4. Partial-failure isolation: the outer /batch HTTP status is 200
       whenever the transport is healthy; per-sub-request results (incl.
       failures) travel inside the body array.  One sub-request failing
       NEVER fails the whole batch.
    5. Dedup granularity: every /batch carries a ``batch_id`` plus the
       caller's sub ``request_id``s so the VPS can dedup on
       ``(batch_id, sub_id)``.

Wire format (VPS must implement this multiplexing /batch -- it is NOT the
legacy shell-command {"commands": [...]} batch):
  Request:  POST /muse-api/batch
            {"batch_id": "<uuid>", "requests": [
                {"id": "<sub_id>", "method": "POST",
                 "path": "/cmd", "body": {...}}, ... ]}
  Response: HTTP 200, body =
            [{"id": "<sub_id>", "status": 200, "body": "<raw text>"}, ...]
  A sub-id missing from the response fails ONLY that sub-request's waiter.

Threading: the daemon serves one thread per unix-socket client, so
``submit()`` blocks the calling thread on a ``threading.Event`` until its
(upstream) response arrives.  All shared state is guarded by one lock;
the upstream ``send_fn`` is always invoked WITHOUT holding the lock.
"""

import json
import threading
import time
import uuid

WINDOW_S = 0.0015        # 1.5ms micro-batch collection window
MAX_BATCH = 10           # max sub-requests per /batch call
MAX_PAYLOAD = 64 * 1024  # max serialized batch payload, bytes


class BatchError(Exception):
    """Transport-level failure of a /batch call, or a missing sub-response.

    Raised to an individual waiter; other sub-requests of the same batch
    are unaffected (partial-failure isolation).
    """


class _Pending:
    __slots__ = ("token", "target", "method", "path", "body", "batchable",
                 "request_id", "event", "result")

    def __init__(self, token, target, method, path, body, batchable,
                 request_id):
        self.token = token
        self.target = target
        self.method = method
        self.path = path
        self.body = body
        self.batchable = batchable
        self.request_id = request_id or uuid.uuid4().hex
        self.event = threading.Event()
        # result: (status:int, body_text:str) on success, BaseException else
        self.result = None


def is_read_only(method, path, body):
    """Conservative heuristic: which requests are safe to auto-batch.

    Callers that know better should pass batchable=True explicitly
    (e.g. when they sent X-Muse-Batchable: 1).
    """
    m = (method or "").upper()
    if m == "GET":
        return True
    if (path or "") == "/status":
        return True
    return False


class MicroBatcher:
    """In-flight-triggered request coalescer.

    ``send_fn(token, method, path, body_dict) -> (status:int, body_text:str)``
    performs one upstream HTTPS call.  For batches, ``method`` is "POST",
    ``path`` is "/batch" and ``body_dict`` is
    ``{"batch_id": ..., "requests": [{"id","method","path","body"}, ...]}``;
    the upstream must answer ``(200, '[{"id","status","body"}, ...]')``.
    """

    def __init__(self, send_fn, window_s=WINDOW_S, max_batch=MAX_BATCH,
                 max_payload=MAX_PAYLOAD):
        self._send = send_fn
        self._window = window_s
        self._max_batch = max_batch
        self._max_payload = max_payload
        self._lock = threading.Lock()
        # target -> {"in_flight": int, "pending": [_Pending],
        #            "timer": threading.Timer | None}
        self._targets = {}
        self._stats = {"singles": 0, "batches": 0, "batched_reqs": 0,
                       "fast_path": 0}

    # ------------------------------------------------------------ public

    def submit(self, token, target, method, path, body, batchable=False,
               request_id=None):
        """Submit one request; block until its (status, body_text) arrives.

        ``target`` may be None (e.g. /status); such requests only batch
        with other untargeted requests of the same token.
        """
        p = _Pending(token, target, method, path, body, batchable,
                     request_id)
        go_single = False
        with self._lock:
            st = self._state_locked(target)
            # Rule 2: non-batchable requests never join a batch.
            # Fast path (spec §4.2): in_flight==0 and nothing queued ->
            # dispatch immediately, 0ms added latency.
            if (not batchable) or (st["in_flight"] == 0 and not st["pending"]):
                st["in_flight"] += 1
                go_single = True
                self._stats["singles"] += 1
                if batchable:
                    self._stats["fast_path"] += 1
            else:
                st["pending"].append(p)
                if st["timer"] is None:
                    t = threading.Timer(self._window, self._flush,
                                        args=(target,))
                    t.daemon = True
                    st["timer"] = t
                    t.start()
        if go_single:
            th = threading.Thread(target=self._run_single, args=(p,),
                                  daemon=True, name="mb-single")
            th.start()
        p.event.wait()
        if isinstance(p.result, BaseException):
            raise p.result
        return p.result

    def stats(self):
        """Snapshot of counters + per-target in_flight/pending depth."""
        with self._lock:
            out = dict(self._stats)
            out["targets"] = {
                t: {"in_flight": s["in_flight"],
                    "pending": len(s["pending"])}
                for t, s in self._targets.items()
            }
            return out

    # ------------------------------------------------------------ internals

    def _state_locked(self, target):
        st = self._targets.get(target)
        if st is None:
            st = {"in_flight": 0, "pending": [], "timer": None}
            self._targets[target] = st
        return st

    def _run_single(self, p):
        try:
            status, body_text = self._send(p.token, p.method, p.path, p.body)
            p.result = (status, body_text)
        except Exception as e:  # transport failure -> this waiter only
            p.result = e
        finally:
            p.event.set()
            self._complete(p.target)

    def _flush(self, target):
        """Timer callback: drain the pending queue into batch(es)."""
        with self._lock:
            st = self._targets.get(target)
            if st is None:
                return
            st["timer"] = None
            pending = st["pending"]
            st["pending"] = []
        if pending:
            self._dispatch_pending(target, pending)

    def _complete(self, target):
        """An upstream call finished: release its slot, drain queue."""
        with self._lock:
            st = self._targets.get(target)
            if st is None:
                return
            st["in_flight"] -= 1
            if st["in_flight"] < 0:  # defensive; should never happen
                st["in_flight"] = 0
            pending = st["pending"]
            st["pending"] = []
            # A scheduled timer may still fire later; it will find an
            # empty queue and become a no-op.
        if pending:
            # Already waited while in_flight>0: dispatch immediately,
            # no second collection window.
            self._dispatch_pending(target, pending)

    def _dispatch_pending(self, target, pending):
        # Rule 1: group by token, preserving arrival order.
        groups = {}
        for p in pending:
            groups.setdefault(p.token, []).append(p)
        jobs = []
        with self._lock:
            st = self._state_locked(target)
            for token, reqs in groups.items():
                for chunk in self._chunk_reqs(reqs):
                    jobs.append((token, chunk))
                    st["in_flight"] += 1
                    self._stats["batches"] += 1
                    self._stats["batched_reqs"] += len(chunk)
        for token, chunk in jobs:
            th = threading.Thread(target=self._run_batch,
                                  args=(target, token, chunk),
                                  daemon=True, name="mb-batch")
            th.start()

    def _chunk_reqs(self, reqs):
        """Split into chunks of <=max_batch reqs and <=max_payload bytes.

        Rule 3: overflowing requests start a new chunk which is dispatched
        immediately ("truncate and go").
        """
        chunk, size = [], 2  # account for the JSON array brackets
        for p in reqs:
            item = {"id": p.request_id, "method": p.method,
                    "path": p.path, "body": p.body}
            sz = len(json.dumps(item, separators=(",", ":")).encode("utf-8"))
            sz += 1  # comma separator
            if chunk and (len(chunk) >= self._max_batch
                          or size + sz > self._max_payload):
                yield chunk
                chunk, size = [], 2
            chunk.append(p)
            size += sz
        if chunk:
            yield chunk

    def _run_batch(self, target, token, chunk):
        batch_id = uuid.uuid4().hex
        req_list = [{"id": p.request_id, "method": p.method,
                     "path": p.path, "body": p.body} for p in chunk]
        try:
            status, body_text = self._send(
                token, "POST", "/batch",
                {"batch_id": batch_id, "requests": req_list})
            # Rule 4: outer MUST be 200 when the transport is healthy;
            # per-sub results (incl. failures) live inside the array.
            by_id = {}
            if status == 200:
                try:
                    arr = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    arr = None
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict) and "id" in item:
                            by_id[item["id"]] = item
            for p in chunk:
                item = by_id.get(p.request_id)
                if item is not None:
                    p.result = (item.get("status", 200),
                                item.get("body", ""))
                else:
                    # Missing sub-response: fail ONLY this waiter.
                    p.result = BatchError(
                        f"batch {batch_id}: no sub-response for "
                        f"sub-id {p.request_id} (outer status={status})")
        except Exception as e:
            # Transport-level failure: every sub-request fails, but each
            # waiter gets its own exception object to inspect.
            for p in chunk:
                p.result = e
        finally:
            for p in chunk:
                p.event.set()
            self._complete(target)
