"""应用层端到端自适应载荷压缩 (Daemon 侧).

设计依据: Gemini 4 第二轮评审 §5.3；OPT-05 Phase 1 (zstd)。

架构原则:
  - 压缩只发生在数据源头 (VM Daemon) 与终点 (PC 插件)。
  - VPS 8471 服务端**完全不感知、不解压、不压缩**，只原样透传字节流
    与 ``Content-Encoding`` 头 —— VPS 零 CPU 消耗。

判定规则 (should_compress):
  1. 载荷 < 2KB → 不压缩 (装不满两个 TCP MSS，压了也省不出包)。
  2. filename 后缀命中黑名单 (已压缩/二进制格式) → 不压缩。
  3. 魔数命中已知二进制格式 → 不压缩 (filename 为 None 或不可靠时的兜底)。
  4. 载荷 > 64KB → 前 1KB 用当前算法试压；若压缩后 > 870B
     (即节省 < 15%) → 判高熵二进制，全块跳过。
     (zstd 有快速拒绝路径，探针开销可忽略；保留探针仅为行为一致)
  5. 其余 → 压缩。

压缩算法 (OPT-05 Phase 1):
  - 优先 ``zstd level 1`` (需 ``zstandard`` 包)；不可用时回退
    ``zlib.compress(data, level=1)`` (Z_BEST_SPEED)。
  - 编码标识: ``Content-Encoding: zstd`` 或 ``Content-Encoding: deflate``。
  - 解压侧同时接受两种编码 —— 新旧端混合部署期间互通。

协议:
  - 请求/响应头携带 ``Content-Encoding: zstd|deflate`` 表示载荷已压缩；
    不携带 (或为 ``identity``) 表示原样。
  - 为兼容旧端，解析时同时接受 ``X-Content-Encoding``。

纯 Python 标准库 + 可选 zstandard，线程安全。
"""

from __future__ import annotations

import os
import zlib

# ---------------------------------------------------------------------------
# 压缩后端 (OPT-05 Phase 1): zstd 优先，zlib 兜底
# ---------------------------------------------------------------------------
try:
    import zstandard

    _HAS_ZSTD = True
    _ZSTD_C = zstandard.ZstdCompressor(level=1)
    _ZSTD_D = zstandard.ZstdDecompressor()
except ImportError:  # pragma: no cover - 无 zstandard 时回退 zlib
    zstandard = None  # type: ignore[assignment]
    _HAS_ZSTD = False
    _ZSTD_C = None
    _ZSTD_D = None


def has_zstd() -> bool:
    """当前环境是否可用 zstd 压缩。"""
    return _HAS_ZSTD

# ---------------------------------------------------------------------------
# 常量 (Gemini §5.3)
# ---------------------------------------------------------------------------

#: 小于 2KB 不压缩
MIN_COMPRESS_SIZE = 2048
#: 大于 64KB 才做 1KB 探针
PROBE_THRESHOLD = 65536
#: 探针采样长度
PROBE_SIZE = 1024
#: 探针压缩后若 > 870B (即 1024B 节省 < 15%)，判为高熵，跳过压缩
PROBE_MAX_COMPRESSED = 870
#: 强制极速压缩级别
COMPRESS_LEVEL = 1

#: 解压输出硬上限 (64MB，与 dedup 的 MAX_TOTAL_CACHE_BYTES 对齐)。
#: 分块解压时累计输出超过此值即抛 ValueError —— 防 zip 炸弹 DoS。
MAX_DECOMPRESSED_SIZE = 64 * 1024 * 1024
#: 解压分块大小
DECOMPRESS_CHUNK = 65536

ENCODING_DEFLATE = "deflate"
ENCODING_ZSTD = "zstd"  # OPT-05 Phase 1
ENCODING_IDENTITY = "identity"

#: 后缀黑名单：已压缩或二进制格式 (Gemini §5.3)
SKIP_SUFFIXES = frozenset({
    ".zip", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz", ".zst",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp4", ".mkv", ".mp3",
    ".pdf", ".woff2",
    ".exe", ".dll", ".so", ".pyc", ".whl",
})

