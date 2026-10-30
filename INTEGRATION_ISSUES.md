# v2 集成问题清单（integration_v2.py 50/50 通过后的人工审查项）

> 生成方式：`python3 integration_v2.py`（50/50 全绿）+ 对各组件接口的代码审查。
> 状态：**P0 必须在 VPS/daemon 接线前修复**，P1 建议同窗口修，P2 可延后。

---

## P0（接线前必须修）

> **2026-10-19 更新：#1、#2、#3 均已修复并测试通过，见各节末尾「✅ 已修复」。**

### #1 WSS scheduler 的 msg_type 命名空间与 VPS 路由层不一致 ⚠️ 最危险

- **现象**：`wss_scheduler.WSSScheduler.enqueue(pc_id, msg_type, payload)` 只识别
  裸名 `"cmd" / "batch" / "file"`（`HIGH_PRIO_TYPES` / `BULK_TYPES`）。
  未知 `msg_type` 按 fail-safe 进 **high_prio**。
- **危险**：VPS 的 HTTP 路由层天然拿到的是路径 `"/cmd" / "/batch" / "/file"`。
  若接线时直接把路径传给 `enqueue()`，`"/file"` 会被判为"未知类型"→
  **进入 high_prio**，`/file` 的 bulk 限流（每 PC 最多 1 个在途分片）被彻底绕过，
  大文件传输重新阻塞 `/cmd`——v2 的核心治理目标落空，且**静默无告警**。
- **修复（二选一，推荐 A）**：
  - A. 在 `enqueue()` / `on_response()` 入口做归一化：`msg_type.lstrip("/")`
     （或显式 `{"​/cmd":"cmd", "/batch":"batch", "/file":"file"}` 映射表）。
  - B. 接线层（muse-bridge.py 的 dispatch）在调用前翻译。
- **验证**：`integration_v2.py` T3/T8 已按裸名测试通过；A 修复后需补一个
  `"/file"` 路径的回归用例。
- ✅ **已修复（2026-10-19）**：`wss_scheduler.py` 新增 `_normalize_msg_type()`
  （`lstrip("/")`），`enqueue()` 入口归一化并存储裸名（`get_next_message()`
  返回裸名供响应匹配），`on_response()` 入口同样归一化。
  `test_wss_scheduler.py` 新增 7 个回归测试（`"/file"` 进 bulk、
  返回名已归一化、bulk 限流不被绕过、路径式响应释放 in-flight 槽位等），
  21/21 通过；`run_wss_tests.py` 21/21 通过。

### #2 FastAuthEngine 未实现 `grace_until`（轮换宽限期语义丢失）

- **现象**：`token-manager/common.py::is_entry_valid` 与加固版 `auth_patch.py`
  都实现了宽限期（`grace_until` 超时前旧 token 仍有效，默认 24h 双活），
  但 `vps-auth-patch/fast_auth.py` 的 `_maybe_reload()` 在预编译记录时
  **只检查 `enabled`**，完全忽略 `grace_until`。
- **影响**：执行 `rotate.py` 后，旧 token 在快路径上会被立即判 401，
  而慢路径（旧 `auth_patch.py`）认为它仍有效——**快慢路径行为分裂**，
  正在用旧 token 的 daemon 会在 200ms 内被踢下线（配置热加载后）。
- **修复**：在 `_maybe_reload()` 预编译时调用与 `common.is_entry_valid`
  一致的宽限期判断（注意 `fast_auth.py` 是自包含的，需把 ~15 行逻辑搬进去，
  或改为 VPS 部署时直接 `import common`——见 #5）。
- ✅ **已修复（2026-10-19）**：双层实现——① `_reload()` 预编译时解析
  `grace_until`（损坏→跳过条目 fail-closed；已过期→跳过；未来→存
  `grace_until_ts`）；② 快路径每次请求做 wall-clock 检查（宽限期超时不改
  文件 mtime，纯预编译会漏），过期→401；宽限期内的 200 **不进**
  `_decision_cache`（否则缓存 200 会撑过宽限期 deadline）；
  `verify_controller_token_fast()` 同样检查。
  **注**：原文"宽限期内快路径误判 401"方向写反了——实际代码是宽限期内
  200 正确、宽限期过后快路径**继续放行**（fail-open），慢路径 401；
  修复后快慢路径与 `common.is_entry_valid` 完全一致。
  `test_fastauth.py` 新增 8 个测试（含"mtime 不变、wall-clock 过期→401"
  关键回归），50/50 通过；性能界未变（快路径 ~1.9μs）。
