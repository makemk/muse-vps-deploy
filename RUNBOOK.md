# VPS 部署 Runbook — Controller Token 鉴权

**目标**：在 VPS 上为 `muse-bridge.py` 部署 controller-token 双层鉴权，生成 3 个 controller token，
验证 6 个场景，最后把 VM→VPS 流量从 SSH 切到 HTTPS daemon。

**范围**：VPS 侧（`/usr/local/bin/muse-bridge.py` + `/etc/muse-bridge/controllers.json`）+
VM 侧（mused daemon + musepc wrapper 切流）。
`/mail-api/` 智能体网关是后续独立阶段，不在本次部署内。

**执行者假设**：本机操作者，有 VPS root SSH（`ssh -F /home/hatch/.ssh/config vps38`）。

**本地材料目录**：`~/workspace/muse-vps-deploy/`（已整合，原 `/tmp/vps-deploy/` 和
`/tmp/bridge-auth-impl/` 的内容都在里面）：

| 目录 | 内容 |
|------|------|
| `scripts/` | `deploy-auth-patch.sh`、`gen-tokens.sh`、`DEPLOY.md` |
| `vps-auth-patch/` | `auth_patch.py`、`dispatch_patch.py`、`IMPLEMENTATION.md`（patch 源码） |
| `token-manager/` | `gen_token.py`（单 token 生成器，gen-tokens.sh 调用它） |
| `mused/` | `mused.py`（daemon）、`museapi`（CLI） |
| `museapi-client/` | `client.py`（裸 HTTPS 原型，含 CONNECT+TLS recipe） |
| `tests/` | `test_daemon.py`（VM 侧 pytest） |

---

## 0. 部署前检查清单

在本地（VM）逐项打勾，全部 ✅ 才开始：

| # | 检查项 | 命令 / 方法 | 预期 |
|---|--------|-------------|------|
| 0.1 | VPS SSH 可达 | `ssh -F /home/hatch/.ssh/config vps38 "echo ok"` | `ok` |
| 0.2 | bridge 服务名确认 | `ssh -F /home/hatch/.ssh/config vps38 "systemctl list-units --type=service \| grep -i -E 'bridge\|muse'"` | 记下实际单元名（脚本默认 `muse-bridge`） |
| 0.3 | bridge 文件存在且可读 | `ssh -F /home/hatch/.ssh/config vps38 "ls -la /usr/local/bin/muse-bridge.py && md5sum /usr/local/bin/muse-bridge.py"` | 记录 md5（这是回滚基准） |
| 0.4 | Python3 可用 | `ssh -F /home/hatch/.ssh/config vps38 "python3 --version"` | 3.x |
| 0.5 | nginx `/muse-api/` 生效 | `ssh -F /home/hatch/.ssh/config vps38 "curl -sk https://127.0.0.1:443/muse-api/status \| head -c 200"` | 200 或 401（都有响应即正常） |
| 0.6 | RD-928、homepc 在线 | VPS 上 `curl -sk https://127.0.0.1:443/muse-api/status` 看 `client_list` | 两台都在（4c 验证需要 RD-928 在线） |
| 0.7 | 本地材料已同步到 VPS | 见 Step 1 | `/root/muse-vps-deploy/` 完整 |
| 0.8 | 已通知用户 | — | 部署期间 PC 侧可能短暂重连（重启服务引起），提前告知 |

> **回滚点**：0.3 的 md5 是黄金基准。任何时候 `md5sum /usr/local/bin/muse-bridge.py`
> 与之不符且服务异常 → 直接回滚（§3）。

---

## 1. 分步部署

### 1.0 约定

- `vps` 前缀的命令 = 在 VPS 上以 root 执行。简写：先 `ssh -F /home/hatch/.ssh/config vps38`
  进去，后面所有 `vps#` 命令都在那个 shell 里跑。
- `<TOKEN>` 占位符 = 从 gen-tokens.sh 输出里复制的明文 token，**绝不贴到聊天、不写文件、不进 git**。
- 脚本里所有 VPS 路径都可被环境变量覆盖（`BRIDGE_PY`、`PATCH_DIR`、`CONTROLLERS_JSON`、
  `SERVICE_NAME`、`GEN_SCRIPT`）。

### Step 0: 把材料传到 VPS

