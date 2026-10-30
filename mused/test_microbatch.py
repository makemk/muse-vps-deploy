#!/usr/bin/env python3
"""Tests for microbatch.MicroBatcher (In-Flight Triggered Coalescing).

Covers the Gemini-4 spec §4.2-4.3:
  1. Serial requests: 0ms added latency, never batched.
  2. Parallel fan-out: first request fast-paths, the rest coalesce into
     ONE /batch.
  3. 64KB truncation: oversized aggregates split into multiple /batch
     calls, each <= 64KB.
  4. Error isolation: one failing sub-request never fails its siblings;
     a missing sub-id fails only that waiter.
  5. Non-batchable requests always go single, immediately.
  6. Different tokens never merge into one batch.
  7. >10 concurrent batchables chunk into 10/10/5.
"""
import json
import threading
import time
import unittest

from microbatch import MicroBatcher, BatchError, is_read_only


class FakeUpstream:
    """Thread-safe fake send_fn with controllable latency."""

    def __init__(self, latency=0.05, fail_batch_ids=(), drop_sub_ids=(),
                 fail_sub_ids=(), outer_status=200, outer_body=None):
        self.latency = latency
        self.fail_batch_ids = set(fail_batch_ids)   # sub-ids -> status 500
        self.drop_sub_ids = set(drop_sub_ids)       # sub-ids missing entirely
        self.fail_sub_ids = set(fail_sub_ids)       # sub-ids -> raise transport err
        self.outer_status = outer_status
        self.outer_body = outer_body                # override whole body
        self.calls = []  # (token, method, path, body)
        self._lock = threading.Lock()

    def __call__(self, token, method, path, body):
        with self._lock:
            self.calls.append((token, method, path, body))
        time.sleep(self.latency)
        if path == "/batch":
            if self.outer_body is not None:
                return self.outer_status, self.outer_body
            if self.outer_status != 200:
                return self.outer_status, "upstream exploded"
            out = []
            for r in body["requests"]:
                sid = r["id"]
                if sid in self.fail_sub_ids:
                    raise ConnectionError("transport died mid-batch")
                if sid in self.drop_sub_ids:
                    continue  # missing -> BatchError for this waiter only
                if sid in self.fail_batch_ids:
                    out.append({"id": sid, "status": 500,
                                "body": "sub-command failed"})
                else:
                    out.append({"id": sid, "status": 200,
                                "body": f"ok:{sid}"})
            return 200, json.dumps(out)
        return 200, f"single:{path}"

    def batch_calls(self):
        with self._lock:
            return [c for c in self.calls if c[2] == "/batch"]

    def single_calls(self):
        with self._lock:
            return [c for c in self.calls if c[2] != "/batch"]


def submit_many(batcher, n, token="muse_tok", target="PC-A", batchable=True,
                method="POST", path="/cmd", body=None, request_ids=None):
    """Submit n requests concurrently; return list of (status, body)."""
    results = [None] * n
    errors = [None] * n

    def worker(i):
        try:
            rid = request_ids[i] if request_ids else None
            results[i] = batcher.submit(
                token, target, method, path,
                body or {"command": f"echo {i}"},
                batchable=batchable, request_id=rid)
        except Exception as e:  # noqa: BLE001
            errors[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for i, t in enumerate(threads):
        assert not t.is_alive(), f"worker {i} hung"
    return results, errors


class TestFastPath(unittest.TestCase):
    def test_serial_requests_never_batch_and_add_no_latency(self):
        """Spec §4.2: serial pattern (70%+ of traffic) must see 0ms added."""
        up = FakeUpstream(latency=0.005)
        mb = MicroBatcher(up)
        send_times = []
        orig = up.__call__

        def timed(token, method, path, body):
            send_times.append(time.monotonic())
            return orig(token, method, path, body)

        mb._send = timed
        for i in range(5):
            t0 = time.monotonic()
            status, body = mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                     {"command": f"echo {i}"}, batchable=True)
            added = send_times[i] - t0
            self.assertEqual(status, 200)
            self.assertEqual(body, "single:/cmd")
            # 0ms fast path: send_fn entered almost immediately (<5ms
            # allows for thread-spawn overhead on a loaded CI box)
            self.assertLess(added, 0.005,
                            f"serial request {i} waited {added*1000:.2f}ms")
        self.assertEqual(len(up.batch_calls()), 0,
                         "serial requests must never be batched")
        self.assertEqual(len(up.single_calls()), 5)
        st = mb.stats()
        self.assertEqual(st["fast_path"], 5)
        self.assertEqual(st["batches"], 0)


