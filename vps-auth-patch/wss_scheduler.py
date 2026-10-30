"""
wss_scheduler.py — VPS-side WSS send scheduler with dual priority queues.

Implements Gemini-4 spec §4.2 (improved a+c) for Head-of-Line blocking mitigation:

- Each online PC gets two in-memory queues:
    - ``high_prio`` — ``/cmd`` and ``/batch`` (latency-sensitive control)
    - ``bulk``     — ``/file`` (throughput-oriented, pre-chunked to ≤64KB
      by the caller/Daemon; this module does NOT chunk, only schedules)
- In-flight pacing: at most **1** ``/file`` chunk may be IN_FLIGHT per PC
  (written to the WSS socket, awaiting the PC's response). This prevents a
  large file from filling the kernel TCP send buffer and blocking subsequent
  ``/cmd`` for seconds (RFC 6455 §5.4: data frames cannot interleave).
- Drain order: after a ``/file`` chunk's response arrives, the sender first
  drains ``high_prio`` completely; only when it is empty is the next ``/file``
  chunk sent.

Integration with muse-bridge.py (pull interface)::

    scheduler = WSSScheduler()

    # on PC WSS connect:
    scheduler.register_pc(pc_id)
    # on PC WSS disconnect:
    scheduler.unregister_pc(pc_id)

    # replacing:  await client.send_text(json.dumps(req))
    # with:
    scheduler.enqueue(pc_id, msg_type, req)   # msg_type: "cmd"|"batch"|"file";
                                                  # path-style "/cmd"|"/batch"|"/file"
                                                  # are normalized to the bare names
    # ... in the per-PC sender task:
    while True:
        item = await scheduler.wait_next(pc_id)   # blocks until sendable
        await client.send_text(json.dumps(item.payload))

    # on PC response frame for request_id (match by stored msg_type):
    scheduler.on_response(pc_id, msg_type)   # path-style "/file" normalized too

All methods are plain (non-async) except ``wait_next``; the scheduler assumes
a single asyncio event loop (no threads), so no locks are needed.

Only the Python standard library is used.
"""

from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

# Message types routed to the high-priority queue.
HIGH_PRIO_TYPES = frozenset({"cmd", "batch"})
# Message types routed to the bulk queue.
BULK_TYPES = frozenset({"file"})

# Unknown types default to high priority: control-plane messages must never
# be stuck behind bulk traffic.
_DEFAULT_QUEUE = "high"


def _normalize_msg_type(msg_type):
    """Map path-style ``'/cmd'`` -> ``'cmd'`` so HTTP routing layers can pass
    request paths straight through (P0-1: otherwise a path-style ``'/file'``
    would be treated as unknown, land in high_prio, and silently bypass the
    bulk in-flight throttle).

    Non-string input passes through unchanged (still lands in high_prio
    fail-safe); ``""`` stays unknown -> high_prio (unchanged behavior).
    """
    if isinstance(msg_type, str):
        return msg_type.lstrip("/")
    return msg_type


class QueueFullError(RuntimeError):
    """Raised by WSSScheduler.enqueue when a per-PC queue reaches
    MAX_QUEUE_DEPTH. Callers should translate this to HTTP 503
    (safe to retry with backoff), never 500."""


@dataclass
class _QueuedMessage:
    msg_type: str
    payload: object  # typically the dict that will be json.dumps()'d


@dataclass
class _PCChannel:
    pc_id: str
    high_prio: Deque[_QueuedMessage] = field(default_factory=collections.deque)
    bulk: Deque[_QueuedMessage] = field(default_factory=collections.deque)
    bulk_in_flight: int = 0
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    # Counters for monitoring/debugging.
    sent_high: int = 0
    sent_bulk: int = 0
    dropped_on_unregister: int = 0