在本地执行（scp 经同一 SSH config）：

```bash
# 打包本地整合目录，传到 VPS /root/
cd ~/workspace && tar czf /tmp/muse-vps-deploy.tar.gz muse-vps-deploy/
scp -F /home/hatch/.ssh/config /tmp/muse-vps-deploy.tar.gz vps38:/root/
ssh -F /home/hatch/.ssh/config vps38 "mkdir -p /root/muse-vps-deploy && tar xzf /root/muse-vps-deploy.tar.gz -C /root/ && ls /root/muse-vps-deploy/"
```

预期输出：`mused  museapi-client  scripts  tests  token-manager  vps-auth-patch`

> 后面所有 VPS 侧路径以 `/root/muse-vps-deploy/` 为准：
> - patch 源码：`/root/muse-vps-deploy/vps-auth-patch/`
> - token 生成器：`/root/muse-vps-deploy/token-manager/gen_token.py`
> - 部署脚本：`/root/muse-vps-deploy/scripts/`

### Step 1: 备份原文件（vps#）

```bash
vps# cp -p /usr/local/bin/muse-bridge.py /usr/local/bin/muse-bridge.py.bak-$(date +%Y%m%d-%H%M%S)
vps# ls -la /usr/local/bin/muse-bridge.py.bak-* && md5sum /usr/local/bin/muse-bridge.py /usr/local/bin/muse-bridge.py.bak-*
```

预期：两个 md5 一致。**记下备份文件名**（回滚用）。

> 注：deploy-auth-patch.sh 自己也会再做一次带时间戳的备份。手动这一步是双保险，
> 也是 0.3 回滚基准的物理载体。

### Step 2: 部署 auth patch（vps#，先 dry-run）

```bash
# 2a. dry-run：只检查锚点，不改文件
vps# BRIDGE_PY=/usr/local/bin/muse-bridge.py \
     PATCH_DIR=/root/muse-vps-deploy/vps-auth-patch \
     bash /root/muse-vps-deploy/scripts/deploy-auth-patch.sh --check-only
```

预期：6 行 "inserted/replaced"、一行 `OK: ... compiles`、exit 0。

**如果出现 `FATAL`**（锚点不匹配）→ 停。说明 VPS 上的 bridge 版本与
`vps-auth-patch/muse-bridge-orig.py` 漂移了。先 `diff` 找出差异，修 patch 锚点后再来。
**绝不强行部署**。

```bash
# 2b. 正式部署（幂等：重复跑会自动跳过已应用的部分）
vps# BRIDGE_PY=/usr/local/bin/muse-bridge.py \
     PATCH_DIR=/root/muse-vps-deploy/vps-auth-patch \
     SERVICE_NAME=muse-bridge \
     bash /root/muse-vps-deploy/scripts/deploy-auth-patch.sh
```

脚本做的事（按顺序）：
1. 再备份一次 → `/usr/local/bin/muse-bridge.py.bak-<timestamp>`
2. 精确锚点合并：全局变量 `CONTROLLERS_PATH` + 4 个函数
   （`load_controllers`/`verify_controller_token`/`check_target_permission`/`resolve_target`）
   + 双层鉴权块替换 + `/cmd`、`/batch`、`/file` 各 8 行 target 权限检查
3. `python3 -m py_compile` 语法检查（失败则中止，备份不动）
4. 建空 `/etc/muse-bridge/controllers.json`（600；空 = legacy token 仍可用，新 token 全部拒绝——fail-closed）
5. `systemctl restart muse-bridge`

部署后立刻验证服务活着：

```bash
vps# systemctl is-active muse-bridge
# 预期：active
vps# sleep 2 && curl -sk https://127.0.0.1:443/muse-api/status -o /dev/null -w "%{http_code}\n"
# 预期：401（鉴权链工作；空 controllers.json + 无 legacy header → 401 正常）
```

**如果 `is-active` 不是 active** → 看日志（§4），修不好 → 回滚（§3）。

### Step 3: 生成 3 个 controller token（vps#）

```bash
vps# GEN_SCRIPT=/root/muse-vps-deploy/token-manager/gen_token.py \
     bash /root/muse-vps-deploy/scripts/gen-tokens.sh
```

