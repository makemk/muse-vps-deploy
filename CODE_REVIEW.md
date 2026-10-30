# v2 代码交叉评审报告

**日期**: 2026-10-19
**范围**: `~/workspace/muse-vps-deploy/mused/` + `~/workspace/muse-vps-deploy/vps-auth-patch/`
**方法**: 只读代码，接口一致性检查

---

## 发现的问题（按严重性排序）

### 🔴 P1: `fastauth.py` 与 `fast_auth.py` 重复文件

> ✅ **已修复 (2026-10-19)**：`vps-auth-patch/fastauth.py` 已不存在（确认删除），
> `fast_auth.py` 为唯一实现。另修复了残留引用：
> `benchmarks/bench_fastauth.py` 的 `from fastauth import FastAuthEngine`
> 改为 `from fast_auth import`（此前 import 恒失败、benchmark 静默走 mock，
> 从未测到真实现；现已验证走 REAL 路径），docstring/输出文案及
> `benchmarks/README.md` 示例引用同步修正。

**位置**: `vps-auth-patch/fastauth.py` vs `vps-auth-patch/fast_auth.py`

**问题**: 两个文件内容完全相同（347 行，diff 无差异），是重复文件。

**影响**: 
- 混淆：开发者不知道该改哪个
- 测试 `test_fastauth.py` 明确 `from fast_auth import`（带下划线），所以 `fastauth.py`（无下划线）是多余的
- 如果将来两个文件被分别修改，会导致行为不一致

**建议**: 删除 `vps-auth-patch/fastauth.py`，只保留 `fast_auth.py`

---

### 🟡 P2: `mused.py` 未集成 `compress.py`

> ✅ **已修复 (2026-10-19)**：
> - `mused.py` 新增 try/except import（pool 模式）：
>   `from compress import request_headers, response_encoding, decompress`，
>   fallback `from mused.compress import ...`；另补 `import zlib`。
> - `Upstream.request()` 发送路径：`body` 非空且 `compress_enabled` 时调用
>   `request_headers(payload)`（Content-Length 按压缩后长度重算）；
>   接收路径：按 `response_encoding(resp.headers)` 调用 `decompress()`，
>   损坏的 deflate 记 warning 并抛 RuntimeError（绝不静默交付坏数据）。
> - 新增配置项 `compress_enabled`（默认 **true**），DEFAULTS 与
>   `config.json.example` 均已补充。
> - 新增回归测试 `mused/test_mused_compress.py`（6 项全过）；
>   既有 `test_compress.py` 等 60 项仍全过。
> - ⚠️ 部署注意：默认 true 要求 PC 插件先实现解压侧
>   （`DECOMPRESS_SPEC.md`，Phase 2 未落地）；插件未就绪前部署时请将
>   `compress_enabled` 设为 false（DEFAULTS/config.example 均有注释说明）。

**位置**: `mused/mused.py` 无 `compress` import；`mused/compress.py` 已交付（25/25 测试通过）

**问题**: 
- `compress.py` 实现了 `SmartCompressor`（should_compress/compress/decompress）
- 但 `mused.py` 的 import 块（line 24-51）中没有 `from compress import ...`
- `mused.py` 的 `Upstream.request()` 没有调用压缩逻辑
- 集成代理的报告中明确写了："集成进 mused.py 本体是下一步（本任务只交付模块+测试，未改 mused.py）"

**影响**: 压缩功能已开发但未启用，v2 的带宽优化收益为零

**建议**: 
1. 在 `mused.py` 添加 `try/except` import（仿照 microbatch 的模式）
2. 在 `Upstream.request()` 的发送路径调用 `SmartCompressor.request_headers()`
3. 在响应路径调用 `response_encoding()` 解压
4. 添加 `compress_enabled` 配置项（默认 false，渐进启用）

---

### 🟡 P3: `pool.py` 参数命名混淆 - `return_connection(self, pc, ...)`

**位置**: `mused/pool.py:153`

```python
def return_connection(self, pc, is_broken=False):
    """Return a connection to the pool."""
    if self._closed or is_broken:
        self._discard(pc)
        return
    pc.last_used_at = time.monotonic()
    self._idle.put(pc)
```

**问题**: 参数名 `pc` 极易误解为 "PC"（目标电脑），实际是 pooled connection 对象。

**影响**:
- 可读性差，新开发者会困惑
- `mused.py:286` 调用处 `pc = self._pool.get_connection()` 同样用 `pc` 变量名，加剧混淆
- 与领域术语冲突（pc = 目标电脑是本项目的核心概念）

**建议**: 重命名为 `conn`：
```python
def return_connection(self, conn, is_broken=False):
```

---

### 🟢 P4: `mused.py` 的 `tls_resume` import 无 fallback

> ✅ **已修复 (2026-10-19)**：改为 try/except fallback 模式
> （`from mused.tls_resume import ...`），与 pool/compress 一致；
> 直接运行与 `python -m` 包模式两种 import 方式均已验证通过。

**位置**: `mused/mused.py:46`

```python
from tls_resume import ResumableHTTPSConnection, TLSSessionCache
```

**问题**: 
- `microbatch` 有 try/except fallback（line 38-44）
- `pool` 有 try/except fallback（line 48-51）
- 但 `tls_resume` 是裸 import，无 fallback