- **遗留决策点**：空字符串 `grace_until` 按"损坏→fail-closed"处理
  （`common.is_entry_valid` 的真值检查会把它当"无宽限期"），已用测试锁定，
  如需改口径再议。

### #3 `/batch` 多路复用线格式是新协议，VPS/PC 两端都没实现

- **现状**：`mused/microbatch.py` 把凑批打成
  `POST /muse-api/batch {"batch_id": ..., "requests": [{"id","method","path","body"}...]}`
  并期望 `(200, '[{"id","status","body"}, ...]')`。
  这与**旧的** `/batch`（shell 命令数组）**不是同一个接口**。
- **缺口**：
  1. VPS 8471 服务端没有这个多路复用 `/batch` 的 handler；
  2. `dedup.py` 的去重是按单 `request_id` 的，batch 需要按
     `(batch_id, sub_id)` 粒度去重（microbatch.py 文档第 5 条铁律已声明，
     但 `DedupManager` 没有对应 API）；
  3. PC 插件侧不需要改（VPS 负责拆 batch → 逐个走现有 WSS 消息）。
- **修复**：VPS 接线时新增 `handle_batch_multiplex()`：
  解析 → 对每个 sub-request 走 `dedup.get_or_create(sub_id)` →
  逐个 `scheduler.enqueue()` → 聚合为 200 数组返回（单个 sub 失败不影响其他，
  外层永远 200，见 Gemini §4.3 铁律 4）。
- ✅ **已修复（2026-10-19）**：新增 `vps-auth-patch/batch_multiplex.py`
  （~270 行，纯标准库）：`is_multiplex_batch()` / `is_legacy_shell_batch()`
  按 `requests`/`commands` 键分流（两者并存；`requests` 优先）、
  `handle_batch_multiplex(body, *, controller, target_pc, dedup, execute, timeout)`
  → `(200, [{"id","status","body"}])`；子请求经 `asyncio.gather` 并发执行
  （永不向上传播异常、按请求顺序返回）；去重键
  `dedup.batch_sub_key(batch_id, sub_id)` = `"batch:<bid>:<sid>"`
  实现 `(batch_id, sub_id)` 粒度；`execute(sub_id, composite_rid, method, path, body)`
  回调为 muse-bridge.py 接线缝（负责 WSS 调度往返，path 原样透传、scheduler
  已归一化）；envelope 损坏→400，子请求级问题→子项 4xx/5xx、外层恒 200；
  `MAX_SUB_REQUESTS=100` 服务端上限。`dedup.py` 仅增量添加 helper（既有逻辑未动）。
  `test_batch_multiplex.py` 17 个新测试 17/17；`test_dedup.py` 31/31 仍绿；
  另跑 `test_fastauth/test_security/test_wss_scheduler` 101 通过确认无破坏。

---

## P1（建议同窗口修）

### #4 鉴权逻辑三处重复实现，有分裂风险

| 位置 | `allowed_ips` | `grace_until` | 严格 `enabled is True` |
|------|---------------|---------------|------------------------|
| `token-manager/common.py` | ✅ | ✅ | ✅（`is_entry_valid`） |
| `vps-auth-patch/auth_patch.py`（加固版） | ❌（无 IP 校验） | ✅ | ✅ |
| `vps-auth-patch/fast_auth.py` | ✅（预编译） | ❌（见 #2） | ✅ |

- **建议**：VPS 部署时只带一份实现。`common.py` 的 docstring 本就写明
  "VPS 端鉴权 patch 可直接复用本模块"——让 `fast_auth.py` 在部署包里
  `from common import ...`（VPS 上 `/usr/local/lib/muse/` 放一份），
  `auth_patch.py` 的旧慢路径保留作 fallback。
- **注意**：`auth_patch.py`（H1/H3 的 fail-closed、文件权限检查）与
  `fast_auth.py` 的 fail-closed 语义要对齐，当前两份都有各自的
  `_fail_closed`，行为一致但代码重复。

### #5 MicroBatcher 按 `target` 聚批，未按 `(token, target)` 隔离

- **现象**：`MicroBatcher._targets` 以 `target` 为 key。Gemini §4.2 铁律 1
  要求同 `(token, target)` 才合并。
- **实际风险低**：一个 daemon 进程只持一个 controller token，
  同一进程内不会出现同 target 不同 token。但若未来 daemon 支持多 token，
  会串批。
- **修复**：`_state_locked()` 的 key 改为 `(token, target)`（约 3 行）。

### #6 `mused.py` 尚未接入 `pool.py` / `tls_resume.py` / `microbatch.py`

- **现状**：`mused/mused.py` 仍是单连接实现（自己管一个 HTTPSConnection）；
  `pool.py`（连接池）、`tls_resume.py`（会话复用）、`microbatch.py`（凑批）
  都是独立模块，**没有被 `mused.py` import**。