预期：生成 `super`、`muse-rd928`、`muse-homepc`，写入
`/etc/muse-bridge/controllers.json`（600，root:root，只存 SHA-256），
**明文 token 在终端打印一次**。

**明文 token 的安全处理（严格执行）**：
1. 终端输出**立即**分发，不截屏、不复制到聊天窗口
2. `super` → 操作者的 Secure Vault（超管 muse 用）
3. `muse-rd928` → 经现有安全文件投递给 RD-928 的 muse（走 RD-928 本机文件，不走聊天）
4. `muse-homepc` → 同上，给 homepc 的 muse
5. 分发完清屏（`clear` / `reset`），终端滚动历史里不留

验证只存了摘要、不暴露明文：

```bash
vps# ls -la /etc/muse-bridge/controllers.json && bash /root/muse-vps-deploy/scripts/gen-tokens.sh --list
# 预期：600 权限；--list 只显示 muse/pcs/label/enabled + digest 前 12 位，无明文
vps# grep -c "muse_" /etc/muse-bridge/controllers.json || echo "no plaintext stored (good)"
# 预期：no plaintext stored (good) —— 注意 key 名本身不含 muse_ 前缀则为 0
```

> hot-reload 自动生效（mtime 检查），**token 变更不需要重启 bridge**。
> 吊销：把某条目的 `"enabled"` 设为 `false`（或删条目），即时生效。

### Step 4: 验证 6 个场景（vps#，全部要在本地先跑通再往下）

```bash
vps# API=https://127.0.0.1:443/muse-api
vps# RD928_TOKEN="<muse-rd928 的明文 token>"
vps# SUPER_TOKEN="<super 的明文 token>"
```

**4a. 错 token → 401**（证明鉴权链工作）：

```bash
vps# curl -sk -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer muse_wrong-token-xyz" $API/status
# 预期：401
```

**4b. controller token 配错 target → 403**（证明 1:1 隔离）：

```bash
vps# curl -sk -H "Authorization: Bearer $RD928_TOKEN" -H "Content-Type: application/json" \
     -d '{"target":"homepc","command":"echo hi","wait":true}' $API/cmd
# 预期：403，body 含 "not authorized"
```

**4c. controller token 配对 target → 200**（证明转发工作；要求 RD-928 在线）：

```bash
vps# curl -sk -H "Authorization: Bearer $RD928_TOKEN" -H "Content-Type: application/json" \
     -d '{"target":"RD-928","command":"echo hello-from-token","wait":true}' $API/cmd
# 预期：200，stdout 含 "hello-from-token"
```

**4d. 超管无 target → 403**（证明超管强制显式 target）：

```bash
vps# curl -sk -H "Authorization: Bearer $SUPER_TOKEN" -H "Content-Type: application/json" \
     -d '{"command":"echo hi","wait":true}' $API/cmd
# 预期：403，body 含 "must specify target"
```

**4d-2. 超管有 target → 200**（证明超管可直控任意 PC）：

```bash
vps# curl -sk -H "Authorization: Bearer $SUPER_TOKEN" -H "Content-Type: application/json" \
     -d '{"target":"RD-928","command":"echo super-ok","wait":true}' $API/cmd
# 预期：200，stdout 含 "super-ok"
```

**4e. legacy 全局 token 仍可用**（过渡期安全；token 在 `/etc/muse-bridge/token`）：

```bash
vps# LEGACY=$(cat /etc/muse-bridge/token 2>/dev/null || echo "NONE")
vps# curl -sk -o /dev/null -w "%{http_code}\n" -H "X-Bridge-Token: $LEGACY" $API/status
# 预期：200（如无 legacy token 文件则跳过本项）
vps# journalctl -u muse-bridge --since "5 min ago" | grep -i "legacy" | tail -2
# 预期：有 "[bridge] Legacy global token used" 审计行
```

**4f. PC 客户端不受影响**（WSS 路径零改动）：

```bash
vps# curl -sk $API/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('client_list', d))"
# 预期：RD-928 和 homepc 都在 client_list 里
vps# journalctl -u muse-bridge --since "10 min ago" | grep -ci "1006"
# 预期：0（无 1006 风暴；重启瞬间 1-2 次重连是正常的）
```

> **6 个场景（4a/4b/4c/4d/4e/4f）必须全绿**，才能进 Step 5。
> 任一失败 → 先看 §4 排查，修不好 → 回滚（§3）。

