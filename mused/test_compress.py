"""SmartCompressor 测试：阈值 / 黑名单 / 探针 / 高熵跳过 / round-trip / 协议头。"""

import os
import time
import traceback
import zlib

import pytest

from compress import (
    DECOMPRESS_CHUNK,
    ENCODING_DEFLATE,
    ENCODING_IDENTITY,
    ENCODING_ZSTD,
    MAX_DECOMPRESSED_SIZE,
    compress,
    decompress,
    has_zstd,
    request_headers,
    response_encoding,
    should_compress,
)


def _text(n: int) -> bytes:
    line = "2026-10-19 11:30:00 INFO  [worker-3] processed request id=abc123 latency=12ms\n"
    reps = (n // len(line)) + 1
    return (line * reps).encode()[:n]


# ---------------------------------------------------------------------------
# 1. 体积阈值 (< 2KB 不压缩)
# ---------------------------------------------------------------------------

def test_tiny_payload_skipped():
    assert should_compress(b"x" * 100, "log.txt") is False


def test_just_under_threshold_skipped():
    assert should_compress(_text(2047), "log.txt") is False


def test_at_threshold_compresses():
    assert should_compress(_text(2048), "log.txt") is True


def test_empty_payload():
    assert should_compress(b"", "log.txt") is False


# ---------------------------------------------------------------------------
# 2. 后缀黑名单
# ---------------------------------------------------------------------------

def _parametrize_blacklist():
    return [
        "photo.png", "img.jpg", "img.jpeg", "a.gif", "b.webp",
        "v.mp4", "m.mkv", "s.mp3",
        "doc.pdf", "f.woff2",
        "a.zip", "b.gz", "c.tgz", "d.7z", "e.rar", "f.bz2", "g.xz", "h.zst",
        "app.exe", "lib.dll", "lib.so", "m.pyc", "p.whl",
    ]


def test_suffix_blacklist():
    for name in _parametrize_blacklist():
        # 即使是 100KB 的"文本"，后缀命中也直接跳过
        assert should_compress(_text(100 * 1024), name) is False, name


def test_suffix_case_insensitive():
    assert should_compress(_text(100 * 1024), "PHOTO.PNG") is False
    assert should_compress(_text(100 * 1024), "Doc.PDF") is False


def test_non_blacklisted_suffix_compresses():
    for name in ["app.log", "main.c", "data.json", "notes.txt", "f.bin", None]:
        assert should_compress(_text(10 * 1024), name) is True, name


# ---------------------------------------------------------------------------
# 3. 魔数兜底 (filename 为 None 或后缀不可靠)
# ---------------------------------------------------------------------------

def test_png_magic_skipped_without_filename():
    data = b"\x89PNG\r\n\x1a\n" + os.urandom(5000)
    assert should_compress(data, None) is False


def test_zip_magic_skipped_with_misleading_suffix():
    data = b"PK\x03\x04" + os.urandom(5000)
    assert should_compress(data, "readme.txt") is False


def test_exe_magic_skipped():
    data = b"MZ" + os.urandom(5000)
    assert should_compress(data, "blob.dat") is False


# ---------------------------------------------------------------------------
# 4. 探针：> 64KB 未知类型，前 1KB 试压
# ---------------------------------------------------------------------------

def test_probe_passes_for_text_over_64k():
    data = _text(100 * 1024)
    assert len(data) > 65536
    assert should_compress(data, "big.log") is True


def test_probe_rejects_high_entropy_over_64k():
    data = os.urandom(100 * 1024)  # 高熵随机字节
    assert should_compress(data, "random.bin") is False


def test_no_probe_needed_under_64k():
    # 2KB~64KB 之间不做探针，直接按前两条规则判定
    data = os.urandom(10 * 1024)
    # 随机数据不在黑名单、无魔数 → True (压缩时安全网会兜底)
    assert should_compress(data, "r.bin") is True
    payload, enc = compress(data)
    # 安全网：压完更大则原样返回
    assert enc == ENCODING_IDENTITY and payload == data


def test_probe_saves_under_15pct_rejected():
    # 构造探针恰好失败的数据：前 1KB 随机 (压不动)
    data = os.urandom(1024) + _text(100 * 1024 - 1024)
    assert should_compress(data, "mixed.bin") is False


# ---------------------------------------------------------------------------
# 5. compress / decompress round-trip
# ---------------------------------------------------------------------------

def test_roundtrip_deflate():
    data = _text(50 * 1024)
    payload, enc = compress(data)
    # OPT-05: encoding is backend-dependent (zstd if available, else deflate)
    assert enc in (ENCODING_DEFLATE, ENCODING_ZSTD)
    assert len(payload) < len(data)
    assert decompress(payload, enc) == data


def test_roundtrip_zstd():
    # OPT-05: explicit zstd roundtrip (skipped if zstandard not installed)
    pytest.importorskip("zstandard")
    data = _text(50 * 1024)
    payload, enc = compress(data)
    assert enc == ENCODING_ZSTD
    assert decompress(payload, enc) == data
    # zlib payloads still decode (backward compat)
    import zlib
    zlib_payload = zlib.compress(data, 1)
    assert decompress(zlib_payload, ENCODING_DEFLATE) == data


def test_roundtrip_identity():
    data = os.urandom(3000)
    assert decompress(data, ENCODING_IDENTITY) == data
    assert decompress(data, "") == data
    assert decompress(data, None) == data


def test_decompress_unknown_encoding_raises():
    try:
        decompress(b"xxx", "br")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_decompress_corrupt_deflate_raises():
    try:
        decompress(b"not-deflate-data", ENCODING_DEFLATE)
    except zlib.error:
        return
    raise AssertionError("expected zlib.error")


# ---------------------------------------------------------------------------
# 6. 协议头 helpers
# ---------------------------------------------------------------------------

def test_request_headers_compressed():
    payload, hdrs = request_headers(_text(10 * 1024), "a.log")
    # OPT-05: encoding is backend-dependent
    expected_enc = ENCODING_ZSTD if has_zstd() else ENCODING_DEFLATE
    assert hdrs == {"Content-Encoding": expected_enc}
    assert response_encoding(hdrs) == expected_enc
    assert decompress(payload, response_encoding(hdrs)) == _text(10 * 1024)


def test_request_headers_identity():
    payload, hdrs = request_headers(b"small", "a.log")
    assert hdrs == {}
    assert response_encoding(hdrs) == "identity"


def test_response_encoding_case_insensitive():
    assert response_encoding({"content-encoding": "Deflate"}) == "deflate"


def test_response_encoding_x_header_compat():
    assert response_encoding({"X-Content-Encoding": "deflate"}) == "deflate"


def test_response_encoding_unknown_raises():
    try:
        response_encoding({"Content-Encoding": "br"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# 7. 压缩比与性能数据 (打印实测数字)
# ---------------------------------------------------------------------------

def test_compression_ratio_report():
    cases = {
        "log文本 100KB": _text(100 * 1024),
        "JSON 100KB": (b'{"id":12345,"name":"sensor-a","value":3.14159,"ts":1729490000}\n' * 1600)[:100 * 1024],
        "C源码 50KB": (b"#include <stdio.h>\nint main(void) {\n    printf(\"hello %d\\n\", 42);\n    return 0;\n}\n" * 700)[:50 * 1024],
    }
    print("\n--- 压缩比实测 (zstd/zlib level=1) ---")
    for name, data in cases.items():
        t0 = time.perf_counter()
        payload, enc = compress(data)
        dt = (time.perf_counter() - t0) * 1000
        ratio = len(payload) / len(data)
        throughput = len(data) / 1024 / 1024 / ((time.perf_counter() - t0) or 1e-9)
        print(f"{name}: {len(data)} -> {len(payload)}  ratio={ratio:.3f}  {dt:.2f}ms  enc={enc}")
        assert enc in (ENCODING_DEFLATE, ENCODING_ZSTD)
        assert ratio < 0.30, name  # 文本类应压到 30% 以内
        assert decompress(payload, enc) == data


def test_probe_cost_microseconds():
    data = os.urandom(200 * 1024)
    t0 = time.perf_counter()
    for _ in range(100):
        should_compress(data, "r.bin")
    dt = (time.perf_counter() - t0) / 100 * 1e6
    print(f"\nshould_compress(200KB随机) 单次判定: {dt:.1f}us (含1KB探针)")
    assert dt < 1000  # 远小于 1ms


# ---------------------------------------------------------------------------
# C1. 解压炸弹防护 (SECURITY_V2.md 高危)：分块解压 + 输出硬上限
# ---------------------------------------------------------------------------

def test_decompress_bomb_rejected_small_limit():
    # 1MB 全零 → 压后仅 ~4.5KB；max_size=1024 时必须抛 ValueError 而非 OOM
    bomb = zlib.compress(b"\x00" * (1024 * 1024), 1)
    assert len(bomb) < 8192, f"bomb fixture too big: {len(bomb)}"
    try:
        decompress(bomb, ENCODING_DEFLATE, max_size=1024)
    except ValueError as e:
        assert "exceeds limit" in str(e)
    else:
        raise AssertionError("bomb was NOT rejected")


def test_decompress_bomb_rejected_default_limit():
    # 70MB 全零 → 压后 ~70KB；默认 64MB 上限必须抛 ValueError。
    # 若用旧实现 zlib.decompress() 会直接分配 70MB+ (生产炸弹可达 5GB)。
    raw = b"\x00" * (70 * 1024 * 1024)
    bomb = zlib.compress(raw, 1)
    print(f"\nbomb fixture: {len(raw)} -> {len(bomb)} bytes")
    assert len(bomb) < 1024 * 1024
    t0 = time.perf_counter()
    try:
        decompress(bomb, ENCODING_DEFLATE)  # default max_size=64MB
    except ValueError as e:
        dt = (time.perf_counter() - t0) * 1000
        print(f"bomb rejected in {dt:.1f}ms: {e}")
        assert "exceeds limit" in str(e)
    else:
        raise AssertionError("70MB bomb was NOT rejected by default limit")


def test_decompress_boundary_exact_limit_passes():
    # 输出恰好 == max_size 时通过
    raw = b"A" * 5000
    bomb = zlib.compress(raw, 1)
    out = decompress(bomb, ENCODING_DEFLATE, max_size=5000)
    assert out == raw


def test_decompress_boundary_one_over_rejected():
    # 输出 == max_size+1 时拒绝
    raw = b"A" * 5000
    bomb = zlib.compress(raw, 1)
    try:
        decompress(bomb, ENCODING_DEFLATE, max_size=4999)
    except ValueError as e:
        assert "exceeds limit" in str(e)
    else:
        raise AssertionError("over-limit output was NOT rejected")


def test_decompress_large_legit_roundtrip():
    # 200KB 文本 → 压后一次 input chunk 内，但输出远超单次 64KB 上限。
    # 验证 unconsumed_tail 被正确排空 (审计建议的 naive for-i-range 实现
    # 会静默截断此处输出而不报错)。
    raw = _text(200 * 1024)
    bomb = zlib.compress(raw, 1)
    out = decompress(bomb, ENCODING_DEFLATE)
    assert out == raw, f"roundtrip corrupt: {len(out)} != {len(raw)}"


def test_decompress_truncated_raises_not_hang():
    # 截断的 deflate 流：必须抛 zlib.error，且不能死循环
    raw = _text(10 * 1024)
    bomb = zlib.compress(raw, 1)
    t0 = time.perf_counter()
    try:
        decompress(bomb[: len(bomb) // 2], ENCODING_DEFLATE)
    except zlib.error:
        pass
    else:
        raise AssertionError("truncated stream did not raise")
    dt = time.perf_counter() - t0
    assert dt < 5, f"truncated decompress took too long: {dt:.1f}s (hang?)"


def test_decompress_empty_deflate_raises():
    # 空输入不可能是合法 deflate 流；与旧 zlib.decompress(b"") 一致抛错
    try:
        decompress(b"", ENCODING_DEFLATE)
    except zlib.error:
        pass
    else:
        raise AssertionError("empty deflate input did not raise")


def test_decompress_max_size_param_respected():
    # max_size 参数透传：小上限拒绝中等输出
    raw = _text(10 * 1024)
    bomb = zlib.compress(raw, 1)
    try:
        decompress(bomb, ENCODING_DEFLATE, max_size=100)
    except ValueError:
        pass
    else:
        raise AssertionError("custom max_size not respected")
    # 放宽后通过
    assert decompress(bomb, ENCODING_DEFLATE, max_size=len(raw)) == raw


def test_decompress_constants_sane():
    assert MAX_DECOMPRESSED_SIZE == 64 * 1024 * 1024
    assert DECOMPRESS_CHUNK == 65536


if __name__ == "__main__":
    # 无 pytest 环境时的自运行入口 (pytest 也可直接收集 test_* 函数)
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed, failed = 0, []
    for name, fn in fns:
        try:
            fn()
        except Exception:
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"ok   {name}")
    print(f"\n{passed} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)
