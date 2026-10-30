# VPS 鉴权 Patch — 安全加固审查报告

日期：2026-10-19
范围：`auth_patch.py` + `dispatch_patch.py`（controller-token 鉴权，~80 行新增）
方法：代码审查 + 加固修改 + 30 项安全测试（`test_security.py`）+ 与原版行为一致性校验
结论：发现 **7 个问题**（2 中危、5 低危），**全部 7 个已修复**，测试 30/30 通过，
与原版在合法输入下行为零差异。残留风险 4 项（2 项在 patch 范围外，见 §4）。

---

## 1. 发现的问题与修复

| # | 问题 | 等级 | 修复 | 测试 |
|---|------|------|------|------|
| H1 | `controllers.json` 损坏/不可读时静默降级为空 `{}`，且实现文档误标为 "fail-closed"（实际是 fail-open：legacy token 继续有效）。攻击者若能破坏该文件，可逼系统退回单一全局 token。 | 中 | 引入 `_controllers_fail_closed` 标志：文件**存在但**不可读 / 权限不安全 / 损坏且无有效缓存 → 拒绝**一切**鉴权（含 legacy），返回 503。文件**缺失**（迁移前）→ 仍允许 legacy，不误伤。损坏但有有效缓存 → 继续用缓存（防瞬时写入竞态），恢复后自动清除标志。 | `test_unsafe_permissions_fail_closed`、`test_corrupt_json_no_cache_fail_closed`、`test_corrupt_json_with_cache_keeps_serving`、`test_missing_file_not_fail_closed`、`test_recovery_clears_fail_closed` |
| H2 | 条目校验缺失：`"enabled": "false"`（字符串 truthy）会被当作启用；`pcs` 写成字符串、`muse` 缺失、digest 非 64-hex 等畸形条目静默生效或静默失效。 | 中 | `_valid_controller_entry()` 严格校验：`enabled is True`（布尔）、digest 必须 64-hex、`pcs` 非空 str 列表、`muse` 非空 str；畸形条目加载时跳过并告警。 | `test_enabled_string_true_is_rejected`、`test_pcs_string_rejected`、`test_missing_muse_rejected`、`test_malformed_digest_key_rejected`、`test_mixed_valid_and_invalid_entries` |
| H3 | `controllers.json` 权限无检查。600 之外的权限（如 644）会被静默接受。 | 低 | `load_controllers()` 检查 `st_mode & 0o077`：任何 group/other 位 → 直接 fail-closed（H1），并输出 CRITICAL 告警。 | `test_unsafe_permissions_fail_closed` |
| H4 | `verify_controller_token` 对非字符串输入（`{"token": 123}`）调用 `.strip()` → `AttributeError` → 500。未认证的畸形请求可打 500。 | 低 | 开头加 `isinstance(provided_token, str)` 守卫；dispatch 层再把非 str token 归一化为 `""`。 | `test_nonstring_token_rejected_no_exception` |
| H5 | ① `check_target_permission` 对非字符串 `target` 调用 `.lower()` → 500；② 403 错误信息把控制器的全部 `allowed_pcs` 明文返回（信息泄露）。 | 低 | ① 非 str target → 400（handler 层）/`(False, ...)`（函数层）；② 403 消息通用化，不再披露允许列表。 | `test_nonstring_target_rejected`、`test_403_message_does_not_leak_allowed_list` |
| H6 | 审计日志 `print(f"...{muse_id} -> {target}...")` 未过滤 CR/LF，请求方可注入伪造日志行。 | 低 | 新增 `_log_safe()`：去 CR/LF、截断；全部审计日志行改用。 | `test_log_safe_strips_newlines_and_truncates` |
| H7 | `Authorization` 解析用 `.replace("Bearer ", "")`：token 中任何位置出现该子串都会被破坏（如 `muse_Bearer_x` → `muse_x`），且大小写敏感。 | 低 | 新增 `extract_bearer_token()`：仅头部前缀、scheme 大小写不敏感。 | `test_bearer_prefix_only`（含回归用例） |

**已确认无问题（审查后保留原设计）：**
- Timing attack：token 比较走 `hmac.compare_digest`（64-char hex 定长）；逐条目循环的早退只泄露"有效/无效"，该信息本就由 401/200 暴露。`test_timing_safe_compare_used` 锁定。
- Token 日志泄露：grep 全量检查 + `test_token_never_in_stderr`：没有任何 `print`/日志行插值原始 token；`test_patch_files_contain_no_token_logging` 做静态锁定。
- 401/403 语义：401=无 token/错 token（认证失败），403=token 有效但 target 无权（授权失败），503=鉴权子系统故障（fail-closed）。`test_401_vs_403_semantics` 锁定。
- 并发读：bridge 是单线程 asyncio，`load_controllers()` 内无 await 点，无竞态；20 线程 × 200 次压测零异常。`test_concurrent_reads_no_crash`。
- 超长/特殊字符 token：1MB token 毫秒级拒绝；unicode/NUL/换行不抛异常。
- 热重载：mtime 机制保留并测试（改文件 5 秒后生效）。