### Step 5: VM 端 daemon 联调（本地，不经过 SSH）

前置：super token 已存到 Secure Vault；从 vault 取出，写入 daemon 的 token 文件。

```bash
# 5a. 准备 daemon（本地）
mkdir -p ~/.config/mused ~/.local/share/mused
cp ~/workspace/muse-vps-deploy/mused/mused.py ~/workspace/bin/ 2>/dev/null || cp ~/workspace/muse-vps-deploy/mused/mused.py ~/.local/bin/mused.py
cp ~/workspace/muse-vps-deploy/mused/museapi ~/workspace/bin/museapi
chmod +x ~/workspace/bin/museapi

# 5b. 写配置（token 单独文件，600；绝不进 config.json）
cat > ~/.config/mused/config.json << 'EOF'
{
  "vps_host": "38.207.167.51",
  "vps_port": 443,
  "sni": "ai.vps38.local",
  "ca_cert_path": "/home/hatch/workspace/muse-vps-deploy/museapi-client/vps38-root-ca.pem",
  "token_file": "/home/hatch/.config/mused/token",
  "socket_path": "/home/hatch/.config/mused/mused.sock",
  "heartbeat_interval": 30
}
EOF
# 注意：ca_cert_path 指向 VPS38 Root CA。muse-bridge 插件源码里自带 vps-root-ca.pem，
# 本地找一份拷到该路径（或从 VPS /etc/ssl/certs/vps38-root-ca.pem 取）。
# token 文件：
printf '%s' "$SUPER_TOKEN" > ~/.config/mused/token   # $SUPER_TOKEN 从 vault 取，不回显
chmod 600 ~/.config/mused/config.json ~/.config/mused/token

# 5c. 起 daemon
nohup python3 ~/workspace/bin/mused.py > ~/.local/share/mused/daemon.out 2>&1 &
sleep 2 && ls -la ~/.config/mused/mused.sock && tail -5 ~/.local/share/mused/mused.log

# 5d. 联调：经 daemon 调 RD-928（走 HTTPS，无 SSH）
~/workspace/bin/museapi --target RD-928 POST /cmd '{"command":"echo hello-via-daemon"}'
# 预期：HTTP 200，stdout 含 "hello-via-daemon"；第二次调用约 27-60ms（keep-alive）
```

如果 5d 返回 401：检查 token 文件内容与 VPS 侧是否一致（注意首尾空白；
`verify_controller_token` 会 strip，但文件里别带换行——上面用 `printf '%s'` 已处理）。

如果连不上：用裸原型交叉验证（排除 daemon 本身的问题）：

```bash
cd ~/workspace/muse-vps-deploy/museapi-client && ./test.sh
# 跑 client.py 的 keep-alive 原型，直连 VPS /muse-api/
```

### Step 6: 切流量（本地）

前提：Step 4 六场景全绿 + Step 5 联调成功。

```bash
# 6a. 先双跑 24h：musepc wrapper 优先走 daemon，失败回退 SSH
# （wrapper 改造是独立小任务；在那之前，手动用 museapi 承担日常调用，SSH 留作备份）

# 6b. 24h 无异常后，把 ~/workspace/bin/musepc 默认改走 daemon：
#     wrapper 里：daemon socket 存在且 /status 200 → 走 museapi；否则走 SSH。
#     （具体改法见实施任务，本 runbook 只定切换策略）

# 6c. 切流后观察 7 天（指标见 §5），一切正常才考虑关 SSH：
#     vps#  # 删 VPS 上的 authorized_keys（最后一步，不可逆前再三确认）
```

> **SSH 在整个过渡期保留**。只有 §5 的 7 天观察期全绿，才动 SSH key。

---

## 2. 回滚方案

### 触发条件（任一即回滚，不犹豫）

| 信号 | 说明 |
|------|------|
| 服务起不来 | Step 2b 后 `systemctl is-active` 非 active，5 分钟内修不好 |
| 4a–4f 任一失败且原因不明 | 排查 15 分钟无头绪 |
| PC 掉线 | 4f 发现 RD-928/homepc 掉线且 5 分钟内不自动重连 |
| 用户报障 | 部署窗口内用户反馈插件异常 |

