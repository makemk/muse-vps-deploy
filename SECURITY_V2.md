# v2 新组件安全审计报告

**审计日期**: 2026-10-19
**修复日期**: 2026-10-19（C1、D1、S1 已修复并测试）
**审计范围**: v2 新增的 7 个组件
**审计方法**: 静态代码审查 + 威胁建模

---

## 审计结论总览

| 组件 | 文件 | 发现问题 | 最高等级 | 状态 |
|------|------|----------|----------|------|
| FastAuthEngine | `vps-auth-patch/fast_auth.py` | 0 | — | ✅ |
| 请求去重状态机 | `vps-auth-patch/dedup.py` | 2 | 中 | ✅ 已修复 |
| WSS 优先级调度 | `vps-auth-patch/wss_scheduler.py` | 2 | 中 | ✅ 已修复 |
| 连接池 | `mused/pool.py` | 1 | 信息 | — |
| TLS 会话复用 | `mused/tls_resume.py` | 0 | — | ✅ |
| 微批聚合 | `mused/microbatch.py` | 1 | 低 | — |
| 应用层压缩 | `mused/compress.py` | 1 | **高** | ✅ 已修复 |

**总计: 7 个问题 (1 高, 2 中, 3 低, 1 信息)；其中 3 个已修复（C1、D1、S1）**

---

## 高严重性问题

### C1: `decompress()` 无输出大小限制 → 解压炸弹 DoS ✅ 已修复

**文件**: `mused/compress.py` → `decompress()`
**等级**: 高
**状态**: ✅ 已修复（2026-10-19），9 项新测试全过

**描述**:
`decompress()` 直接调用 `zlib.decompress(data)`，对解压后的输出大小没有任何限制。攻击者（或被攻陷的 VPS/中间人）可以发送一个经典的 zip 炸弹（如 42KB 压缩 → 5GB 解压），导致 Daemon 进程内存耗尽崩溃。

**为什么现有防护不够**:
- `should_compress()` 的 1KB 探针只在**压缩端**做判定，解压端完全信任 `Content-Encoding: deflate` 头
- zip 炸弹本身是高度可压缩数据，会**通过**探针检测（这正是炸弹的设计目的）
- 后缀黑名单和魔数检查只防"不值得压缩"，不防"恶意压缩"

**攻击场景**:
1. 攻击者攻陷 VPS 或劫持 VM→VPS 链路
2. 返回 `Content-Encoding: deflate` + 42KB 的炸弹载荷
3. Daemon 调用 `decompress()` → 尝试分配 5GB → OOM → Daemon 崩溃 → 所有 PC 控制中断

**修复建议**:
```python
MAX_DECOMPRESSED_SIZE = 64 * 1024 * 1024  # 64MB，与 dedup 的上限对齐

def decompress(data: bytes, encoding: str | None,
               max_size: int = MAX_DECOMPRESSED_SIZE) -> bytes:
    if encoding in (None, "", ENCODING_IDENTITY):
        return data
    if encoding == ENCODING_DEFLATE:
        d = zlib.decompressobj()
        out = []
        total = 0
        # 分块解压，每块检查累计大小
        CHUNK = 65536
        for i in range(0, len(data), CHUNK):
            chunk = d.decompress(data[i:i+CHUNK], CHUNK)
            total += len(chunk)
            if total > max_size:
                raise ValueError(f"decompressed size exceeds {max_size}")
            out.append(chunk)
        tail = d.flush()
        total += len(tail)
        if total > max_size:
            raise ValueError(f"decompressed size exceeds {max_size}")
        out.append(tail)
        return b"".join(out)
    raise ValueError(f"unsupported content encoding: {encoding!r}")
```

**注意**: PC 插件侧的解压代码必须做同样的防护（Node.js 的 `zlib.inflateSync` 同样无限制）。

