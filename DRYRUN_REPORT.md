# 部署 Dry-Run 报告（VM 本机模拟，未碰真实 VPS）

日期：2026-10-19 · 模拟环境：`/tmp/mock-vps/`（muse-bridge.py 原文件副本 + 空 controllers.json）
依据：`RUNBOOK_V2.md` Step 0–2

---

## 1. 部署脚本验证

### 1.1 `deploy-auth-patch.sh --check-only`
- 6 处插入点全部命中（globals / 8 个鉴权函数 / 双层鉴权替换 / /file+/batch+/cmd 权限检查）
- `py_compile` 通过
- **结果：PASS**

### 1.2 真实合并（mock）
- 合并后文件编译通过；8 个关键符号齐全（CONTROLLERS_PATH、load_controllers、verify_controller_token、check_target_permission、resolve_target、_controllers_fail_closed、hmac.compare_digest、extract_bearer_token）
- **结果：PASS**

### 1.3 幂等性
- 第二次运行提前退出（"patch already applied"），**不产生新备份**（修复前会）
- **结果：PASS**（修复后）

### 1.4 回滚
- `--rollback`（默认取最新备份）恢复后与原文件**逐字节一致**
- **结果：PASS**（修复后）

---

## 2. Token 生成（Step 1 模拟）

用 `gen_token.py` 生成 4 个 token（super / muse-rd928 / muse-homepc / ssh_fallback_admin+`allowed_ips=["127.0.0.1/32"]`）：
- `controllers.json` 权限 600，只存 SHA-256 摘要，无明文
- `verify.py` 验证通过；`revoke.py` 吊销通过（`enabled=false`，条目保留）
- **明文 token 走 stderr 输出**（刻意设计，防管道/日志泄露）——抓取时必须用 `2>&1 1>/dev/null`
- **结果：PASS**

## 3. 冒烟测试（Step 2 的 7 场景，用 FastAuthEngine + mock 数据）

| # | 场景 | 预期 | 结果 |
|---|------|------|------|
| 4a | 错 token | 401 | PASS |
| 4a | 空 token | 401 | PASS |
| 4b | RD-928 token 访问 homepc | 403 | PASS |
| 4c | 正确 token+target | 200 | PASS |
| 4c | 单 PC 绑定 token 不带 target（自动解析） | 200 | PASS |
| 4d | 超管无 target | 403 | PASS |
| 4d-2 | 超管带 target | 200 | PASS |
| 4e-7 | fallback token + 127.0.0.1 | 200 | PASS |
| 4e-7b | fallback token + 公网 IP | 403 | PASS |

**9/9 通过。** 鉴权热路径实测 ~2.0μs/req（含 Python 循环开销；Gemini 目标 <0.8μs 指纯缓存命中，量级一致）。

## 4. 去重状态机冒烟
- `get_or_create` 去重、COMPLETED 缓存重放、UNCERTAIN 粘滞：**PASS**

## 5. v2 模块导入检查
- `pool.py`（ConnectionPool）、`microbatch.py`、`tls_resume.py`、`compress.py`、`dedup.py`、`wss_scheduler.py`、`fast_auth.py`：全部可导入

---

## 6. 发现的问题（共 4 个）

### 🔴 P0-1（已修复）：`deploy-auth-patch.sh` 二次运行毒化回滚备份
- **现象**：脚本先备份、后检查 marker。第二次运行时会把**已打补丁的文件**存为 `.bak-<新时间戳>`，`--rollback` 默认取最新备份 → 回滚到补丁版而非原版（dry-run 中复现：回滚后与原文件不一致）。
- **修复**：bash 层在备份前先 `grep -q CONTROLLERS_PATH`，已打补丁则直接退出（`scripts/deploy-auth-patch.sh` 已改，`bash -n` 通过；mock 重新验证：二次运行零新备份，回滚逐字节一致）。
- **真实 VPS 影响**：若此前有人在 VPS 上跑过旧版脚本两次，`/usr/local/bin/muse-bridge.py.bak-*` 中最新的可能是补丁版——**部署前务必 `grep -L CONTROLLERS_PATH` 确认回滚源是干净原版**，或直接用本次 dry-run 验证过的备份策略重建。

### 🟡 P1-2：`gen-tokens.sh` 不支持第 4 个 token（ssh_fallback_admin）
- **现象**：脚本只生成 3 个标准 token，无 `--allowed-ips` 参数，无法生成 `ssh_fallback_admin`（需 `allowed_ips=["127.0.0.1/32"]`）。
- **现状**：RUNBOOK_V2 已写明手动用 `gen_token.py --allowed-ips` 补第 4 个。
- **建议**：二选一——(a) 给 `gen-tokens.sh` 加 `--allowed-ips` 透传 + 第 4 token 预设；(b) 保持现状，runbook 手动步骤已够用。**需人工确认**。

### 🟡 P1-3：`gen-tokens.sh` 与 `deploy-auth-patch.sh` 的 VPS 路径默认值指向 `/tmp`
- **现象**：`GEN_SCRIPT` 默认 `/tmp/token-mgr-proto/gen_token.py`，`PATCH_DIR` 默认 `/tmp/bridge-auth-impl`——VPS 上若未按文档预置，脚本会直接报错退出（fail-loud，非静默错）。
- **评估**：行为安全（报错而非错装），RUNBOOK_V2 已要求显式覆盖。**需人工确认** VPS 上的预置路径与文档一致。

### 🟢 P2-4：`verify.py --ip` 不传时跳过 IP 校验（向后兼容设计）
- **现象**：不带 `--ip` 时只验 token 有效性，不验 allowed_ips。
- **评估**：这是刻意的向后兼容，VPS 侧 FastAuthEngine 始终校验（不受影响）。无需改动，记录备查。

---

## 7. 需人工确认的点

1. **VPS 预置路径**：`deploy-auth-patch.sh`（PATCH_DIR）、`gen-tokens.sh`（GEN_SCRIPT）在 VPS 上的实际落盘位置，是否与 RUNBOOK_V2 一致。
2. **P0-1 的历史污染**：VPS 上若存在旧版脚本产生的 `.bak-*`，部署前用 `grep -L CONTROLLERS_PATH /usr/local/bin/muse-bridge.py.bak-*` 挑出干净原版。
3. **P1-2 决策**：`gen-tokens.sh` 是否需要支持第 4 token，还是接受 runbook 手动步骤。
4. **真实冒烟**：本报告的 9 个冒烟用例是 FastAuthEngine 单元级验证；VPS 部署后仍需按 RUNBOOK_V2 跑真实 HTTP 冒烟（curl 401/403/200）。
5. **systemd 服务名**：`SERVICE_NAME` 默认为 `muse-bridge`，需在 VPS 上 `systemctl list-units | grep -i bridge` 确认。

---

## 8. 结论

- 脚本逻辑 dry-run **全部通过**（合并/幂等/回滚/token/鉴权/去重/模块导入）。
- 发现 **4 个问题**：1 个 P0 已修复并验证，2 个 P1 待人工确认，1 个 P2 备查。
- **可以进入真实部署**（按 RUNBOOK_V2 Step 0→4），前提是先确认 §7 的 5 个点。