### 回滚步骤（vps#，按顺序，每一步可单独执行）

```bash
# R1. 恢复 bridge 文件（用 Step 1 的手动备份，或脚本自动备份里最新的）
vps# ls -t /usr/local/bin/muse-bridge.py.bak-* | head -3
vps# cp /usr/local/bin/muse-bridge.py.bak-<Step1记下的时间戳> /usr/local/bin/muse-bridge.py
vps# md5sum /usr/local/bin/muse-bridge.py
# 对照 0.3 记录的 md5，必须一致

# R2. 语法检查 + 重启（或一键：bash /root/muse-vps-deploy/scripts/deploy-auth-patch.sh --rollback）
vps# python3 -m py_compile /usr/local/bin/muse-bridge.py && echo "syntax OK"
vps# systemctl restart muse-bridge && systemctl is-active muse-bridge
# 预期：active

# R3. 验证回到部署前状态
vps# curl -sk https://127.0.0.1:443/muse-api/status -o /dev/null -w "%{http_code}\n"
# 预期：401（无 token 时；有 legacy token 则 200）——关键是服务有响应
vps# curl -sk https://127.0.0.1:443/muse-api/status | python3 -c "import json,sys; print(json.load(sys.stdin).get('client_list'))"
# 预期：两台 PC 重新上线（最多等 1-2 分钟，插件会自动重连）

# R4. 本地恢复 SSH 路径（daemon 停掉，musepc 走原 SSH）
pkill -f mused.py; rm -f ~/.config/mused/mused.sock
# musepc wrapper 本来就默认 SSH，无需改
```

### 回滚后

1. 在终端里 `echo` 确认 PC 上线后，通知用户"已回滚，服务恢复"
2. 保留现场：`journalctl -u muse-bridge --since "<部署开始时间>" > /root/bridge-fail-$(date +%Y%m%d-%H%M%S).log`
3. 不删除备份文件；分析日志找到根因后再定下一次窗口

---

## 3. 故障排查

### 日志位置（vps#）

```bash
vps# journalctl -u muse-bridge -f                    # 实时
vps# journalctl -u muse-bridge --since "30 min ago" | tail -50
vps# journalctl -u muse-bridge -p err --since "1 hour ago"   # 只看错误
```

### 常见错误速查

| 现象 | 可能原因 | 诊断命令 | 处理 |
|------|----------|----------|------|
| `systemctl restart` 后 inactive | patch 语法错误（py_compile 应已拦截）或端口被占 | `journalctl -u muse-bridge -p err --since "5 min ago"` | 看 traceback；修不好 → 回滚 §2 |
| 4a 返回 200 而不是 401 | patch 没生效（服务跑的还是旧文件） | `vps# grep -c "verify_controller_token" /usr/local/bin/muse-bridge.py`（应 >0）；`systemctl show muse-bridge -p ExecStart` 确认跑的是 `/usr/local/bin/muse-bridge.py` | 重新跑 Step 2b |
| 4c 返回 403 但 target 是对的 | controllers.json 里 pcs 写错（如大小写） | `bash gen-tokens.sh --list` 核对 pcs | `--add` 重建或改 json 后等 hot-reload（≤60s） |
| 4c 返回 401 | token 复制错 / 多了换行 | VPS 侧 digest 对不上；本地 `wc -c ~/.config/mused/token` 看长度是否异常 | 从 vault 重取，`printf '%s'` 重写 |
| 4e legacy 返回 401 | legacy token 文件路径不对或 bridge 重启后没加载 | `ls -la /etc/muse-bridge/token`；看启动日志里 `load_token` | 对照原文件行为；过渡期可跳过 4e（SSH 还在） |
| PC 掉线不重连 | 重启瞬间正常（插件有重连）；超过 5 分钟不回 | `journalctl -u muse-bridge --since "10 min ago" \| grep -iE "1006\|disconnect" \| tail` | 让用户 Reload Window；仍不行 → 回滚 |
| daemon 连不上 VPS | egress 代理变量没透 / SNI 错 / CA 路径错 | `curl -v` 看 CONNECT 阶段；`python3 client.py` 裸测 | 对照 `museapi-client/client.py` 的 recipe 逐段排查 |
| `--check-only` 报 FATAL | VPS bridge 版本漂移 | `diff /root/muse-vps-deploy/vps-auth-patch/muse-bridge-orig.py /usr/local/bin/muse-bridge.py`（回滚前先备份！） | 修锚点，不强上 |