#: 魔数黑名单：(偏移, 魔数字节) —— filename 不可靠时的兜底
_MAGIC_SIGNATURES: tuple[tuple[int, bytes], ...] = (
    (0, b"\x89PNG\r\n\x1a\n"),        # PNG
    (0, b"\xff\xd8\xff"),             # JPEG
    (0, b"GIF87a"), (0, b"GIF89a"),   # GIF
    (0, b"PK\x03\x04"), (0, b"PK\x05\x06"), (0, b"PK\x07\x08"),  # ZIP/JAR/APK
    (0, b"\x1f\x8b"),                # GZIP
    (0, b"7z\xbc\xaf'\x1c"),          # 7z
    (0, b"Rar!\x1a\x07\x00"), (0, b"Rar!\x1a\x07\x01\x00"),      # RAR
    (0, b"%PDF"),                    # PDF
    (0, b"MZ"),                      # EXE/DLL (PE)
    (0, b"\x7fELF"),                 # ELF (.so)
    (4, b"ftyp"),                    # MP4 (box size 之后)
    (0, b"ID3"),                     # MP3
    (0, b"OggS"),                    # OGG
    (0, b"\xfd7zXZ\x00"),            # XZ
    (0, b"BZh"),                     # BZIP2
    (8, b"WEBP"),                    # WEBP (RIFF....WEBP)
)


def _has_binary_magic(data: bytes) -> bool:
    """魔数命中已知二进制格式则返回 True。"""
    for offset, magic in _MAGIC_SIGNATURES:
        end = offset + len(magic)
        if len(data) >= end and data[offset:end] == magic:
            return True
    return False


def should_compress(data: bytes, filename: str | None = None) -> bool:
    """按 Gemini §5.3 三步规则判定是否值得压缩。

    :param data: 待发送的载荷字节。
    :param filename: 可选文件名 (用于后缀黑名单，可为 None)。
    :return: True → 调用方应压缩后发送；False → 原样发送。
    """
    # 规则 1：< 2KB 不压缩
    if len(data) < MIN_COMPRESS_SIZE:
        return False

    # 规则 2：后缀黑名单
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in SKIP_SUFFIXES:
            return False

    # 魔数兜底 (filename 为 None 或后缀不可靠时)
    if _has_binary_magic(data):
        return False

    # 规则 3：> 64KB 的未知类型做 1KB 探针 (用当前压缩算法)
    if len(data) > PROBE_THRESHOLD:
        probe = _raw_compress(data[:PROBE_SIZE])
        if len(probe) > PROBE_MAX_COMPRESSED:
            return False

    return True


def _raw_compress(data: bytes) -> bytes:
    """用当前后端压缩 (不含安全网)，供探针与 compress() 共用。"""
    if _HAS_ZSTD:
        return _ZSTD_C.compress(data)
    return zlib.compress(data, COMPRESS_LEVEL)