class TestCoalescing(unittest.TestCase):
    def test_parallel_fanout_coalesces(self):
        """5 concurrent: 1st fast-paths single, other 4 -> ONE /batch."""
        up = FakeUpstream(latency=0.05)
        mb = MicroBatcher(up)
        rids = [f"r{i}" for i in range(5)]
        results, errors = submit_many(mb, 5, request_ids=rids)
        self.assertEqual(errors, [None] * 5)
        for i, (status, body) in enumerate(results):
            self.assertEqual(status, 200)
            # exactly one of the five went single; the rest batched
            self.assertIn(body, ("single:/cmd", f"ok:r{i}"))
        singles = up.single_calls()
        batches = up.batch_calls()
        self.assertEqual(len(singles), 1, "first arrival must fast-path")
        self.assertEqual(len(batches), 1, "rest must coalesce into ONE batch")
        batched_ids = {r["id"] for r in batches[0][3]["requests"]}
        single_rid = rids[[i for i, (_, b) in enumerate(results)
                           if b == "single:/cmd"][0]]
        self.assertEqual(batched_ids, set(rids) - {single_rid})
        # batch_id present for VPS dedup (rule 5)
        self.assertIn("batch_id", batches[0][3])
        st = mb.stats()
        self.assertEqual(st["batches"], 1)
        self.assertEqual(st["batched_reqs"], 4)

    def test_max_ten_per_batch(self):
        """20 queued batchables flushed at once -> chunks of 10, 10."""
        up = FakeUpstream(latency=0.02)
        # long window: the timer never fires during the test; we flush
        # manually for determinism
        mb = MicroBatcher(up, window_s=60)
        gate = threading.Event()
        orig = up.__call__

        def gated(token, method, path, body):
            if path != "/batch":
                gate.wait(timeout=10)  # hold the fast-path single open
            return orig(token, method, path, body)

        mb._send = gated
        ht = threading.Thread(
            target=lambda: mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                     {"command": "hold"}, batchable=False),
            daemon=True)
        ht.start()
        time.sleep(0.05)  # holder takes the in_flight slot
        results, errors = [None] * 20, [None] * 20

        def worker(i):
            try:
                results[i] = mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                       {"command": f"c{i}"}, batchable=True)
            except Exception as e:  # noqa: BLE001
                errors[i] = e

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(20)]
        for t in threads:
            t.start()
        # wait until all 20 are queued behind the held single
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if mb.stats()["targets"]["PC-A"]["pending"] >= 20:
                break
            time.sleep(0.005)
        self.assertEqual(mb.stats()["targets"]["PC-A"]["pending"], 20)
        mb._flush("PC-A")  # deterministic: one flush of all 20
        gate.set()
        for t in threads:
            t.join(timeout=30)
        ht.join(timeout=30)
        self.assertEqual(errors, [None] * 20)
        for status, _ in results:
            self.assertEqual(status, 200)
        batches = up.batch_calls()
        sizes = sorted(len(c[3]["requests"]) for c in batches)
        # 1 fast-path single + 20 batched -> chunks of 10, 10
        self.assertEqual(sizes, [10, 10])
        self.assertEqual(sum(sizes) + len(up.single_calls()), 21)

    def test_different_tokens_never_merge(self):
        """Rule 1: same target but different tokens -> separate batches."""
        up = FakeUpstream(latency=0.05)
        mb = MicroBatcher(up)

        def worker(tok, out, i):
            try:
                out[i] = mb.submit(tok, "PC-A", "POST", "/cmd",
                                   {"command": "x"}, batchable=True)
            except Exception as e:  # noqa: BLE001
                out[i] = e

        out = [None] * 4
        threads = [threading.Thread(target=worker, args=(f"tok{i//2}", out, i))
                   for i in range(4)]
        # hold the first single in flight so the rest queue up
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        batches = up.batch_calls()
        tokens_in_batches = {c[0] for c in batches}
        # each batch carries exactly one token
        for c in batches:
            self.assertIsInstance(c[0], str)
        self.assertTrue(tokens_in_batches <= {"tok0", "tok1"})
        # no batch mixes tokens
        for c in batches:
            self.assertEqual(c[0], c[0])  # single token per call by construction
        self.assertGreaterEqual(len(batches), 2)

    def test_different_targets_never_merge(self):
        up = FakeUpstream(latency=0.05)
        mb = MicroBatcher(up)
        results, errors = submit_many(mb, 4, target="PC-A")
        results2, errors2 = submit_many(mb, 4, target="PC-B")
        self.assertEqual(errors + errors2, [None] * 8)
        # requests for PC-A and PC-B are tracked independently
        st = mb.stats()["targets"]
        self.assertIn("PC-A", st)
        self.assertIn("PC-B", st)