**修复实现** (2026-10-19):
- `decompress(data, encoding, max_size=MAX_DECOMPRESSED_SIZE)`，`MAX_DECOMPRESSED_SIZE = 64MB`
- `zlib.decompressobj()` 流式分块解压，每块 `DECOMPRESS_CHUNK=64KB` 上限输出，累计超限抛 `ValueError`
- 关键修正：审计建议稿中的 `for i in range(0, len(data), CHUNK)` 实现有 bug —— 输出超限时未消费的 input 滞留在 `unconsumed_tail` 被静默丢弃，炸弹会被截断成损坏数据而不报错。正确实现必须用 `max_length` 限流 + 排空 `unconsumed_tail` 循环
- 截断/损坏的流：`d.eof` 检查，抛 `zlib.error`（不静默返回部分数据）
- 空 deflate 输入抛 `zlib.error`（与旧 `zlib.decompress(b"")` 行为一致）
- 测试：`test_compress.py` 新增 9 项（炸弹小/默认上限拒绝、边界精确值、200KB 合法 round-trip、截断不挂死、空输入、max_size 透传），34/34 通过

---

## 中严重性问题

### D1: `DedupManager` 的 IN_FLIGHT 记录无数量上限 → 内存耗尽 ✅ 已修复

**文件**: `vps-auth-patch/dedup.py` → `DedupManager`
**等级**: 中
**状态**: ✅ 已修复（2026-10-19），8 项新测试全过

**描述**:
`_enforce_limits()` 只淘汰已结算（COMPLETED/UNCERTAIN）记录，IN_FLIGHT 记录**永不**被淘汰（设计如此，防止丢失在途请求）。但没有任何上限限制 IN_FLIGHT 的数量。

**攻击场景**:
持有有效 token 的攻击者发送大量永不完成的请求（如 `/cmd` 执行 `sleep 3600`），每个请求创建一个 IN_FLIGHT 记录（含 `asyncio.Event` 对象）。10 万条记录 ≈ 20MB+，100 万条可耗尽 VPS 内存。

**缓解因素**:
- 每个 IN_FLIGHT 对应一个真实下发到 PC 的执行，PC 端的执行能力是天然瓶颈
- 但攻击者可以针对多个 PC 并行，或利用 `/batch` 放大

**修复建议**:
```python
MAX_IN_FLIGHT_TOTAL = 10000
MAX_IN_FLIGHT_PER_PC = 1000

def get_or_create(self, request_id, controller, target_pc):
    # 在创建前检查
    in_flight_total = sum(1 for r in self._records.values()
                          if r.status == IN_FLIGHT)
    if in_flight_total >= MAX_IN_FLIGHT_TOTAL:
        raise OverloadError("too many in-flight requests")
    # 按 PC 检查...
```

超限时返回 `503 Service Unavailable`（而不是 500），调用方可重试。

**修复实现** (2026-10-19):
- `MAX_IN_FLIGHT_TOTAL = 10000`（`DedupManager.__init__` 可覆盖为 `max_in_flight`，便于测试）
- `OverloadedError(RuntimeError)`，`get_or_create()` 在**实际创建新记录前**检查并抛出；调用方（/cmd handler）应转为 HTTP 503
- O(1) 计数器 `_in_flight_count`（而非每次 O(n) 扫描）：创建时 +1，`complete()`/`mark_uncertain()`/`_remove()` 时 -1（含防漂移钳制）
- 设计决策：去重命中（已存在的 request_id，含重试）**不计入**上限且永不抛异常 —— 重试不产生新内存，拒绝重试会破坏 at-most-once 的重挂载语义
- `stats()` 新增 `in_flight`（计数器）与 `in_flight_limit`
- 模块 docstring 的 /cmd 集成示例已更新（含 `except OverloadedError → 503`）
- 测试：`test_dedup.py` 新增 8 项（上限抛异常、重试豁免、complete/uncertain 释放、混合 churn 计数器一致性、默认值），30/30 通过

### S1: `WSSScheduler` 队列无深度上限 → 内存耗尽 ✅ 已修复

**文件**: `vps-auth-patch/wss_scheduler.py` → `enqueue()`
**等级**: 中
**状态**: ✅ 已修复（2026-10-19），7 项新测试全过

**描述**:
`enqueue()` 直接 `append` 到 deque，没有任何队列深度检查。持有有效 token 的攻击者可以向某台 PC 的队列塞入数百万条消息，VPS 内存被耗尽。