### 快速诊断一键包（vps#，出问题先跑这个，把输出贴给排查者）

```bash
vps# {
echo "=== service ==="; systemctl is-active muse-bridge
echo "=== md5 ==="; md5sum /usr/local/bin/muse-bridge.py
echo "=== patch marker ==="; grep -c "verify_controller_token" /usr/local/bin/muse-bridge.py
echo "=== controllers ==="; ls -la /etc/muse-bridge/controllers.json 2>/dev/null
echo "=== api ==="; curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1:443/muse-api/status
echo "=== clients ==="; curl -sk https://127.0.0.1:443/muse-api/status | head -c 300; echo
echo "=== errors ==="; journalctl -u muse-bridge -p err --since "15 min ago" | tail -10
} 2>&1 | tee /root/bridge-diag-$(date +%Y%m%d-%H%M%S).txt
```

---

## 4. 部署后验证清单（7 天观察期）

从 Step 6 切流成功当天算起，每天看一次，记到检查表里：

| # | 指标 | 怎么看 | 正常线 | 异常动作 |
|---|------|--------|--------|----------|
| 1 | 服务存活 | `systemctl is-active muse-bridge` | 持续 active | 非 active → 按 §3 排查 |
| 2 | 401/403 比例 | `journalctl -u muse-bridge --since "24 hours ago" \| grep -cE "401\|403"` vs 总请求 | 401/403 只来自探测；无突发增长 | 突增 → 查是否 token 泄露被刷 |
| 3 | 鉴权审计行 | 日志里 `[bridge] <muse> -> <target>` | 每次 controller 调用都有 | 缺失 → patch 日志被关了，检查 |
| 4 | legacy 使用 | 日志里 `Legacy global token used` | 逐日下降 → 0 | 7 天后仍大量 → 有调用方没切，找出并迁移 |
| 5 | PC 在线 | `/muse-api/status` 的 client_list | 两台常年在线 | 掉线 → 先看插件侧，再看 VPS |
| 6 | 1006 计数 | `journalctl ... \| grep -c 1006`（每天） | 接近 0 | 风暴 → 回滚 §2 |
| 7 | 端到端延迟 | `museapi --target RD-928 POST /cmd '{"command":"echo t"}'` 记时 | 稳态 <100ms（原型 27ms） | 持续 >300ms → 查 VPS 负载 / 网络 |
| 8 | controllers.json 完整性 | `ls -la` 权限 + `--list` 条目数 | 600、3 条、enabled 全 true | 权限变 → 立刻改回 600 并查原因 |
| 9 | 磁盘/日志 | `df -h /` + `journalctl --disk-usage` | 余量充足 | journal 膨胀 → 设 `SystemMaxUse` |

**7 天全绿 → 才允许动 SSH key**（Step 6c）。任一天出现"异常动作"列的回滚级事件，
先回滚再分析，不带病运行。

---

## 附：文件与路径总表

| 路径（VPS） | 说明 |
|-------------|------|
| `/usr/local/bin/muse-bridge.py` | 被 patch 的目标 |
| `/usr/local/bin/muse-bridge.py.bak-<ts>` | 自动+手动备份 |
| `/etc/muse-bridge/controllers.json` | 600，只存 SHA-256 |
| `/etc/muse-bridge/token` | legacy 全局 token（过渡期） |
| `/root/muse-vps-deploy/` | 本次部署材料 |
| `/etc/ssl/certs/vps38-root-ca.pem` | VPS38 Root CA（VM 侧校验用，可拷一份到本地） |

| 路径（本地 VM） | 说明 |
|-----------------|------|
| `~/workspace/muse-vps-deploy/` | 材料总库 |
| `~/workspace/bin/musepc` | 现有 SSH wrapper（过渡期保留） |
| `~/workspace/bin/museapi` | daemon CLI（Step 5 部署） |
| `~/.config/mused/config.json` | daemon 配置（600，无 token） |
| `~/.config/mused/token` | super 明文 token（600，vault 分发） |
| `~/.local/share/mused/mused.log` | daemon 日志（轮转 10MB×5） |
