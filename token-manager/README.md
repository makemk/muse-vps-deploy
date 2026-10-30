# Token 管理器（token-manager）

controller token 的生成、验证、轮换、吊销全套工具。纯 Python 标准库。

## Token 模型

- 格式：`muse_` + `secrets.token_urlsafe(32)`（256-bit 熵）
- VPS 只存 **SHA-256 hex**，明文永不落盘、永不可恢复
- `controllers.json` 条目字段见 [SCHEMA.md](SCHEMA.md)
- 鉴权头：`Authorization: Bearer <token>`（VPS 端解析）
- 安全规范见 [SECURITY.md](SECURITY.md)

## 文件清单

| 文件 | 说明 |
|------|------|
| `common.py` | 共享逻辑：格式校验、SHA-256、`is_entry_valid`（含宽限期语义）、原子写入（600）、flock、审计日志。VPS 鉴权 patch 可复用 |
| `gen_token.py` | 生成 token：`--muse` `--pcs` `--label` `--output` `--force` `--allowed-ips`；`--output` 直接追加写入 db（flock+原子+600），重名默认拒绝；明文只在 stderr 显示一次 |
| `verify.py` | 验证明文 token：格式校验 → `hmac.compare_digest` → `enabled`/`grace_until` 检查 → `--ip` 校验 `allowed_ips`；输出 `{"ok": true/false, ...}` |
| `rotate.py` | 零停机轮换：`--muse` `--digest` `--grace-hours`（默认 24）；新旧双活，旧 token 宽限期后自动失效；`--grace-hours 0` 立即切换 |
| `revoke.py` | 吊销：`--muse`（全部有效）或 `--digest`（精确一条）；`--reason` 必填；二次确认（`--yes` 跳过）；审计日志 |
| `SCHEMA.md` | `controllers.json` JSON Schema 文档（全部字段、约束、示例） |
| `SECURITY.md` | 安全规范：明文生命周期、文件保护、传输、轮换周期、泄露应急、审计 |
| `test_token_mgr.py` | pytest 测试（21 项） |
| `controllers.json` | 测试用示例数据库（**不是生产数据**，生产路径 `/etc/muse-bridge/controllers.json`） |

## 测试方法

```bash
cd ~/workspace/muse-vps-deploy/token-manager
python3 -m pytest -q
# 预期：45 passed（21 基础 + 24 allowed_ips 白名单）
```

覆盖：token 格式/唯一性/熵、格式错误拒绝、db 权限 600、验证成功/错误/禁用/fail-closed、
宽限期内有效/超时失效/损坏 fail-closed、CLI 生成→验证 round-trip、重名拒绝、`"*"` 独占、
轮换双活/立即切换/无有效token失败、吊销（按 muse/按 digest）、二次确认、审计日志。

## 常用操作

```bash
# 生成并直接写入（明文从 stderr 复制，只显示一次）
./gen_token.py --muse muse-rd928 --pcs RD-928 --label "RD-928 绑定" \
    --output /etc/muse-bridge/controllers.json

# 超管（绑定源 IP 白名单）
./gen_token.py --muse super --pcs "*" --label "超管 Muse" \
    --allowed-ips "203.0.113.10/32,198.51.100.0/24" \
    --output /etc/muse-bridge/controllers.json

# SSH 回退通道专用 token：只允许 VPS 本机回环调用
./gen_token.py --muse ssh_fallback_admin --pcs "*" --label "SSH 回退通道" \
    --allowed-ips "127.0.0.1/32" \
    --output /etc/muse-bridge/controllers.json

# 验证（含源 IP 校验）
./verify.py --token 'muse_...' --db /etc/muse-bridge/controllers.json --ip 203.0.113.10

# 轮换（24h 宽限期双活）
./rotate.py --muse muse-rd928 --db /etc/muse-bridge/controllers.json

# 吊销（疑似泄露）
./revoke.py --digest a1b2c3d4 --db /etc/muse-bridge/controllers.json \
    --reason "疑似泄露" --yes
```

## 与 VPS 鉴权 patch 的衔接

`bridge-auth-impl` 的 `verify_controller_token` **必须**实现与
`common.is_entry_valid` 相同的语义，特别是 `grace_until` 宽限期检查，
否则轮换期间旧 token 会被 VPS 提前拒绝。建议 VPS 端直接 import 本模块
（或复制 `is_entry_valid` + `TOKEN_RE` 过去，保持单语义源）。
