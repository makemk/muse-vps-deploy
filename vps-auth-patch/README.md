# VPS 鉴权补丁（vps-auth-patch）

给 VPS 上 `/usr/local/bin/muse-bridge.py` 加 controller-token 鉴权层的补丁包。

## 用途

让 VPS 的 `/muse-api/` 接口支持按 muse+PC 的细粒度鉴权：

- **双层鉴权**：先验 controller token（`muse_` 开头），失败再降级到 legacy token（兼容旧客户端）
- **目标权限检查**：`/cmd`、`/batch`、`/file` 三个 handler 校验 `target` 是否在 token 的 `pcs` 列表内，越权返回 403
- **超管 token**：`pcs=["*"]`，但调用时 `target` 必须显式指定（防误发默认机器）

## 文件清单

| 文件 | 说明 |
|------|------|
| `auth_patch.py` | 新增代码：`load_controllers()`、`verify_controller_token()`、`check_target_permission()`、`resolve_target()` + 全局变量（**已做安全加固，见 SECURITY_REVIEW.md**） |
| `dispatch_patch.py` | 补丁指令：`handle_connection()` 内 5 个插入点的精确锚点说明（**已做安全加固**） |
| `muse-bridge-orig.py` | VPS 原文件副本（637 行，参考用，**禁止部署**） |
| `IMPLEMENTATION.md` | 完整实施说明：插入点行号、测试报告、原代码结构问题 |
| `SECURITY_REVIEW.md` | **安全加固审查报告**：7 个问题/修复、测试结果、残留风险、nginx 加固 |
| `test_security.py` | 安全测试 30 项（`python3 -m pytest test_security.py -q`） |
| `dedup.py` | **request_id 去重状态机**：IN_FLIGHT/COMPLETED/UNCERTAIN 两阶段，at-most-once 执行 + 幂等结果重放（Gemini 4 评审 Q4/§5 落地） |
| `test_dedup.py` | 去重测试 22 项（去重/并发挂载/LRU/TTL/UNCERTAIN/集成模式） |
| `nginx-muse-api-secure.conf.example` | nginx 加固配置：限流 + 去 query-string 日志 + body 上限 |
| `*.orig-pre-hardening` | 加固前的原版备份 |

## 安全状态

2026-10-19 安全审查完成：7 个问题（2 中危 H1/H2、5 低危 H3–H7）**全部修复**，
`test_security.py` 30/30 通过，加固版与原版在合法输入下行为零差异。
部署时 `PATCH_DIR` 必须指向**本目录**（加固版），不要用 `/tmp/bridge-auth-impl` 的旧版：

```bash
PATCH_DIR=~/workspace/muse-vps-deploy/vps-auth-patch ../scripts/deploy-auth-patch.sh --check-only
```

## 改动量

约 80 行新增，0 行删除，5 个插入点。无需新 import（`hashlib`、`hmac`、`json`、`os`、`sys` 原文件已有）。

## 测试方法

```bash
cd ~/workspace/muse-vps-deploy/vps-auth-patch
python3 - <<'EOF'
# 对原文件做合并，验证插入点命中
import re
src = open('muse-bridge-orig.py').read()
patch = open('auth_patch.py').read()
# 提取函数名并确认原文件无冲突
for fn in ['load_controllers', 'verify_controller_token', 'check_target_permission', 'resolve_target']:
    assert f'def {fn}' not in src, f'{fn} 已存在于原文件'
    assert f'def {fn}' in patch, f'{fn} 在 patch 中缺失'
print('OK: 4 个函数无命名冲突')
EOF
```

完整合并测试由 `../scripts/deploy-auth-patch.sh --check-only` 在部署前执行（见 scripts/README.md）。

## 去重状态机（dedup.py）

VM daemon 每次调用生成 UUIDv4 `request_id`；8471 服务端用 `DedupManager`
做两阶段去重，保证**命令执行 at-most-once**、结果获取幂等可重放：

- `get_or_create(id, controller, target)` → `(is_new, record)`：新 id 建 IN_FLIGHT；
  重复 id 若 COMPLETED 直接回缓存，若 IN_FLIGHT 用 `wait_for` 挂载等待，
  若 UNCERTAIN 回 504（WSS 中途断开，绝不重发）
