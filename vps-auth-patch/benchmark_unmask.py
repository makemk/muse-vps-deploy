"""
Benchmark: WebSocket payload unmasking, old vs new.

Compares:
  OLD (slow): bytes([b ^ mask[i % 4] for i, b in enumerate(payload)])
  NEW (fast): ws_unmask_fast.unmask_payload  (C ext if available, else optimized Python)

Payload sizes: 1KB, 64KB, 1MB, 10MB.
Iterations: 100 (10 for 10MB to keep runtime sane). Warmup: 5.

Output: table with size, old_time_ms, new_time_ms, speedup, old_MBps, new_MBps.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ws_unmask_fast import unmask_payload, BACKEND


def unmask_old(payload: bytes, mask: bytes) -> bytes:
    """The old slow implementation (byte-by-byte list comprehension)."""
    return bytes([b ^ mask[i % 4] for i, b in enumerate(payload)])


def bench(fn, payload: bytes, mask: bytes, iterations: int, warmup: int = 5):
    """Time `fn(payload, mask)`; return mean seconds per operation."""
    # Warmup (also correctness-checked once by the caller).
    for _ in range(warmup):
        fn(payload, mask)
    t0 = time.perf_counter()
    for _ in range(iterations):
        fn(payload, mask)
    t1 = time.perf_counter()
    return (t1 - t0) / iterations


def main():
    print(f"ws_unmask_fast BACKEND = {BACKEND!r}\n")

    cases = [
        ("1KB", 1 << 10, 100),
        ("64KB", 64 << 10, 100),
        ("1MB", 1 << 20, 100),
        ("10MB", 10 << 20, 10),
    ]

    # Generate data up front (not timed).
    data = []
    for label, size, iters in cases:
        payload = os.urandom(size)
        mask = os.urandom(4)
        # Correctness check before timing.
        want = unmask_old(payload, mask)
        got = unmask_payload(payload, mask)
        assert got == want, f"mismatch at {label}"
        data.append((label, size, iters, payload, mask))
    print("Correctness: all sizes match reference implementation.\n")

    rows = []
    for label, size, iters, payload, mask in data:
        old_s = bench(unmask_old, payload, mask, iters)
        new_s = bench(unmask_payload, payload, mask, iters)
        mb = size / (1024 * 1024)
        old_ms = old_s * 1000
        new_ms = new_s * 1000
        old_mbps = mb / old_s
        new_mbps = mb / new_s
        speedup = old_s / new_s if new_s > 0 else float("inf")
        rows.append((label, old_ms, new_ms, speedup, old_mbps, new_mbps))

    # Print table.
    header = (
        f"{'size':>6} | {'old_time_ms':>11} | {'new_time_ms':>11} | "
        f"{'speedup':>8} | {'old_MBps':>9} | {'new_MBps':>9}"
    )
    print(header)
    print("-" * len(header))
    for label, old_ms, new_ms, speedup, old_mbps, new_mbps in rows:
        print(
            f"{label:>6} | {old_ms:>11.3f} | {new_ms:>11.3f} | "
            f"{speedup:>7.1f}x | {old_mbps:>8.1f} | {new_mbps:>8.1f}"
        )


if __name__ == "__main__":
    main()