---

## 2. 修改清单

- `auth_patch.py`（加固版，原文件备份为 `auth_patch.py.orig-pre-hardening`）：
  新增 `_log_safe`、`_valid_controller_entry`、`controllers_fail_closed`、
  `extract_bearer_token`；改写 `load_controllers`（H1/H2/H3）、
  `verify_controller_token`（H4）、`check_target_permission`（H5）；
  文件自带 import（合并进 bridge 时重复 import 无害，且可独立测试）。
- `dispatch_patch.py`（加固版，原文件备份为 `dispatch_patch.py.orig-pre-hardening`）：
  PATCH 1 增加 fail-closed 检查（503）+ H7 解析；PATCH 2/3/4 增加 target 类型检查（400）+
  通用 403 + `_log_safe` 审计日志。
- 新增 `test_security.py`：30 项，全部通过。
- 新增 `nginx-muse-api-secure.conf.example`：限流 + 去 query-string 日志 + body 上限（见 §3 R4/R5）。
- 行为一致性：加固版 vs 原版在 7 类合法 token × 11 种权限组合 × 3 种 resolve 场景下**零差异**（`parity mismatches: 0`）；dispatch 嵌入代码块 4/4 语法有效。

核心逻辑未动：token 格式、SHA-256 存储、双层鉴权、超管 target 必填、1:1 自动解析等语义与原版一致。

---

## 3. 部署建议（给实施方）

- R1：部署前 `python3 -m py_compile` + 跑 `test_security.py`（30/30）。
- R2：`/etc/muse-bridge/controllers.json` 必须 `600 root:root`，目录 `/etc/muse-bridge/` 设 `700`；否则 bridge fail-closed（503），属预期行为。
- R3：先在 VPS 上用 6 场景验证：错 token→401、无 target 的超管→403、错 target→403、正常→200、损坏 controllers.json→503 一切、删文件→legacy 恢复（迁移态）。
- R4：应用 `nginx-muse-api-secure.conf.example`：`limit_req_zone` 10r/s + burst 20（按实际调），防 token 爆破与 DoS。
- R5：nginx 日志用 `museapi_sanitized` 格式（去掉 query string）。**永远不要用 `?token=` 传 token**——会进 access log 落盘；只用 `Authorization: Bearer` 头。

---

## 4. 残留风险（未在本次修复范围，需决策）

| # | 风险 | 说明 | 建议 |
|---|------|------|------|
| P1 | **原文件无请求体上限**（`muse-bridge-orig.py` ~line 428：`readexactly(content_length)` 无 cap） | 未认证攻击者发 `Content-Length: 9999999999` 可致内存耗尽。这是**原文件**的问题，不在 patch 范围内，但与本次鉴权同属 HTTP 面。 | 在 bridge 加 `MAX_BODY_SIZE`（如 1MB，cmd/file 场景足够），超限直接 413；nginx 层已配 `client_max_body_size 1m` 做第一道。 |
| P2 | `/status` 向**任何**有效 controller token 暴露全部在线客户端列表 | 1:1 绑定的低权 token 可枚举其他 PC 主机名。 | 按 token 过滤 `client_list`，或把 `/status` 拆为公开版（version/uptime）与超管版。 |
| P3 | Legacy 全局 token 在迁移期拥有 `["*"]` 全部权限 | 过渡设计，有明确的收敛计划即无问题；长期不收敛 = 权限模型形同虚设。 | 定下线时间表；审计日志中 legacy 调用单独告警（已实现），定期 review。 |
| P4 | `controllers.json` 热重载无文件锁 | 并发写（两个管理员同时改）可能读到半截 JSON → 触发 H1 的"损坏"分支 → 若无缓存则 fail-closed。 | 写文件时用"写临时文件 + 原子 rename"；或接受 fail-closed 为安全默认值（当前行为）。 |

P1 建议在部署 patch 的同一窗口修复（改动小、风险低）；P2–P4 可排期。

---

## 5. 测试运行

```
$ cd ~/workspace/muse-vps-deploy/vps-auth-patch
$ python3 -m pytest test_security.py -q
30 passed in 0.25s
```