class WSSScheduler:
    """Dual-priority WSS send scheduler, one channel per online PC."""

    # Hard cap: at most this many /file chunks IN_FLIGHT per PC.
    MAX_BULK_IN_FLIGHT = 1

    # Hard cap: at most this many queued (unsent) messages per queue,
    # per PC (DoS guard, audit S1). enqueue() raises QueueFullError beyond
    # this; callers answer 503 with backoff.
    MAX_QUEUE_DEPTH = 10000

    def __init__(self) -> None:
        self._channels: Dict[str, _PCChannel] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def register_pc(self, pc_id: str) -> None:
        """Create (or reset) the channel for a newly-online PC."""
        self._channels[pc_id] = _PCChannel(pc_id=pc_id)

    def unregister_pc(self, pc_id: str) -> int:
        """Drop the channel for a disconnected PC.

        Returns the number of queued-but-unsent messages discarded.
        In-flight bulk count is forgotten (the socket is gone).
        """
        ch = self._channels.pop(pc_id, None)
        if ch is None:
            return 0
        dropped = len(ch.high_prio) + len(ch.bulk)
        ch.dropped_on_unregister = dropped
        ch.wakeup.set()  # release any waiter so it can observe unregistration
        return dropped

    def is_registered(self, pc_id: str) -> bool:
        return pc_id in self._channels

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------
    def enqueue(self, pc_id: str, msg_type: str, payload: object) -> None:
        """Route a message into the PC's high-prio or bulk queue.

        ``msg_type`` is normalized first: path-style values (``"/cmd"``,
        ``"/batch"``, ``"/file"``) map to their bare names, so an HTTP routing
        layer can pass request paths straight through without bypassing the
        bulk in-flight throttle.

        Unknown ``msg_type`` values go to high-prio (fail-safe: never let an
        unrecognized control message sit behind bulk traffic).

        :raises KeyError: PC not registered.
        :raises QueueFullError: the target queue already holds
            MAX_QUEUE_DEPTH messages (DoS guard, audit S1).
        """
        ch = self._channels.get(pc_id)
        if ch is None:
            raise KeyError(f"PC not registered: {pc_id!r}")
        msg_type = _normalize_msg_type(msg_type)
        if msg_type in BULK_TYPES:
            target = ch.bulk
        else:  # HIGH_PRIO_TYPES or unknown -> high
            target = ch.high_prio
        if len(target) >= self.MAX_QUEUE_DEPTH:
            raise QueueFullError(
                f"queue depth exceeded for {pc_id!r} "
                f"({len(target)} >= {self.MAX_QUEUE_DEPTH})"
            )
        target.append(_QueuedMessage(msg_type=msg_type, payload=payload))
        ch.wakeup.set()

    # ------------------------------------------------------------------
    # pull interface
    # ------------------------------------------------------------------
    def get_next_message(self, pc_id: str) -> Optional[Tuple[str, object]]:
        """Return the next (msg_type, payload) the sender may transmit.

        Ordering rules (Gemini-4 §4.2):
          1. ``high_prio`` always drains first — a queued ``/cmd`` never
             waits behind a queued ``/file``.
          2. A ``bulk`` message is returned only when no bulk chunk is
             currently IN_FLIGHT for this PC (``MAX_BULK_IN_FLIGHT == 1``).
             Returning it marks it IN_FLIGHT.
          3. Returns ``None`` when nothing is currently sendable
             (use :meth:`wait_next` to block instead of busy-polling).
        """
        ch = self._channels.get(pc_id)
        if ch is None:
            return None
        if ch.high_prio:
            msg = ch.high_prio.popleft()
            ch.sent_high += 1
            if not ch.high_prio and not ch.bulk:
                ch.wakeup.clear()
            return (msg.msg_type, msg.payload)
        if ch.bulk and ch.bulk_in_flight < self.MAX_BULK_IN_FLIGHT:
            msg = ch.bulk.popleft()
            ch.bulk_in_flight += 1
            ch.sent_bulk += 1
            if not ch.bulk:
                ch.wakeup.clear()
            return (msg.msg_type, msg.payload)
        return None

    async def wait_next(self, pc_id: str) -> Optional[Tuple[str, object]]:
        """Block until a message is sendable for ``pc_id``, then return it.

        Returns ``None`` if the PC was unregistered while waiting (sender
        task should exit its loop).
        """
        while True:
            item = self.get_next_message(pc_id)
            if item is not None:
                return item
            ch = self._channels.get(pc_id)
            if ch is None:
                return None
            ch.wakeup.clear()
            # Re-check after clearing to avoid lost-wakeup race:
            # enqueue() sets the event *after* appending, so an enqueue
            # that landed between get_next_message() and clear() will have
            # its message still in the deque; check once more.
            item = self.get_next_message(pc_id)
            if item is not None:
                return item
            if pc_id not in self._channels:
                return None
            await ch.wakeup.wait()

    # ------------------------------------------------------------------
    # response handling
    # ------------------------------------------------------------------
    def on_response(self, pc_id: str, msg_type: str) -> None:
        """Record that the PC answered a message of ``msg_type``.

        ``msg_type`` is normalized first (``"/file"`` -> ``"file"``), so a
        response arriving with a path-style type still releases the bulk
        IN_FLIGHT slot.

        For bulk messages this releases the IN_FLIGHT slot so the next
        ``/file`` chunk (after draining any backlogged ``high_prio``) may
        be sent. For high-prio messages it is a no-op besides waking the
        sender (a no-op wake is harmless).
        """
        ch = self._channels.get(pc_id)
        if ch is None:
            return
        msg_type = _normalize_msg_type(msg_type)
        if msg_type in BULK_TYPES and ch.bulk_in_flight > 0:
            ch.bulk_in_flight -= 1
        ch.wakeup.set()

    # ------------------------------------------------------------------
    # introspection (monitoring / tests)
    # ------------------------------------------------------------------
    def queue_depths(self, pc_id: str) -> Optional[Tuple[int, int, int]]:
        """Return (high_prio_len, bulk_len, bulk_in_flight) for a PC."""
        ch = self._channels.get(pc_id)
        if ch is None:
            return None
        return (len(ch.high_prio), len(ch.bulk), ch.bulk_in_flight)

    def stats(self, pc_id: str) -> Optional[dict]:
        ch = self._channels.get(pc_id)
        if ch is None:
            return None
        return {
            "pc_id": pc_id,
            "high_queued": len(ch.high_prio),
            "bulk_queued": len(ch.bulk),
            "bulk_in_flight": ch.bulk_in_flight,
            "sent_high": ch.sent_high,
            "sent_bulk": ch.sent_bulk,
        }