def _raw_decompress_zstd(data: bytes, max_size: int) -> bytes:
    """zstd 流式解压，带输出上限保护。"""
    out_parts: list[bytes] = []
    total = 0
    with _ZSTD_D.stream_reader(data) as reader:
        while True:
            chunk = reader.read(DECOMPRESS_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise ValueError(
                    f"decompressed size exceeds limit ({max_size} bytes)"
                )
            out_parts.append(chunk)
    return b"".join(out_parts)


def compress(data: bytes) -> tuple[bytes, str]:
    """压缩载荷，返回 (payload, encoding)。

    编码为 ``zstd`` (zstandard 可用时) 或 ``deflate`` (回退)。
    安全网：若压缩后反而更大 (高熵数据)，返回原样 + "identity"。
    """
    out = _raw_compress(data)
    if len(out) >= len(data):
        return data, ENCODING_IDENTITY
    return out, (ENCODING_ZSTD if _HAS_ZSTD else ENCODING_DEFLATE)


def decompress(
    data: bytes, encoding: str | None, max_size: int = MAX_DECOMPRESSED_SIZE
) -> bytes:
    """按 encoding 还原载荷。

    :param max_size: 解压后输出的硬上限 (字节)。流式分块解压，
                     累计输出超过上限即抛 ``ValueError`` —— 防 zip 炸弹
                     (如 42KB → 5GB) 导致的 Daemon OOM 崩溃。
    :raises ValueError: encoding 未知，或解压输出超过 ``max_size``，
                        或声明 zstd 但本机无 zstandard。
    :raises zlib.error: deflate 数据损坏/截断。

    实现注意：必须用 ``max_length`` 限流每次 ``decompress()`` 的输出，
    并排空 ``unconsumed_tail``。直接按 input 分块
    (``for i in range(0, len(data), CHUNK)``) 是错的 —— 输出超限时
    未消费的 input 会滞留在 ``unconsumed_tail`` 被静默丢弃，导致炸弹
    被截断成一段损坏数据而不报错。
    """
    if encoding in (None, "", ENCODING_IDENTITY):
        return data
    if encoding == ENCODING_ZSTD:
        # OPT-05 Phase 1: zstd 解压路径
        if not _HAS_ZSTD:
            raise ValueError("zstd content received but zstandard not installed")
        if not data:
            raise ValueError("empty zstd payload")
        try:
            return _raw_decompress_zstd(data, max_size)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"zstd decompress failed: {e}") from e
    if encoding != ENCODING_DEFLATE:
        raise ValueError(f"unsupported content encoding: {encoding!r}")
    if not data:
        # 空输入不可能是合法 deflate 流；与旧 zlib.decompress(b"") 一致抛错
        raise zlib.error(
            "Error -5 while decompressing data: incomplete or truncated stream"
        )
    d = zlib.decompressobj()
    out_parts: list[bytes] = []
    total = 0
    feed = data
    while True:
        # max_length 限流单次输出；超限的 input 留在 unconsumed_tail
        chunk = d.decompress(feed, DECOMPRESS_CHUNK)
        feed = d.unconsumed_tail
        if chunk:
            total += len(chunk)
            if total > max_size:
                raise ValueError(
                    f"decompressed size exceeds limit ({max_size} bytes)"
                )
            out_parts.append(chunk)
        if d.eof:
            break
        if not feed and not chunk:
            # 输入已耗尽但流未结束 (截断数据)：跳出循环，
            # 下面的 eof 检查会抛 zlib.error
            break
    if not d.eof:
        # 流在结束标记前断掉 —— 截断/损坏数据。必须报错而非静默返回
        # 部分数据 (下游可能把残缺 JSON 当完整解析)。
        raise zlib.error(
            "Error -5 while decompressing data: incomplete or truncated stream"
        )
    tail = d.flush()
    total += len(tail)
    if total > max_size:
        raise ValueError(f"decompressed size exceeds limit ({max_size} bytes)")
    out_parts.append(tail)
    return b"".join(out_parts)


# ---------------------------------------------------------------------------
# 协议头 helpers (Daemon <-> VPS <-> PC 透传)
# ---------------------------------------------------------------------------

def request_headers(data: bytes, filename: str | None = None) -> tuple[bytes, dict[str, str]]:
    """发送端一站式：判定 + 压缩 + 生成请求头。

    返回 (待发送字节, headers)。headers 含
    ``Content-Encoding: zstd|deflate`` (压缩时)；不压缩时返回空 dict
    (VPS/PC 按缺省 identity 处理)。

    用法 (http.client)::
        payload, hdrs = request_headers(body_bytes, "app.log")
        headers.update(hdrs)
        headers["Content-Length"] = str(len(payload))
    """
    if should_compress(data, filename):
        payload, encoding = compress(data)
        if encoding in (ENCODING_DEFLATE, ENCODING_ZSTD):
            return payload, {"Content-Encoding": encoding}
    return data, {}


def response_encoding(headers) -> str:
    """从响应头解析载荷编码。

    :param headers: 大小写不敏感的 mapping (如 http.client.HTTPMessage)
                    或普通 dict。兼容 ``X-Content-Encoding``。
    :return: "zstd"、"deflate" 或 "identity"。
    """
    get = getattr(headers, "get", None)
    if get is None:
        return ENCODING_IDENTITY
    enc = get("Content-Encoding") or get("content-encoding")
    if not enc and hasattr(headers, "get_all"):
        # HTTPMessage 风格
        enc = headers.get("Content-Encoding")
    if not enc:
        # 兼容旧端的 X-Content-Encoding
        enc = get("X-Content-Encoding") or get("x-content-encoding")
    enc = (enc or "").strip().lower()
    if enc in ("", ENCODING_IDENTITY):
        return ENCODING_IDENTITY
    if enc in (ENCODING_DEFLATE, ENCODING_ZSTD):
        return enc
    raise ValueError(f"unsupported content encoding: {enc!r}")