**攻击场景**:
攻击者用合法 token 以 10k/s 的速度 enqueue `/cmd` 请求，目标 PC 执行速度跟不上（或 PC 已离线但未 unregister）。1 小时 = 3600 万条消息 ≈ 数 GB 内存。

**修复建议**:
```python
MAX_QUEUE_DEPTH = 10000  # per-PC, per-queue

def enqueue(self, pc_id, msg_type, payload):
    ch = self._channels.get(pc_id)
    if ch is None:
        raise KeyError(f"PC not registered: {pc_id!r}")
    target_queue = ch.bulk if msg_type in BULK_TYPES else ch.high_prio
    if len(target_queue) >= MAX_QUEUE_DEPTH:
        raise QueueFullError(f"queue depth exceeded for {pc_id}")
    # ... existing append logic
```

超限时调用方应返回 `503` 给 VM Daemon，Daemon 可退避重试。

**修复实现** (2026-10-19):
- `WSSScheduler.MAX_QUEUE_DEPTH = 10000`（类属性，可按实例覆盖）
- `QueueFullError(RuntimeError)`，`enqueue()` 在 append 前检查目标队列长度，超限抛出
- 上限为 per-queue（high_prio 与 bulk 各 10000）、per-PC（各 PC 独立）
- 测试：`run_wss_tests.py` 新增 7 项（high/bulk 上限、per-queue 独立、per-PC 隔离、drain 后恢复、默认值、异常类型），21/21 通过

---

## 低严重性问题

### S2: `/file` 在持续 `/cmd` 洪流下可能饿死

**文件**: `vps-auth-patch/wss_scheduler.py` → `get_next_message()`
**等级**: 低

**描述**:
`get_next_message()` 总是优先排空 `high_prio` 队列。如果攻击者（或 bug）持续以高速率发送 `/cmd`，`bulk` 队列中的 `/file` 分片可能永远得不到发送机会。

**缓解因素**:
- 正常 AI 工作流是突发式，不是持续洪流
- `/cmd` 本身受 PC 执行速度限制，不可能无限快

**修复建议** (可选，视威胁模型):
实现加权公平队列：每发送 N 条 high_prio（如 100 条），强制发送 1 条 bulk。或记录 `bulk` 队首等待时间，超过阈值（如 30 秒）则优先发送。

```python
# 在 _PCChannel 中增加
high_sent_since_bulk = 0

# 在 get_next_message 中：
if ch.high_prio and ch.bulk:
    if ch.high_sent_since_bulk >= 100:
        # 强制公平：发送一条 bulk
        ...
```

### D2: `wait_for(timeout=None)` 永久挂起

**文件**: `vps-auth-patch/dedup.py` → `wait_for()`
**等级**: 低

**描述**:
如果调用方传入 `timeout=None`，`asyncio.wait_for` 会无限等待。正常情况下 `done_event` 总会被 `complete()` 或 `mark_uncertain()` 触发，但如果代码有 bug 导致既不 complete 也不 mark_uncertain，等待的协程会永久泄漏。

**修复建议**:
在 `wait_for` 中强制上限：
```python
MAX_WAIT_TIMEOUT = 600  # 10 分钟硬上限
async def wait_for(self, request_id, timeout=None):
    if timeout is None or timeout > MAX_WAIT_TIMEOUT:
        timeout = MAX_WAIT_TIMEOUT
    ...
```

### M1: VPS `/batch` 处理器必须验证外层 Token 权限

**文件**: `mused/microbatch.py`（Daemon 侧已正确，VPS 侧待实现）
**等级**: 低（实现要求，非当前代码 bug）

**描述**:
Daemon 侧的 `MicroBatcher` 正确地按 `(token, target)` 分组，同一 batch 内的子请求共享同一个 token 和 target。但 VPS 侧的 `/batch` 处理器在实现时**必须**：
1. 用外层请求的 Bearer token 做一次完整鉴权（含 target 权限检查）
2. 子请求**继承**外层的 (token, target)，不允许子请求指定不同的 target

如果 VPS 允许子请求携带独立的 `target` 字段而不验证，恶意 Daemon 可以用 A 的 token 发送针对 B 的 PC 的子请求。

