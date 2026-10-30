"""VPS 8471 request_id dedup state machine.

Implements the two-phase dedup design from the architecture review
(Gemini 4 round-1 Q4 + round-2 section 5):

  VM daemon generates a UUIDv4 ``request_id`` per call. The 8471 service keeps
  one ``RequestRecord`` per request_id with a two-phase state machine:

    IN_FLIGHT  ->  COMPLETED   (PC answered; result cached 300s from completion)
    IN_FLIGHT  ->  UNCERTAIN   (PC WSS dropped mid-flight; at-most-once: never
                                 re-execute, answer 504 EXECUTION_UNCERTAIN)

Reliability contract: command execution is at-most-once; result *retrieval*
is idempotent at-least-once via the COMPLETED cache.

Memory bounds (IN_FLIGHT records are NEVER evicted by TTL/LRU, but are
capped in count by MAX_IN_FLIGHT_TOTAL):
  * CACHE_TTL_SECONDS      = 300   (from completed_at, i.e. completion time)
  * MAX_CACHE_ENTRIES      = 5000  (LRU over settled records, oldest first)
  * MAX_SINGLE_PAYLOAD_BYTES = 256KB (bodies larger than this: status cached,
                                      body dropped)
  * MAX_TOTAL_CACHE_BYTES  = 64MB  (hard cap, evict oldest settled first)
  * MAX_IN_FLIGHT_TOTAL    = 10000 (new IN_FLIGHT beyond this -> OverloadedError)

Concurrency model: single asyncio event loop. ``get_or_create`` /
``complete`` / ``mark_uncertain`` are synchronous and contain no ``await``,
so each is atomic with respect to the event loop. All calls must originate
from the same loop thread. Waiting for an in-flight request uses
``asyncio.Event`` via ``wait_for``.

Integration pattern for the /cmd handler (8471 service)::

    from dedup import DedupManager, IN_FLIGHT, COMPLETED, UNCERTAIN, OverloadedError
    import uuid

    dedup = DedupManager()

    async def on_cmd(request_id, controller, target_pc, dispatch_to_pc):
        # request_id: UUIDv4 from the VM daemon; generate one for legacy
        # callers that do not send it (no dedup benefit, no breakage).
        request_id = request_id or str(uuid.uuid4())

        try:
            is_new, rec = dedup.get_or_create(request_id, controller, target_pc)
        except OverloadedError:
            # IN_FLIGHT cap (MAX_IN_FLIGHT_TOTAL) reached: shed load with
            # 503 (safe to retry), never 500.
            return 503, b'{"error":"SERVER_OVERLOADED"}'
        if not is_new:
            if rec.status == COMPLETED:
                return rec.http_status, rec.response_body   # idempotent replay
            if rec.status == UNCERTAIN:
                return 504, b'{"error":"EXECUTION_UNCERTAIN"}'
            # IN_FLIGHT: attach to the in-flight execution, do NOT re-dispatch.
            try:
                rec = await dedup.wait_for(request_id, timeout=300)
            except asyncio.TimeoutError:
                return 504, b'{"error":"EXECUTION_UNCERTAIN"}'
            if rec.status == COMPLETED:
                return rec.http_status, rec.response_body
            return 504, b'{"error":"EXECUTION_UNCERTAIN"}'

        # We own this execution.
        try:
            http_status, body = await dispatch_to_pc(request_id)
        except Exception:
            # May or may not have reached the PC: at-most-once says do NOT
            # retry with the same request_id; report uncertainty instead.
            dedup.mark_uncertain(request_id)
            return 504, b'{"error":"EXECUTION_UNCERTAIN"}'
        dedup.complete(request_id, http_status, body)
        return http_status, body

    # When a PC's WSS connection drops, poison its in-flight requests:
    #   dedup.mark_uncertain_for_target("PC-A")

Multiplex batch usage (VPS /muse-api/batch, from mused/microbatch.py)::

    from dedup import DedupManager, batch_sub_key
    import batch_multiplex

    # One batch fans out to N independent sub-executions, each deduped on
    # the composite key (batch_id, sub_id) -- NOT on the sub_id alone:
    #
    #   key = batch_sub_key(batch_id, sub_id)   # "batch:<bid>:<sid>"
    #
    # A retry of the same sub-request inside the SAME batch_id replays /
    # attaches to the existing record (no double execution); the same
    # sub_id arriving under a DIFFERENT batch_id is a brand-new execution.
    # The actual fan-out lives in batch_multiplex.handle_batch_multiplex;
    # this module only supplies the composite-key helper.

Pure standard library: asyncio, time, uuid, dataclasses, collections.
"""

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

__all__ = [
    "IN_FLIGHT",
    "COMPLETED",
    "UNCERTAIN",
    "CACHE_TTL_SECONDS",
    "MAX_CACHE_ENTRIES",
    "MAX_SINGLE_PAYLOAD_BYTES",
    "MAX_TOTAL_CACHE_BYTES",
    "MAX_IN_FLIGHT_TOTAL",
    "OverloadedError",
    "RequestRecord",
    "DedupManager",
    "new_request_id",
    "batch_sub_key",
]

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
IN_FLIGHT = "IN_FLIGHT"
COMPLETED = "COMPLETED"
UNCERTAIN = "UNCERTAIN"

