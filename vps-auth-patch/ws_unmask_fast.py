"""
Fast WebSocket payload unmasking.

Tries to use the C extension ``ws_unmask`` (built separately on the VPS).
Falls back to an optimized pure-Python implementation (bytearray +
memoryview-friendly loop) when the extension is unavailable.

Public API:
    unmask_payload(payload: bytes, mask: bytes) -> bytes
    BACKEND  -> "c" if the C extension is active, otherwise "python"
"""

from __future__ import annotations

__all__ = ["unmask_payload", "BACKEND"]


def _unmask_python(payload: bytes, mask: bytes) -> bytes:
    """Optimized pure-Python unmask.

    Mask index must be the absolute byte position (``j % 4``) so that
    chunk boundaries do not shift the mask alignment.
    """
    if len(mask) != 4:
        raise ValueError("mask must be 4 bytes")
    ba = bytearray(payload)
    m0, m1, m2, m3 = mask[0], mask[1], mask[2], mask[3]
    n = len(ba)
    i = 0
    # Fast path: full 4-byte chunks.
    limit = n & ~3
    while i < limit:
        ba[i] ^= m0
        ba[i + 1] ^= m1
        ba[i + 2] ^= m2
        ba[i + 3] ^= m3
        i += 4
    # Tail bytes: absolute mask index (j % 4).
    while i < n:
        ba[i] ^= mask[i & 3]
        i += 1
    return bytes(ba)


try:  # pragma: no cover - exercised on the VPS
    from ws_unmask import unmask as _c_unmask

    BACKEND = "c"

    def unmask_payload(payload: bytes, mask: bytes) -> bytes:
        if len(mask) != 4:
            raise ValueError("mask must be 4 bytes")
        return _c_unmask(payload, mask)

except ImportError:
    BACKEND = "python"

    def unmask_payload(payload: bytes, mask: bytes) -> bytes:
        return _unmask_python(payload, mask)


def _unmask_slow(payload: bytes, mask: bytes) -> bytes:
    """Reference implementation (byte-by-byte list comprehension)."""
    return bytes([b ^ mask[i % 4] for i, b in enumerate(payload)])


if __name__ == "__main__":
    import os
    import time

    print(f"BACKEND = {BACKEND!r}")

    rng = __import__("random").Random(0xC0FFEE)
    sizes = [0, 1, 3, 4, 5, 100, 1000, 65536]
    for size in sizes:
        payload = bytes(rng.randrange(256) for _ in range(size))
        mask = bytes(rng.randrange(256) for _ in range(4))
        got = unmask_payload(payload, mask)
        want = _unmask_slow(payload, mask)
        assert got == want, f"mismatch at size={size}"
        print(f"size={size:>6}: OK")

    # Bad-mask check.
    for bad in (b"", b"\x00" * 3, b"\x00" * 5):
        try:
            unmask_payload(b"hello", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for mask len {len(bad)}")
    print("mask-length validation: OK")

    # Quick perf sanity (informational only).
    payload = os.urandom(1 << 20)  # 1 MiB
    mask = os.urandom(4)
    t0 = time.perf_counter()
    for _ in range(20):
        unmask_payload(payload, mask)
    dt = time.perf_counter() - t0
    print(f"1 MiB x20: {dt:.3f}s ({20 / dt:.1f} ops/s backend={BACKEND})")

    print("ALL SELF-TESTS PASSED")