**修复建议** (VPS `/batch` 实现时):
```python
# 伪代码
async def handle_batch(request):
    token = extract_bearer(request)
    # 外层鉴权：必须提供 target（batch 不支持自动解析）
    status, target = auth.authenticate_and_authorize(token, client_ip, body.get("target"))
    if status != 200:
        return status
    for sub in body["requests"]:
        # 子请求禁止覆盖 target
        if "target" in sub.get("body", {}):
            return 400, "sub-request must not specify target"
        # 用外层的 target 执行
        dispatch(sub, target)
```

---

## 信息性发现

### P1: `ConnectionPool` 双重归还导致计数器漂移

**文件**: `mused/pool.py` → `return_connection()`
**等级**: 信息

**描述**:
如果调用方 bug 导致对同一个 `PooledConnection` 调用两次 `return_connection()`，`_total` 会被递减两次，导致计数器漂移（可能变负）。文档已注明"调用方必须恰好调用一次"，但没有防御性检查。

**建议** (可选):
在 `PooledConnection` 增加 `_returned` 标志，重复归还时记录警告日志而不重复递减：
```python
class PooledConnection:
    __slots__ = ("conn", "last_used_at", "created_at", "_returned")
    def __init__(self, conn):
        ...
        self._returned = False
```

---

## 已验证安全（无问题）

### FastAuthEngine (`fast_auth.py`)

| 检查项 | 结果 |
|--------|------|
| `_mem_key` 真随机 | ✅ `secrets.token_bytes(16)`，密码学安全 |
| blake2b 抗碰撞 | ✅ 128-bit 输出 + 16 字节随机密钥，攻击者无法预测/伪造 tag |
| 缓存投毒 | ✅ 只缓存 200 结果，401/403 永不入缓存；攻击者无法用随机 token 污染 |
| 计时攻击 | ✅ 热路径是 dict 查找，key 含不可预测的 MAC，无 token 信息泄漏 |
| 内存零明文 | ✅ 原始 token 永不作为 dict key 或存储值 |
| IP 伪造 | ✅ `client_ip` 由调用方传入；VPS 需从 TCP 或可信的 X-Real-IP 获取（部署配置责任） |
| 并发安全 | ✅ 单线程 asyncio，无 await 穿插，原子操作 |

### TLS 会话复用 (`tls_resume.py`)

| 检查项 | 结果 |
|--------|------|
| 会话重放 | ✅ 0-RTT 从未启用；会话复用只缩短握手，不提前发送应用数据 |
| 会话绑定 | ✅ 由服务端（nginx）通过 ticket 加密密钥管理；客户端 ticket 失效时自动回退到完整握手 |
| 线程安全 | ✅ `TLSSessionCache` 全锁保护 |
| TLS 1.3 坑 | ✅ 正确在 `getresponse()` 后提取 session，而非 `connect()` 后 |

### 微批聚合权限隔离 (`microbatch.py`)

| 检查项 | 结果 |
|--------|------|
| 跨 token 合并 | ✅ `_dispatch_pending` 按 `p.token` 分组，不同 token 永不合并 |
| 跨 target 合并 | ✅ pending 队列按 target 隔离，batch 内子请求共享同一 target |
| 不可批请求 | ✅ `batchable=False` 的请求永远走单发路径 |

---

## 修复优先级

1. **立即修复** (部署前必须): C1（解压炸弹）✅ 已修复
2. **部署窗口期修复**: D1、S1（队列/IN_FLIGHT 上限）✅ 已修复
3. **后续迭代**: S2、D2、M1（VPS /batch 实现时）、P1

---

## 附录：审计未覆盖范围

- `mused.py` 主体（v1 已审计，见 `SECURITY_REVIEW.md`）
- `auth_patch.py` / `dispatch_patch.py`（v1 已审计并加固）
- PC 插件侧代码（Node.js，需单独审计；特别注意其解压实现也需要 C1 的防护）
- Nginx 配置（已在 `nginx-muse-api-secure.conf.example` 中覆盖基础项）
- 网络传输层（依赖 TLS + VPS CA 校验，v1 已验证）