# ---------------------------------------------------------------------------
# Memory bounds (Gemini 4 round-2 section 5.2)
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 300
MAX_CACHE_ENTRIES = 5000
MAX_SINGLE_PAYLOAD_BYTES = 256 * 1024      # 256 KB
MAX_TOTAL_CACHE_BYTES = 64 * 1024 * 1024   # 64 MB

#: Hard cap on concurrent IN_FLIGHT records (DoS guard, audit D1).
#: Each IN_FLIGHT holds an asyncio.Event + metadata; without a cap an
#: attacker with a valid token could exhaust VPS memory with requests
#: that never complete. When exceeded, get_or_create() raises
#: OverloadedError and the /cmd handler must answer 503 (retryable),
#: never 500.
MAX_IN_FLIGHT_TOTAL = 10000


class OverloadedError(RuntimeError):
    """Raised by DedupManager.get_or_create when the IN_FLIGHT cap
    (MAX_IN_FLIGHT_TOTAL) is reached. Callers should translate this to
    HTTP 503 Service Unavailable (safe to retry later)."""


def new_request_id() -> str:
    """Generate a UUIDv4 request_id (VM daemon side, or server-side fallback
    for legacy callers that do not send one)."""
    return str(uuid.uuid4())


def batch_sub_key(batch_id: str, sub_id: str) -> str:
    """Composite dedup key for one sub-request of a multiplex batch.

    Gives (batch_id, sub_id) dedup granularity (Gemini round-2 §4.3 iron
    rule 5): the same sub-request retried inside the same batch_id dedups;
    a retry in a NEW batch_id is a new execution. Add to __all__.
    """
    return f"batch:{batch_id}:{sub_id}"


@dataclass
class RequestRecord:
    request_id: str
    controller: str
    target_pc: str
    status: str  # "IN_FLIGHT" | "COMPLETED" | "UNCERTAIN"
    created_at: float
    completed_at: float = 0.0
    http_status: int = 200
    response_body: bytes = b""
    done_event: asyncio.Event = field(default_factory=asyncio.Event)