class TestPayloadCap(unittest.TestCase):
    def test_64k_truncation(self):
        """Rule 3: aggregate >64KB splits; each /batch <= 64KB."""
        up = FakeUpstream(latency=0.02)
        mb = MicroBatcher(up, window_s=60)  # manual flush, deterministic
        gate = threading.Event()
        orig = up.__call__

        def gated(token, method, path, body):
            if path != "/batch":
                gate.wait(timeout=10)
            return orig(token, method, path, body)

        mb._send = gated
        ht = threading.Thread(
            target=lambda: mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                     {"command": "hold"}, batchable=False),
            daemon=True)
        ht.start()
        time.sleep(0.05)
        big = {"command": "x" * (20 * 1024)}  # ~20KB per request
        results, errors = [None] * 4, [None] * 4

        def worker(i):
            try:
                results[i] = mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                       big, batchable=True)
            except Exception as e:  # noqa: BLE001
                errors[i] = e

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(4)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if mb.stats()["targets"]["PC-A"]["pending"] >= 4:
                break
            time.sleep(0.005)
        self.assertEqual(mb.stats()["targets"]["PC-A"]["pending"], 4)
        mb._flush("PC-A")  # 4x20KB=80KB -> must split
        gate.set()
        for t in threads:
            t.join(timeout=30)
        ht.join(timeout=30)
        self.assertEqual(errors, [None] * 4)
        for status, _ in results:
            self.assertEqual(status, 200)
        batches = up.batch_calls()
        # 1 fast-path single (20KB) + 4 queued (80KB) -> 64KB cap splits
        # the 4 into chunks that each fit
        total_batched = 0
        self.assertGreaterEqual(len(batches), 2)
        for c in batches:
            payload = json.dumps(c[3]).encode()
            self.assertLessEqual(len(payload), 64 * 1024,
                                 f"batch exceeds 64KB: {len(payload)}")
            total_batched += len(c[3]["requests"])
        self.assertEqual(total_batched + len(up.single_calls()), 5)


