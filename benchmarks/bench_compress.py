#!/usr/bin/env python3
"""Benchmark: SmartCompressor.

Gemini-4 targets (code_artifact__2.md section 5.3):
- 64KB text chunk compress (zlib level=1): ~0.28ms
- At 10Mbps, saves ~40ms transfer time (140:1 ROI)
- <2KB: skip (identity)
- Blacklisted suffixes (.zip/.png/.exe/...): skip
- 1KB probe: if saves <15%, skip (high-entropy binary)

Tries to import real SmartCompressor from mused/compress.py.
Falls back to spec-faithful reference.

Pure stdlib (zlib). No network.
"""
import os
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mused"))

try:
    from compress import SmartCompressor
    REAL_IMPL = True
    _USE_CLASS = True
except ImportError:
    try:
        # Real impl uses module-level functions
        import compress as _compress_mod
        if hasattr(_compress_mod, "should_compress"):
            REAL_IMPL = True
            _USE_CLASS = False
        else:
            raise ImportError
    except ImportError:
        REAL_IMPL = False
        _USE_CLASS = True

    BLACKLIST = {
        ".zip", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz", ".zst",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mkv",
        ".mp3", ".pdf", ".woff2", ".exe", ".dll", ".so", ".pyc", ".whl",
    }

    class SmartCompressor:
        """Reference per Gemini-4 spec (code_artifact__2.md 5.3)."""

        def should_compress(self, data: bytes, filename=None) -> bool:
            if len(data) < 2048:
                return False
            if filename:
                ext = os.path.splitext(filename)[1].lower()
                if ext in BLACKLIST:
                    return False
            if len(data) > 65536:
                probe = zlib.compress(data[:1024], 1)
                if len(probe) > 870:  # saves <15%
                    return False
            return True

        def compress(self, data: bytes):
            return zlib.compress(data, 1), "deflate"

        def decompress(self, data: bytes, encoding: str) -> bytes:
            if encoding == "deflate":
                return zlib.decompress(data)
            return data


def make_text_data(size):
    """Simulate 1MB log file: repetitive text lines."""
    line = b"2026-10-19 11:30:00 [INFO] worker-3 processed request id=abc123 latency=12ms\n"
    reps = size // len(line) + 1
    return (line * reps)[:size]


def make_binary_data(size):
    """1MB high-entropy random bytes (os.urandom)."""
    return os.urandom(size)


def percentile(data, p):
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def main():
    print("=" * 60)
    print("SmartCompressor benchmark")
    print(f"Implementation: {'REAL (mused/compress.py)' if REAL_IMPL else 'REFERENCE (spec-faithful mock)'}")
    print("=" * 60)
    if REAL_IMPL and not _USE_CLASS:
        import compress as _cm
        class _Adapter:
            def should_compress(self, data, filename=None):
                return _cm.should_compress(data, filename)
            def compress(self, data):
                return _cm.compress(data)
            def decompress(self, data, encoding):
                return _cm.decompress(data, encoding)
        comp = _Adapter()
    else:
        comp = SmartCompressor()

    # --- 1. text file: 1MB log ---
    text = make_text_data(1024 * 1024)
    print(f"\nText (1MB log, should_compress): {comp.should_compress(text, 'app.log')}")
    assert comp.should_compress(text, "app.log"), "text should compress!"

    # 64KB chunk timing (Gemini claim: ~0.28ms)
    chunk = text[:65536]
    times = []
    for _ in range(1000):
        t0 = time.perf_counter_ns()
        out, enc = comp.compress(chunk)
        t1 = time.perf_counter_ns()
        times.append((t1 - t0) / 1e6)  # ms
    p50 = percentile(times, 50)
    ratio = len(out) / len(chunk)
    print(f"64KB text chunk compress (zlib level=1, n=1000):")
    print(f"  p50: {p50:.3f} ms   target: ~0.28 ms   {'PASS' if p50 < 1.0 else 'FAIL'}")
    print(f"  ratio: {ratio:.1%} (text -> ~15-22% expected)")
    print(f"  encoding: {enc}")

    # full 1MB
    t0 = time.perf_counter_ns()
    out1m, _ = comp.compress(text)
    t1 = time.perf_counter_ns()
    print(f"1MB text: {(t1-t0)/1e6:.2f} ms, ratio {len(out1m)/len(text):.1%}")

    # round-trip
    back = comp.decompress(out, enc)
    assert back == chunk, "round-trip failed!"
    print(f"  round-trip: OK")

    # --- 2. binary file: 1MB random (should skip) ---
    binary = make_binary_data(1024 * 1024)
    skip = not comp.should_compress(binary, "data.bin")
    print(f"\nBinary (1MB random, should SKIP): {skip}  {'PASS' if skip else 'FAIL'}")

    # --- 3. small payload <2KB (should skip) ---
    small = make_text_data(1024)
    skip_small = not comp.should_compress(small, "tiny.log")
    print(f"Small (1KB text, should SKIP): {skip_small}  {'PASS' if skip_small else 'FAIL'}")

    # --- 4. blacklisted suffix ---
    skip_zip = not comp.should_compress(text, "archive.zip")
    skip_png = not comp.should_compress(text, "image.png")
    skip_exe = not comp.should_compress(text, "app.exe")
    print(f"Blacklist (.zip/.png/.exe skip): "
          f"{skip_zip and skip_png and skip_exe}  "
          f"{'PASS' if (skip_zip and skip_png and skip_exe) else 'FAIL'}")

    # --- 5. transfer time saved at 10Mbps ---
    # 64KB text -> ~15% = ~10KB. At 10Mbps (1.25MB/s): 64KB=51ms, 10KB=8ms. Saved ~43ms.
    saved_ms = (len(chunk) - len(out)) / (10 * 1024 * 1024 / 8) * 1000
    print(f"\nTransfer saved (64KB chunk, 10Mbps): ~{saved_ms:.0f} ms "
          f"(Gemini claim ~40ms)")
    print(f"  ROI: {saved_ms:.0f}ms saved / {p50:.2f}ms CPU = {saved_ms/max(p50,0.01):.0f}:1")

    print("\n" + "=" * 60)
    ok = (p50 < 1.0 and skip and skip_small
          and skip_zip and skip_png and skip_exe)
    print(f"OVERALL: {'PASS' if ok else 'FAIL'}")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