- **接线工作**（daemon 侧，约 80 行）：
  1. `pool = ConnectionPool(connect_fn=..., min_size=4, max_size=16)`，
     其中 `connect_fn` 内部用 `ResumableHTTPSConnection` + 共享
     `TLSSessionCache`（TLS 1.3 ticket 在首次 `getresponse()` 后提取，
     见 `tls_resume.py` 文档）；
  2. `batcher = MicroBatcher(send_fn=...)`，`send_fn` 内做
     `compress` 判定 → `pool.get_connection()` → 发请求 →
     `pool.return_connection()`；
  3. unix-socket 的请求处理函数改为 `batcher.submit(...)`。
- **注意**：`pool.py` 的 `connect_fn` 需处理 egress 代理 CONNECT
 （复用 `mused.py` 现有的代理隧道代码）。

### #7 `dedup.py` 是 asyncio 模型，`fast_auth` / `scheduler` 是同步模型

- **现状**：`DedupManager.wait_for` 是 coroutine（`asyncio.Event`），其余方法同步；
  `FastAuthEngine` 与 `WSSScheduler` 全同步。
- **约束**：VPS 8471 必须是 **单 asyncio event loop**（`dedup.py` 文档已声明
  "mutating methods are synchronous therefore atomic on a single loop"）。
  若 8471 是多线程模型，`DedupManager` 需要加锁。
- **确认项**：接线前确认 `muse-bridge.py` 的 8471 服务是 asyncio 单 loop
  （WSS 部分极大概率是；HTTP 部分需核实）。

---

## P2（可延后）

### #8 `compress.py` 的 `Content-Encoding` 头需要 PC 插件配合

- VPS 对压缩**完全透明**（只透传字节 + 头），但 PC 插件必须学会：
  读 `Content-Encoding: deflate` → `zlib.decompress`。
- 在插件实现前，daemon 侧默认关闭压缩（或只对 `/file` 读开启，
  且插件先升级）。

### #9 集成测试的 MockVPS 未覆盖 `UNCERTAIN → 504` 的真实等待路径

- `integration_v2.py` T2 测了 `mark_uncertain` 的状态机，
  但 T8 的 mock 对 `IN_FLIGHT` 重试直接回 409（真实语义应是 `wait_for`
  挂起等待）。504 路径需在 VPS 联调时用真实 asyncio loop 覆盖。

---

## 接口速查（已对齐 ✅）

| 组件 | 关键接口 | 状态 |
|------|----------|------|
| `fast_auth.FastAuthEngine` | `authenticate_and_authorize(token, ip, target) -> (200\|401\|403\|503, str)` | ✅（#2 已修复：grace_until 双层检查） |
| `dedup.DedupManager` | `get_or_create(rid, ctrl, pc) -> (is_new, rec)` / `complete(rid, status, body)` / `mark_uncertain(rid)` / `async wait_for(rid, timeout)` / `batch_sub_key(bid, sid)` | ✅（#3 新增 batch 复合键） |
| `wss_scheduler.WSSScheduler` | `register_pc / enqueue(pc, type, payload) / get_next_message(pc) / on_response(pc, type)` | ✅（#1 已修复：msg_type 归一化，`"/file"`→`file`） |
| `batch_multiplex`（新增） | `is_multiplex_batch(body)` / `is_legacy_shell_batch(body)` / `async handle_batch_multiplex(body, *, controller, target_pc, dedup, execute, timeout)` → `(200, [{id,status,body}])` | ✅（#3 新模块，17/17 测试） |
| `pool.ConnectionPool` | `ConnectionPool(connect_fn, min_size=4, max_size=16)` / `get_connection() -> PooledConnection` / `return_connection(pc, is_broken)` | ✅ |
| `tls_resume` | `TLSSessionCache.get/set` / `ResumableHTTPSConnection(..., session_getter, session_setter)` | ✅ |
| `microbatch.MicroBatcher` | `MicroBatcher(send_fn)` / `submit(token, target, method, path, body, batchable, request_id) -> (status, body)`（阻塞） | ✅（除 #5） |
| `compress` | `should_compress(data, filename) / compress(data) -> (bytes, enc) / decompress(data, enc)` | ✅ |
| `token-manager` | `allowed_ips`（CIDR 列表，空=不限，坏格式→条目失效） | ✅（SCHEMA.md 已文档化） |
| `nginx/muse-api.conf` | upstream keepalive 32 + `proxy_http_version 1.1` + TLS 会话缓存 | ✅（`test-nginx.sh` 校验） |
