# 从 musepc（SSH）迁移到 musecli（HTTPS）

## 一句话

把 `~/workspace/bin/musepc` 换成 `musecli`，参数不变，延迟从 730ms 降到 ~100ms。

## 对照表

| musepc（旧） | musecli（新） | 说明 |
|---|---|---|
| `musepc --target H "cmd"` | `musecli --target H "cmd"` | 完全一致 |
| `musepc --cwd D "cmd"` | `musecli --cwd D "cmd"` | 完全一致 |
| `musepc --timeout 120 "cmd"` | `musecli --timeout 120 "cmd"` | 完全一致 |
| `musepc --read <path>` | `musecli --read <path>` | 完全一致 |
| `musepc --write <p> <c>` | `musecli --write <p> <c>` | 完全一致 |
| `musepc --edit <p> <o> <n>` | `musecli --edit <p> <o> <n>` | 完全一致 |
| `musepc --glob <pat> [path]` | `musecli --glob <pat> [path]` | 完全一致 |
| `musepc --grep <pat> [path]` | `musecli --grep <pat> [path]` | 完全一致 |
| `musecli --ls <path>` | `musecli --ls <path>` | 完全一致 |
| `musepc --archive <p> ...` | `musecli --archive <p> ...` | 完全一致 |
| `musepc --extract <p> [d]` | `musecli --extract <p> [d]` | 完全一致 |
| `musepc --status` | `musecli --status` | 完全一致 |
| `musepc --json "cmd"` | `musecli --json "cmd"` | 完全一致 |
| `MUSEPC_TARGET=H musepc ...` | `MUSEPC_TARGET=H musecli ...` | 环境变量同样生效 |
| `musepc --slice <p> <o> <c>` | `musecli --slice <p> <o> <c>` | 新实现：读全文件本地切片 |

## 迁移步骤

### 1. 准备 token

```bash
# token 由 VPS 管理员生成（见 ../scripts/gen-tokens.sh）
# 存到 ~/.config/mused/token，权限 600
mkdir -p ~/.config/mused
cat > ~/.config/mused/token <<'EOF'
muse_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
EOF
chmod 600 ~/.config/mused/token
```

### 2. 检查配置

```bash
# 默认配置开箱即用；如需覆盖：
cat ~/.config/mused/config.json
# {"vps_host":"38.207.167.51","vps_port":443,"sni":"ai.vps38.local",...}
```

### 3. 验证连通

```bash
cd ~/workspace/muse-vps-deploy/museapi-client
python3 cli.py --status
# 预期：HTTP 200 + 客户端列表（或 401 = token 错误，先修 token）
```

### 4. 并行试运行

```bash
# 旧链路保持不动，新链路并行验证
diff <(~/workspace/bin/musepc --target RD-928 "echo hi") \
     <(python3 cli.py --target RD-928 "echo hi") && echo SAME
```

### 5. 切换

```bash
# 方案 A：别名（可随时回退）
alias musepc='python3 ~/workspace/muse-vps-deploy/museapi-client/cli.py'

# 方案 B：替换 wrapper（彻底）
# 备份 ~/workspace/bin/musepc 为 musepc.ssh.bak，再替换
```

## 行为差异

| 方面 | musepc（SSH） | musecli（HTTPS） |
|---|---|---|
| 首次延迟 | ~300ms（SSH 握手） | ~250ms（TLS 握手，一次性） |
| 稳态延迟 | ~300ms（每次新建） | **~32ms**（keep-alive） |
| 并发 | 受 SSH maxsessions=128 限制 | 仅受 VPS CPU 限制（>2k req/s） |
| 鉴权 | SSH 密钥（整机权限） | Bearer token（per-muse，target 受限） |
| 超管无 target | 默认打到 RD-928 | **403 拒绝**（必须显式指定） |
| 断网重连 | SSH 自动 | 连接池自动重建 |
| 输出格式 | 透传 PC 原始输出 | 相同（stdout/stderr/exit_code 语义不变） |

## 回滚

```bash
# 任何时候：直接用回 ~/workspace/bin/musepc（SSH 链路未动）
~/workspace/bin/musepc --target RD-928 "echo rollback-ok"
```

SSH 通道在切流观察期（建议 7 天）内保留，确认稳定后再关闭 VPS 的 SSH
密码/密钥登录（按用户"去 SSH"终态要求）。