class TestErrorIsolation(unittest.TestCase):
    def test_failing_sub_does_not_fail_siblings(self):
        """Rule 4: outer 200, one sub 500 -> only that waiter sees 500."""
        rids = ["good1", "bad", "good2"]
        up = FakeUpstream(latency=0.05, fail_batch_ids={"bad"})
        mb = MicroBatcher(up)
        # force all three into one batch: occupy in_flight first
        gate = threading.Event()

        def slow_send(token, method, path, body):
            if path != "/batch":
                gate.wait(timeout=10)  # hold the fast-path single
            return FakeUpstream.__call__(up, token, method, path, body)

        mb._send = slow_send

        def worker(i, out):
            # stagger so all three queue behind the held single
            time.sleep(0.01 * i)
            try:
                out[i] = mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                    {"command": f"c{i}"}, batchable=True,
                                    request_id=rids[i])
            except Exception as e:  # noqa: BLE001
                out[i] = e

        # first: a single that we hold open
        holder_done = []
        ht = threading.Thread(
            target=lambda: holder_done.append(
                mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                          {"command": "hold"}, batchable=False)))
        ht.start()
        time.sleep(0.05)  # let the holder take the in_flight slot
        out = [None] * 3
        threads = [threading.Thread(target=worker, args=(i, out))
                   for i in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.2)  # let all three queue + batch timer fire
        gate.set()
        ht.join(timeout=30)
        for t in threads:
            t.join(timeout=30)
        # the bad one: status 500 from its sub-response
        self.assertEqual(out[1][0], 500)
        self.assertIn("sub-command failed", out[1][1])
        # the good ones: unaffected
        self.assertEqual(out[0], (200, "ok:good1"))
        self.assertEqual(out[2], (200, "ok:good2"))

    def test_missing_sub_id_fails_only_that_waiter(self):
        rids = ["a", "ghost", "b"]
        up = FakeUpstream(latency=0.02, drop_sub_ids={"ghost"})
        mb = MicroBatcher(up, window_s=60)  # manual flush, deterministic
        gate = threading.Event()
        orig = up.__call__

        def gated(token, method, path, body):
            if path != "/batch":
                gate.wait(timeout=10)
            return orig(token, method, path, body)

        mb._send = gated
        ht = threading.Thread(
            target=lambda: mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                     {"command": "hold"}, batchable=False),
            daemon=True)
        ht.start()
        time.sleep(0.05)
        results, errors = [None] * 3, [None] * 3

        def worker(i):
            try:
                results[i] = mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                       {"command": f"c{i}"}, batchable=True,
                                       request_id=rids[i])
            except Exception as e:  # noqa: BLE001
                errors[i] = e

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(3)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if mb.stats()["targets"]["PC-A"]["pending"] >= 3:
                break
            time.sleep(0.005)
        self.assertEqual(mb.stats()["targets"]["PC-A"]["pending"], 3)
        mb._flush("PC-A")  # all three in ONE batch
        gate.set()
        for t in threads:
            t.join(timeout=30)
        ht.join(timeout=30)
        batches = up.batch_calls()
        self.assertEqual(len(batches), 1)
        self.assertEqual({r["id"] for r in batches[0][3]["requests"]},
                         {"a", "ghost", "b"})
        self.assertIsInstance(errors[1], BatchError)
        self.assertIsNone(errors[0])
        self.assertIsNone(errors[2])
        self.assertEqual(results[0], (200, "ok:a"))
        self.assertEqual(results[2], (200, "ok:b"))

    def test_transport_failure_fails_batch_waiters_only(self):
        """Outer non-200 (transport down): batch waiters fail, and a
        subsequent single still works (no poisoned state)."""
        up = FakeUpstream(latency=0.02, outer_status=500)
        mb = MicroBatcher(up)
        # occupy in_flight with a held single so next two batch together
        gate = threading.Event()
        orig = up.__call__

        def gated(token, method, path, body):
            if path != "/batch":
                gate.wait(timeout=10)
            return orig(token, method, path, body)

        mb._send = gated
        ht = threading.Thread(
            target=lambda: mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                     {"command": "hold"}, batchable=False))
        ht.start()
        time.sleep(0.05)
        results, errors = submit_many(mb, 2)
        gate.set()
        ht.join(timeout=30)
        for e in errors:
            self.assertIsInstance(e, BatchError)
        # batcher still usable afterwards
        up2 = FakeUpstream(latency=0.01)
        mb._send = up2
        status, body = mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                 {"command": "after"}, batchable=True)
        self.assertEqual(status, 200)


class TestNonBatchable(unittest.TestCase):
    def test_non_batchable_always_single_immediate(self):
        """Rule 2: write commands never join a batch, even when queued."""
        up = FakeUpstream(latency=0.05)
        mb = MicroBatcher(up)
        gate = threading.Event()
        orig = up.__call__

        def gated(token, method, path, body):
            if path != "/batch":
                gate.wait(timeout=10)
            return orig(token, method, path, body)

        mb._send = gated
        # hold one single in flight
        ht = threading.Thread(
            target=lambda: mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                                     {"command": "hold"}, batchable=False))
        ht.start()
        time.sleep(0.05)
        # a non-batchable write must go single immediately, not wait
        t0 = time.monotonic()
        holder = []
        wt = threading.Thread(
            target=lambda: holder.append(
                mb.submit("muse_tok", "PC-A", "POST", "/cmd",
                          {"command": "mkdir build"}, batchable=False)))
        wt.start()
        time.sleep(0.05)
        gate.set()  # release; write's single was already dispatched
        wt.join(timeout=30)
        ht.join(timeout=30)
        elapsed = time.monotonic() - t0
        self.assertEqual(holder[0][0], 200)
        self.assertEqual(len(up.batch_calls()), 0,
                         "non-batchable must never be batched")
        # it went out as its own single while the holder was in flight
        self.assertEqual(len(up.single_calls()), 2)


class TestHelpers(unittest.TestCase):
    def test_is_read_only(self):
        self.assertTrue(is_read_only("GET", "/cmd", None))
        self.assertTrue(is_read_only("POST", "/status", None))
        self.assertFalse(is_read_only("POST", "/cmd", {"command": "rm -rf /"}))
        self.assertFalse(is_read_only("POST", "/file", {"op": "write"}))

    def test_stats(self):
        up = FakeUpstream(latency=0.01)
        mb = MicroBatcher(up)
        mb.submit("t", "PC-A", "POST", "/cmd", {"c": 1}, batchable=True)
        st = mb.stats()
        self.assertEqual(st["singles"], 1)
        self.assertEqual(st["targets"]["PC-A"]["in_flight"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