- `complete(id, status, body)`：转 COMPLETED，唤醒等待者；body >256KB 只存状态不存包体
- `mark_uncertain(id)` / `mark_uncertain_for_target(pc)`：PC 的 WSS 断开时调用
- 内存三重上限：5000 条 / 64MB 总量 / 300s TTL（从完成时刻起算）；IN_FLIGHT 永不淘汰
- 单 asyncio 事件循环下同步方法即原子；纯标准库

在 `/cmd` handler 入口按 `dedup.py` 模块 docstring 的集成模式接入（约 20 行）。

## 部署

不要手动改 VPS 文件。用 `../scripts/deploy-auth-patch.sh`（自动备份、合并、语法检查、重启服务、支持回滚）。

## v2: FastAuthEngine（`fast_auth.py`，2026-10-19）

Gemini-4 第二轮评审 §6.4 的生产级实现：复合决策缓存，把
`(token, client_ip, target)` 的完整鉴权链（SHA-256 查表 → allowed_ips →
target 权限）压缩为一次 `blake2b` + 一次 dict 查找。

- **热路径约 3.0μs/次**（20 万次实测均值），未缓存路径约 4.0μs
- 内存零明文：缓存键为 `blake2b(token, key=16B进程随机盐)`，不可逆
- 只缓存 200 通过的决策；401/403 永不缓存（防缓存投毒）
- 决策缓存 1024 条上限（满则清空）；配置变更（mtime）时原子切换 + 清缓存，
  吊销 ~200ms 内全局生效
- 继承 v1 的 H1/H2/H3 加固：严格条目校验、非安全权限/损坏无缓存时 fail-closed (503)
- 新增 `allowed_ips` 可选字段（CIDR 列表，热加载时预编译）
- `verify_controller_token_fast(token, client_ip="")` 与 v1 同接口；
  有 allowed_ips 限制但未提供 client_ip 时拒绝（fail safe）
- `authenticate_and_authorize_fast(token, client_ip, target)` 为 v2 全路径入口，
  返回 `(200|401|403|503, resolved_target_or_msg)`

测试：`python3 -m pytest test_fastauth.py -v`（42 项全过，含性能基准）。

部署注意：`scripts/deploy-auth-patch.sh` 目前只提取 auth_patch.py +
dispatch_patch.py；上 v2 时需扩展 merger 以合并 fast_auth.py，或将
dispatch_patch.py 的鉴权调用点改为 `authenticate_and_authorize_fast()`。

## v2: Multiplex /batch handler（`batch_multiplex.py`，2026-10-19）

实现 `mused/microbatch.py` 的服务端契约（P0-3）：VM daemon 发
`POST /muse-api/batch`，body 为 `{"batch_id": "<uuid>", "requests":
[{"id","method","path","body"}, ...]}`，VPS 把 batch 扇出为 N 条独立
WSS 消息发往 PC，再把每个子请求的结果拼回
`(200, '[{"id","status","body"}, ...]')`。

- **与 legacy 共存的分发规则**：`is_multiplex_batch()`（body 是 dict 且带
  `requests` 列表）→ 复用 handler；`is_legacy_shell_batch()`（带 `commands`
  列表）→ 原 shell batch 逻辑，不变。两者都命中时 `requests` 优先；
  建议的接线顺序写在 `batch_multiplex.py` 模块 docstring。
- **去重粒度**：`dedup.batch_sub_key(batch_id, sub_id)` → `"batch:<bid>:<sid>"`；
  同 batch_id 内重试重放/挂载，换 batch_id 即新执行。
- **铁律**：传输健康时外层恒 200；单个子请求失败绝不拖累整批；
  子请求间 `asyncio.gather` 并发，逐个永不抛异常的包装；子执行
  at-most-once（`execute` 抛异常 → 该子记 UNCERTAIN → 504，
  绝不重发）。
- **接线缝**：`handle_batch_multiplex(body, *, controller, target_pc,
  dedup, execute, timeout=300.0)`，其中 `execute` 是 muse-bridge.py 提供
  的异步回调：`await execute(sub_id, composite_request_id, method, path,
  sub_body_dict) -> (status:int, body:bytes|str)`，负责单次子请求的
  WSS 往返（path 原样透传，scheduler 会归一化 "/cmd" 类路径）。
  `MAX_SUB_REQUESTS = 100` 为服务端硬上限。

测试：`python3 -m pytest test_batch_multiplex.py test_dedup.py -q`
（48 项全过，含已有 dedup 回归）。