class DedupManager:
    """Two-phase request dedup for the 8471 command service.

    All mutating methods are synchronous (no awaits) and therefore atomic
    on a single asyncio event loop. ``wait_for`` is the only coroutine.
    """

    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        max_entries: int = MAX_CACHE_ENTRIES,
        max_single_payload_bytes: int = MAX_SINGLE_PAYLOAD_BYTES,
        max_total_bytes: int = MAX_TOTAL_CACHE_BYTES,
        max_in_flight: int = MAX_IN_FLIGHT_TOTAL,
        clock=time.time,
    ):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._max_single_payload = max_single_payload_bytes
        self._max_total_bytes = max_total_bytes
        self._max_in_flight = max_in_flight
        self._clock = clock
        self._records: dict[str, RequestRecord] = {}
        # Settled (COMPLETED/UNCERTAIN) request_ids in settle order,
        # oldest first. Used for TTL purge + LRU eviction.
        self._settled_order: OrderedDict[str, None] = OrderedDict()
        self._total_bytes = 0
        # O(1) IN_FLIGHT counter (audit D1). Incremented only when a NEW
        # IN_FLIGHT record is created; decremented on complete /
        # mark_uncertain / _remove-of-in-flight. Never derived by scanning.
        self._in_flight_count = 0

    # ------------------------------------------------------------------
    # Introspection (monitoring / tests)
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        settled = len(self._settled_order)
        return {
            "in_flight": self._in_flight_count,
            "in_flight_limit": self._max_in_flight,
            "settled": settled,
            "total_records": len(self._records),
            "total_bytes": self._total_bytes,
        }

    def get(self, request_id: str) -> RequestRecord | None:
        return self._records.get(request_id)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------
    def get_or_create(
        self, request_id: str, controller: str, target_pc: str
    ) -> tuple[bool, RequestRecord]:
        """Return ``(is_new, record)``.

        * New request_id -> creates an IN_FLIGHT record, returns (True, rec).
        * Existing IN_FLIGHT -> (False, rec): caller must NOT re-dispatch;
          use ``wait_for`` to attach to the in-flight execution.
        * Existing settled (COMPLETED/UNCERTAIN) within TTL -> (False, rec).
        * Existing settled but TTL-expired -> dropped, treated as new.

        :raises OverloadedError: when creating a NEW record would exceed
            MAX_IN_FLIGHT_TOTAL. Dedup hits (existing request_ids, including
            retries) are NOT counted against the cap and never raise.
            Callers translate this to HTTP 503.
        """
        now = self._clock()
        rec = self._records.get(request_id)
        if rec is not None:
            if rec.status == IN_FLIGHT:
                return False, rec
            if now - rec.completed_at < self._ttl:
                return False, rec
            # TTL expired: drop the stale record, fall through to recreate.
            self._remove(request_id)

        # Only NEW records consume IN_FLIGHT budget (audit D1). Retries and
        # cache hits above return before this point and are always allowed.
        if self._in_flight_count >= self._max_in_flight:
            raise OverloadedError(
                f"too many in-flight requests ({self._in_flight_count} >= "
                f"{self._max_in_flight})"
            )

        rec = RequestRecord(
            request_id=request_id,
            controller=controller,
            target_pc=target_pc,
            status=IN_FLIGHT,
            created_at=now,
        )
        self._records[request_id] = rec
        self._in_flight_count += 1
        self._enforce_limits(now)
        return True, rec

    def complete(
        self, request_id: str, http_status: int = 200, body: bytes | str | None = b""
    ) -> bool:
        """Move an IN_FLIGHT record to COMPLETED and wake waiters.

        Bodies larger than MAX_SINGLE_PAYLOAD_BYTES are NOT cached (only the
        status is kept). Returns False if the record is unknown or already
        settled (never clobbers a settled record).
        """
        rec = self._records.get(request_id)
        if rec is None or rec.status != IN_FLIGHT:
            return False
        if isinstance(body, str):
            body = body.encode("utf-8")
        body = body or b""

        rec.status = COMPLETED
        self._in_flight_count -= 1
        rec.completed_at = self._clock()
        rec.http_status = http_status
        if len(body) <= self._max_single_payload:
            rec.response_body = bytes(body)
            self._total_bytes += len(rec.response_body)
        else:
            rec.response_body = b""
        self._settled_order[request_id] = None  # most-recently settled -> end
        rec.done_event.set()
        self._enforce_limits()
        return True

    async def wait_for(self, request_id: str, timeout: float | None = None) -> RequestRecord:
        """Wait until an IN_FLIGHT record settles; return the record.

        Returns immediately for already-settled records. Raises KeyError if
        the request_id is unknown, asyncio.TimeoutError on timeout (caller
        should answer 504 EXECUTION_UNCERTAIN, never auto-retry).
        """
        rec = self._records.get(request_id)
        if rec is None:
            raise KeyError(request_id)
        if rec.status != IN_FLIGHT:
            return rec
        await asyncio.wait_for(rec.done_event.wait(), timeout)
        return rec

    def mark_uncertain(self, request_id: str) -> bool:
        """Move an IN_FLIGHT record to UNCERTAIN (PC WSS dropped mid-flight).

        Wakes waiters; they must answer 504 EXECUTION_UNCERTAIN. The record
        stays until TTL expiry so retries with the same request_id keep
        getting 504 instead of re-executing (at-most-once). Returns False if
        unknown or already settled.
        """
        rec = self._records.get(request_id)
        if rec is None or rec.status != IN_FLIGHT:
            return False
        rec.status = UNCERTAIN
        self._in_flight_count -= 1
        rec.completed_at = self._clock()  # TTL measured from settle time
        rec.http_status = 504
        rec.response_body = b""
        self._settled_order[request_id] = None
        rec.done_event.set()
        self._enforce_limits()
        return True

    def mark_uncertain_for_target(self, target_pc: str) -> list[str]:
        """Mark ALL in-flight requests for one PC as UNCERTAIN.

        Call this when the PC's WSS connection drops. Returns the affected
        request_ids.
        """
        affected = []
        for rid, rec in list(self._records.items()):
            if rec.status == IN_FLIGHT and rec.target_pc == target_pc:
                if self.mark_uncertain(rid):
                    affected.append(rid)
        return affected

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _remove(self, request_id: str) -> None:
        rec = self._records.pop(request_id, None)
        if rec is None:
            return
        if rec.status == IN_FLIGHT:
            # Safety net: IN_FLIGHT records are normally only retired via
            # complete()/mark_uncertain(); decrement if one is removed here.
            self._in_flight_count -= 1
        self._settled_order.pop(request_id, None)
        self._total_bytes -= len(rec.response_body)
        if self._total_bytes < 0:  # guard against accounting drift
            self._total_bytes = 0
        if self._in_flight_count < 0:  # guard against accounting drift
            self._in_flight_count = 0

    def _purge_expired(self, now: float) -> None:
        # _settled_order is oldest-settled-first; stop at first live record.
        while self._settled_order:
            rid = next(iter(self._settled_order))
            rec = self._records.get(rid)
            if rec is None:
                self._settled_order.pop(rid, None)
                continue
            if now - rec.completed_at < self._ttl:
                break
            self._remove(rid)

    def _enforce_limits(self, now: float | None = None) -> None:
        """Purge expired, then evict oldest-settled until within all bounds.

        IN_FLIGHT records are never in _settled_order, so they can never
        be evicted here.
        """
        if now is None:
            now = self._clock()
        self._purge_expired(now)
        while len(self._settled_order) > self._max_entries:
            rid, _ = self._settled_order.popitem(last=False)
            self._remove(rid)
        while self._total_bytes > self._max_total_bytes and self._settled_order:
            rid, _ = self._settled_order.popitem(last=False)
            self._remove(rid)