**影响**: 如果以不同方式运行（如 `python -m`），会 ImportError 崩溃

**建议**: 统一 import 模式：
```python
try:
    from tls_resume import ResumableHTTPSConnection, TLSSessionCache
except ImportError:
    from mused.tls_resume import ResumableHTTPSConnection, TLSSessionCache
```

---

### 🟢 P5: `dedup.py` 与 `fast_auth.py` 的集成点未在 `dispatch_patch.py` 中声明

> ✅ **已修复 (2026-10-19)**：`dispatch_patch.py` 新增 "V2 MODULES" 章节，
> 声明三模块的部署方式（整文件拷贝到 `/usr/local/bin/`，非代码合并）与
> 集成锚点：V2-A fast_auth（import + 鉴权块替换，含 client_ip 取法与
> allowed_ips/超管语义）、V2-B dedup（import + /cmd 处理器 + WSS 断开回调）、
> V2-C wss_scheduler（import + PC 上/下线 + 发送泵 + 背压），以及 V2 部署
> 7 步骤与回滚说明。已验证 `deploy-auth-patch.sh` 的正则锚点
> （`# NEW:`×1、`# INSERT:`×3）未被破坏，py_compile 通过。

**位置**: `vps-auth-patch/dispatch_patch.py`

**问题**: 
- `dispatch_patch.py` 是部署脚本用来定位插入点的"锚点说明"文件
- 但 grep 显示它没有引用 `fast_auth`、`dedup`、`wss_scheduler`
- 这意味着部署脚本 `deploy-auth-patch.sh` 可能不知道要合并这些新模块

**影响**: v2 新功能（FastAuthEngine、去重、WSS 调度）可能不会被部署到 VPS

**建议**: 
1. 检查 `deploy-auth-patch.sh` 是否已更新为处理 v2 模块
2. 如果没有，在 `dispatch_patch.py` 中添加新模块的集成说明
3. 或者确认 v2 采用不同的部署方式（如独立文件拷贝而非代码合并）

---

## 验证通过的项 ✅

### 1. mused.py 的模块 import（除 compress 外）
- ✅ `microbatch`: `from microbatch import MicroBatcher, is_read_only` - 正确，有 fallback
- ✅ `tls_resume`: `from tls_resume import ResumableHTTPSConnection, TLSSessionCache` - 正确（但无 fallback，见 P4）
- ✅ `pool`: `from pool import ConnectionPool` - 正确，有 fallback
- ✅ 所有三个模块在 `mused/` 目录下可正常 import

### 2. FastAuthEngine 接口
- ✅ `FastAuthEngine.authenticate_and_authorize(raw_token, client_ip, req_target)` 
- ✅ 返回 `(http_status, payload)`: (200, target) / (401, msg) / (403, msg) / (503, msg)
- ✅ 与调用方期望一致（HTTP 状态码 + 目标/错误信息）
- ✅ 类可正常实例化，方法可调用

### 3. DedupManager 接口
- ✅ `DedupManager.get_or_create(request_id: str, controller: str, target_pc: str) -> tuple[bool, RequestRecord]`
- ✅ 返回 `(is_new, record)`，文档清晰
- ✅ 参数与 `dedup.py` 模块 docstring 中的集成示例一致

### 4. ConnectionPool 接口
- ✅ `ConnectionPool(connect_fn, min_size=4, max_size=16, ...)` - 构造函数签名正确
- ✅ `get_connection(timeout=None)` - mused.py:286 调用正确
- ✅ `return_connection(pc, is_broken=False)` - mused.py:293,297 调用正确（但参数名混淆，见 P3）
- ✅ `mused.py:226` 实例化参数 `connect_fn=self._new_verified_conn, min_size=4, max_size=16` 与签名匹配

### 5. 配置项
- ✅ `microbatch_enabled`: DEFAULTS 有（False），config.json.example 有（false），mused.py:411 检查 `MicroBatcher is None`
- ✅ 所有基础配置项（vps_host, vps_port, sni, ca_cert_path, token_file, socket_path, heartbeat_interval, log_level）在 DEFAULTS 和 example 中一致

### 6. vps-auth-patch 模块可 import
- ✅ `from fast_auth import FastAuthEngine` - 成功
- ✅ `from dedup import DedupManager` - 成功  
- ✅ `from wss_scheduler import WSSScheduler` - 成功

---

## 总结

| 严重性 | 数量 | 说明 | 状态 |
|--------|------|------|------|
| 🔴 P1 | 1 | 重复文件 `fastauth.py`，删除即可 | ✅ 已修复 |
| 🟡 P2 | 1 | `compress.py` 未集成到 `mused.py`，功能未启用 | ✅ 已修复 |
| 🟡 P3 | 1 | `return_connection` 参数命名 `pc` 混淆 | ⬜ 未处理（不在本次任务范围） |
| 🟢 P4 | 1 | `tls_resume` import 无 fallback | ✅ 已修复 |
| 🟢 P5 | 1 | v2 新模块在 `dispatch_patch.py` 中无集成说明 | ✅ 已修复 |

**最紧急**: P1（删除重复文件，1 分钟）+ P2（集成压缩，或明确推迟到 Phase 2）

**建议修复顺序**: P1 → P2（决策：集成还是推迟）→ P3 → P4 → P5（确认部署方式）
