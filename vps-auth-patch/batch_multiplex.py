#!/usr/bin/env python3
"""VPS-side multiplex /batch handler (8471 service).

Implements the server side of the ``mused/microbatch.py`` contract. The VM
daemon sends::

    POST /muse-api/batch
    {"batch_id": "<uuid>", "requests": [
        {"id": "<sub_id>", "method": "POST",
         "path": "/cmd", "body": {...}}, ... ]}

and expects ``(200, '[{"id","status","body"}, ...]')``. The VPS fans the
batch out into N individual WSS messages to the PC, then packs the
per-sub results back into one array.

Wire format
-----------
* Request envelope:  ``{"batch_id": "<uuid>", "requests": [sub, ...]}``
  where each sub is ``{"id", "method", "path", "body"}``.
* Response: HTTP 200, body ``[{"id", "status", "body"}, ...]`` with
  ``body`` always a JSON string; items are returned in request order.

Dispatch rule (legacy coexistence)
----------------------------------
* body is a dict with a ``requests`` list  -> this multiplex handler.
* body is a dict with a ``commands`` list  -> the LEGACY shell batch
  handler (``{"commands": [...]}``), unchanged by this module.
* Check order is multiplex first: if both keys are present, ``requests``
  wins. Suggested wiring::

      if is_multiplex_batch(body):
          return await handle_batch_multiplex(...)
      elif is_legacy_shell_batch(body):
          return legacy_shell_batch(...)
      else:
          return single_cmd(...)

Dedup granularity
-----------------
Every sub-request is deduped on the composite key
``dedup.batch_sub_key(batch_id, sub_id)`` = ``"batch:<bid>:<sid>"``.
A retry of the same sub inside the SAME batch_id replays (COMPLETED),
attaches (IN_FLIGHT) or sticks (UNCERTAIN); the same sub_id under a
DIFFERENT batch_id is a new execution. Result *retrieval* is idempotent
at-least-once via the COMPLETED cache; execution is at-most-once.

Gemini §4.3 iron rules (VPS side)
---------------------------------
1. The OUTER HTTP status is 200 whenever the transport is healthy;
   per-sub-request results -- including failures -- travel inside the
   response array. 400 is returned ONLY for a malformed batch envelope
   (body not a dict, ``requests`` not a list, too many sub-requests).
2. One sub-request failing NEVER fails the whole batch: an ``execute``
   exception becomes that sub's 504 EXECUTION_UNCERTAIN item.
3. At-most-once per sub-request, enforced by DedupManager on the
   composite key (see dedup.py's two-phase state machine).
4. Sub-requests run CONCURRENTLY (asyncio.gather); a never-raising
   wrapper per sub guarantees one failure cannot cancel its siblings.

Wiring seam: ``execute`` callback
---------------------------------
``handle_batch_multiplex`` does NOT talk to the PC itself. The
muse-bridge.py wiring provides::

    async def execute(sub_id, composite_request_id, method, path,
                      sub_body_dict) -> (status:int, body:bytes|str)

It performs one sub-request's WSS round-trip (enqueue via the scheduler;
paths are passed through unchanged -- the scheduler already normalizes
"/cmd"-style paths). It MAY RAISE on transport failure; that is turned
into the sub's 504 item (at-most-once: the sub's record is marked
UNCERTAIN so a same-batch retry will not re-execute).

Pure standard library: asyncio, json, uuid, time.
"""

import asyncio
import json
import uuid

from dedup import (
    DedupManager,
    IN_FLIGHT,
    COMPLETED,
    UNCERTAIN,
    OverloadedError,
    batch_sub_key,
)

__all__ = [
    "MAX_SUB_REQUESTS",
    "is_multiplex_batch",
    "is_legacy_shell_batch",
    "handle_batch_multiplex",
]

#: Server-side cap on sub-requests per multiplex batch (DoS guard).
#: The daemon caps at 10 and 64KB; this is a hard ceiling for
#: hand-crafted / malicious envelopes.
MAX_SUB_REQUESTS = 100

#: Default per-sub attach timeout when a duplicate arrives while the
#: original is IN_FLIGHT.
_DEFAULT_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# Dispatch predicates
# ---------------------------------------------------------------------------
def is_multiplex_batch(body) -> bool:
    """True when body is a dict carrying a ``requests`` list (new protocol)."""
    return isinstance(body, dict) and isinstance(body.get("requests"), list)


def is_legacy_shell_batch(body) -> bool:
    """True when body is a dict carrying a ``commands`` list (legacy)."""
    return isinstance(body, dict) and isinstance(body.get("commands"), list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _error_envelope(message: str) -> list:
    return [{"error": message}]


def _uncertain_body() -> str:
    return json.dumps({"error": "EXECUTION_UNCERTAIN"})


def _text_of(raw) -> str:
    """Normalize an execute() body to str for the response array."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    return str(raw)


def _validate_sub(sub):
    """Return (sub_id, method, path, sub_body) or a 400 item.

    Returns ``(None, None, None, None, item)`` for invalid subs, where
    ``item`` is the ready-made 400 sub-item.
    """
    if not isinstance(sub, dict):
        return None, None, None, None, {
            "id": None, "status": 400, "body": "invalid sub-request"}
    sub_id = sub.get("id")
    if sub_id is None or not isinstance(sub_id, (str, int, float, bool)):
        return None, None, None, None, {
            "id": None, "status": 400, "body": "missing sub-request id"}
    path = sub.get("path")
    if not isinstance(path, str) or not path:
        return None, None, None, None, {
            "id": sub_id, "status": 400,
            "body": "missing or invalid sub-request path"}
    method = str(sub.get("method") or "POST").upper()
    sub_body = sub.get("body")
    if sub_body is None:
        sub_body = {}
    return sub_id, method, path, sub_body, None


def _replay_item(sub_id, rec):
    """COMPLETED-record -> sub-item (idempotent replay)."""
    return {
        "id": sub_id,
        "status": rec.http_status,
        "body": rec.response_body.decode("utf-8", errors="replace"),
    }


def _sub_id_of(requests, idx):
    """Echo-safe sub id for internal error items (never raises)."""
    sub = requests[idx]
    if isinstance(sub, dict):
        sid = sub.get("id")
        if sid is None or not isinstance(sid, (str, int, float, bool)):
            return None
        return sid
    return None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------
async def handle_batch_multiplex(
    body,
    *,
    controller,
    target_pc,
    dedup: DedupManager,
    execute,
    timeout: float = _DEFAULT_TIMEOUT,
):
    """Fan a multiplex batch out to the PC and return ``(status, items)``.

    ``execute``: ``async (sub_id, composite_request_id, method, path,
    sub_body_dict) -> (status:int, body:bytes|str)``; may raise on
    transport failure.

    Returns ``(200, [{"id","status","body"}, ...])`` in request order,
    or ``(400, [{"error": ...}])`` for a malformed envelope only.
    """
    # ---- envelope validation: 400s are the ONLY non-200 outers ----
    if not isinstance(body, dict):
        return 400, _error_envelope("malformed batch: body must be a dict")
    requests = body.get("requests")
    if not isinstance(requests, list):
        return 400, _error_envelope(
            "malformed batch: 'requests' must be a list")
    if len(requests) > MAX_SUB_REQUESTS:
        return 400, _error_envelope(
            f"malformed batch: too many sub-requests "
            f"({len(requests)} > {MAX_SUB_REQUESTS})")
    batch_id = body.get("batch_id") or uuid.uuid4().hex
    if not requests:
        return 200, []

    # ---- per-sub dispatch: validation is synchronous, execution is not --
    results: list = [None] * len(requests)
    jobs = []  # (index, coro)
    for i, sub in enumerate(requests):
        sub_id, method, path, sub_body, invalid = _validate_sub(sub)
        if invalid is not None:
            results[i] = invalid
            continue
        jobs.append((i, _run_sub(
            batch_id, sub_id, method, path, sub_body,
            controller=controller, target_pc=target_pc,
            dedup=dedup, execute=execute, timeout=timeout)))

    async def _safe(coro, fallback_id):
        # Never-raising: one sub's failure must not cancel its siblings.
        try:
            return await coro
        except Exception as e:  # defensive; _run_sub already traps
            return {"id": fallback_id, "status": 500,
                    "body": f"internal error: {e}"}

    done = await asyncio.gather(*[
        _safe(coro, _sub_id_of(requests, idx)) for idx, coro in jobs
    ])

    for (idx, _), item in zip(jobs, done):
        results[idx] = item
    return 200, results


async def _run_sub(batch_id, sub_id, method, path, sub_body, *,
                   controller, target_pc, dedup, execute, timeout):
    """Execute one sub-request under dedup; never raises."""
    key = batch_sub_key(batch_id, sub_id)
    try:
        try:
            is_new, rec = dedup.get_or_create(key, controller, target_pc)
        except OverloadedError:
            # IN_FLIGHT cap reached: shed load, safe to retry.
            return {"id": sub_id, "status": 503,
                    "body": "SERVER_OVERLOADED"}

        if not is_new:
            return await _attach_or_replay(
                sub_id, key, rec, dedup=dedup, timeout=timeout)

        # We own this execution.
        try:
            status, raw = await execute(sub_id, key, method, path, sub_body)
        except Exception:
            # May or may not have reached the PC: at-most-once says do NOT
            # retry with the same composite key; report uncertainty instead.
            dedup.mark_uncertain(key)
            return {"id": sub_id, "status": 504,
                    "body": _uncertain_body()}
        body_text = _text_of(raw)
        dedup.complete(key, int(status), body_text)
        return {"id": sub_id, "status": int(status), "body": body_text}
    except Exception as e:  # last-resort guard: batch must survive
        return {"id": sub_id, "status": 500,
                "body": f"internal error: {e}"}


async def _attach_or_replay(sub_id, key, rec, *, dedup, timeout):
    """Not-new composite key: replay COMPLETED, stick UNCERTAIN,
    or attach to IN_FLIGHT."""
    if rec.status == COMPLETED:
        return _replay_item(sub_id, rec)
    if rec.status == UNCERTAIN:
        return {"id": sub_id, "status": 504, "body": _uncertain_body()}
    # IN_FLIGHT: attach to the in-flight execution, do NOT re-dispatch.
    try:
        rec = await dedup.wait_for(key, timeout=timeout)
    except asyncio.TimeoutError:
        return {"id": sub_id, "status": 504, "body": _uncertain_body()}
    if rec.status == COMPLETED:
        return _replay_item(sub_id, rec)
    return {"id": sub_id, "status": 504, "body": _uncertain_body()}
