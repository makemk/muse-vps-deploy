# VPS 部署 Runbook v2 — Controller Token 鉴权（Gemini 4 修正顺序）

**目标**：在 VPS 上为 `muse-bridge.py` 部署 controller-token 双层鉴权，生成 controller token
（含 SSH 回退专用 token），验证冒烟场景，最后把 VM→VPS 流量从 SSH 切到 HTTPS daemon。

**与 v1 的核心差异**（见文末 §6 差异对照表）：
1. **顺序倒置修正**：Token 生成（Step 1）前置到 VPS 补丁上线（Step 2）之前——v1 是先打补丁再生成 token，
   中间存在"服务已强制鉴权但无有效凭证"的空窗期。
2. **新增 Step 0**：资产固化确认（/tmp → workspace 已完成）+ SSH 回退通道兼容确认
   （VPS 本机 musepc 必须先配好 fallback token，否则补丁上线瞬间 SSH 回退即被 401 锁死）。
3. **新增 `ssh_fallback_admin` 专用 token**：`pcs=["*"]` + `allowed_ips=["127.0.0.1/32"]`，
   存 `/etc/musepc/fallback.token`（600）。**严禁对 127.0.0.1 做回环白名单 bypass**
   （Nginx 转发的所有公网请求在 8471 看来都是 127.0.0.1，白名单 = 全网裸奔）。
4. **回滚判据量化**：Step 2 / Step 4 各设明确回滚点与数字阈值（10s / 5min / 1% / 150ms）。

**范围**：VPS 侧（`/usr/local/bin/muse-bridge.py` + `/etc/muse-bridge/controllers.json` +
`/etc/musepc/fallback.token`）+ VM 侧（mused daemon + musepc wrapper 切流）。
`/mail-api/` 智能体网关是后续独立阶段，不在本次部署内。

**执行者假设**：本机操作者，有 VPS root SSH（`ssh -F /home/hatch/.ssh/config vps38`）。

**本地材料目录**：`~/workspace/muse-vps-deploy/`（已整合，原 `/tmp/vps-deploy/`、
`/tmp/bridge-auth-impl/`、`/tmp/token-mgr-proto/`、`/tmp/mused-prod/` 的内容都在里面）：

| 目录 | 内容 |
|------|------|
| `scripts/` | `deploy-auth-patch.sh`、`gen-tokens.sh`、`DEPLOY.md` |
| `vps-auth-patch/` | `auth_patch.py`、`dispatch_patch.py`、`IMPLEMENTATION.md`、`SECURITY_REVIEW.md`（加固版 patch） |
| `token-manager/` | `gen_token.py`、`verify.py`、`rotate.py`、`revoke.py`、`common.py`、`SCHEMA.md`、`SECURITY.md` |
| `mused/` | `mused.py`（daemon）、`museapi`（CLI）、`mused.service`、`install.sh`、`uninstall.sh` |
| `museapi-client/` | `museapi.py`（生产级客户端库）、`cli.py`、`benchmark.py` |
| `tests/` | `test_daemon.py`、`test_token_mgr.py`、`test_museapi.py`、`test_security.py` |
| `monitoring/` | `vm_monitor.py`、`vps_monitor.py`、`alert.py` |

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
| 0.6 | RD-928、homepc 在线 | VPS 上 `curl -sk https://127.0.0.1:443/muse-api/status` 看 `client_list` | 两台都在（冒烟测试需要 RD-928 在线） |
| 0.7 | 本地材料已同步到 VPS | 见 Step 0 | `/root/muse-vps-deploy/` 完整 |
| 0.8 | 已通知用户 | — | 部署期间 PC 侧可能短暂重连（重启服务引起），提前告知 |

> **回滚点**：0.3 的 md5 是黄金基准。任何时候 `md5sum /usr/local/bin/muse-bridge.py`
> 与之不符且服务异常 → 直接回滚（§3）。

---

## 1. 分步部署

### 1.0 约定

- `vps` 前缀的命令 = 在 VPS 上以 root 执行。简写：先 `ssh -F /home/hatch/.ssh/config vps38`
  进去，后面所有 `vps#` 命令都在那个 shell 里跑。
- `<TOKEN>` 占位符 = 从 token 生成脚本输出里复制的明文 token，**绝不贴到聊天、不写文件、不进 git**。
- 脚本里所有 VPS 路径都可被环境变量覆盖（`BRIDGE_PY`、`PATCH_DIR`、`CONTROLLERS_JSON`、
  `SERVICE_NAME`、`GEN_SCRIPT`）。

---

### Step 0: 资产固化确认 + SSH 回退兼容确认

> **为什么这是第 0 步**：Gemini 4 指出两个前置风险——① 原型/补丁/脚本曾在 /tmp，
> 重启即丢；② VPS 本机 musepc 经 SSH 调用 `127.0.0.1:8471`，若补丁上线后它没有有效 token，
> SSH 回退通道会立即被 401 锁死（自锁）。

**0a. 资产固化确认**（本地）：

```bash
ls ~/workspace/muse-vps-deploy/
# 预期：README.md  RUNBOOK.md  RUNBOOK_V2.md  mused/  museapi-client/  scripts/
#        tests/  token-manager/  vps-auth-patch/  monitoring/  （+ 后续补充的目录）
```

预期：`muse-vps-deploy/` 完整（已在 2026-10-19 由 agent 固化，24+ 文件，测试全绿）。
若缺目录 → 先补齐，不进下一步。

**0b. 材料传到 VPS**（本地执行，scp 经同一 SSH config）：

```bash
cd ~/workspace && tar czf /tmp/muse-vps-deploy.tar.gz muse-vps-deploy/
scp -F /home/hatch/.ssh/config /tmp/muse-vps-deploy.tar.gz vps38:/root/
ssh -F /home/hatch/.ssh/config vps38 "mkdir -p /root/muse-vps-deploy && tar xzf /root/muse-vps-deploy.tar.gz -C /root/ && ls /root/muse-vps-deploy/"
```

预期输出含：`mused  museapi-client  scripts  tests  token-manager  vps-auth-patch  monitoring`

> 后面所有 VPS 侧路径以 `/root/muse-vps-deploy/` 为准：
> - patch 源码：`/root/muse-vps-deploy/vps-auth-patch/`
> - token 生成器：`/root/muse-vps-deploy/token-manager/gen_token.py`
> - 部署脚本：`/root/muse-vps-deploy/scripts/`

**0c. SSH 回退兼容确认**（vps#）——**本步骤只确认、不上线补丁**：

```bash
# 确认 VPS 本机 musepc 的调用路径
vps# which musepc; head -20 /usr/local/bin/musepc 2>/dev/null | head -20
```

记录：VPS 本机 musepc 是否直连 `127.0.0.1:8471`（是 → 它在 Step 2 后需要 fallback token；
否 → 记录实际调用方式，评估影响）。

> **关键认知**：Nginx 转发的公网请求在 8471 看来**全都是** `127.0.0.1`。
> 因此**绝不能**写 `if client_ip == "127.0.0.1": bypass`——那等于对全公网免鉴权。
> 正确做法是给本机 musepc 配一个独立 token（Step 1 生成），走完全相同的鉴权代码路径。

---

### Step 1: Token 生成与分发（vps#）

> **为什么先生成 token**：此时 8471 服务**尚未打补丁**，对现网流量零影响。
> 生成 controllers.json 只是写文件，不触及运行中的服务。等 Step 2 补丁上线重启时，
> 凭证已经就位，不存在"强制鉴权但无凭证"的空窗期。

**1a. 生成 4 个 token**（3 业务 + 1 回退专用）：

```bash
vps# mkdir -p /etc/muse-bridge /etc/musepc
vps# cd /root/muse-vps-deploy/token-manager

# 业务 token（gen_token.py 用法见 README；--output 直接写 db，原子落盘，600）
vps# python3 gen_token.py --muse super --pcs "*" --label "super-admin" --output /etc/muse-bridge/controllers.json
vps# python3 gen_token.py --muse muse-rd928 --pcs RD-928 --label "rd928-binding" --output /etc/muse-bridge/controllers.json
vps# python3 gen_token.py --muse muse-homepc --pcs homepc --label "homepc-binding" --output /etc/muse-bridge/controllers.json

# SSH 回退专用 token：pcs=["*"] 全通，但 allowed_ips 锁死本机回环
vps# python3 gen_token.py --muse ssh_fallback_admin --pcs "*" --label "ssh-fallback" \
       --output /etc/muse-bridge/controllers.json
# 注：若 gen_token.py 尚不支持 --allowed-ips 参数，生成后手动编辑 controllers.json，
# 为 ssh_fallback_admin 条目加上 "allowed_ips": ["127.0.0.1/32"]（SCHEMA.md 有字段说明）
```

预期：终端各打印一次明文 token（`muse_...`），controllers.json 只存 SHA-256。

**明文 token 的安全处理（严格执行）**：
1. 终端输出**立即**分发，不截屏、不复制到聊天窗口
2. `super` → 操作者的 Secure Vault（超管 muse 用）
3. `muse-rd928` → 经现有安全文件投递给 RD-928 的 muse（走 RD-928 本机文件，不走聊天）
4. `muse-homepc` → 同上，给 homepc 的 muse
5. `ssh_fallback_admin` → **不出 VPS**，直接写入本机文件（下一步 1b）
6. 分发完清屏（`clear` / `reset`），终端滚动历史里不留

**1b. 回退 token 落盘到本机**（vps#，**明文不出 VPS**）：

```bash
# 从终端输出复制 ssh_fallback_admin 的明文（仅此一次），写入本机文件
vps# printf '%s' "<ssh_fallback_admin 明文>" > /etc/musepc/fallback.token
vps# chmod 600 /etc/musepc/fallback.token && chown root:root /etc/musepc/fallback.token
vps# ls -la /etc/musepc/fallback.token
# 预期：-rw------- 1 root root
vps# clear   # 立即清屏
```

**1c. 验证 controllers.json**（vps#）：

```bash
vps# ls -la /etc/muse-bridge/controllers.json
# 预期：-rw-------（600）
vps# python3 /root/muse-vps-deploy/token-manager/verify.py --db /etc/muse-bridge/controllers.json --list 2>/dev/null \
  || python3 -c "import json; d=json.load(open('/etc/muse-bridge/controllers.json')); print([(k, v.get('muse'), v.get('pcs'), v.get('enabled')) for k,v in d.items()])"
# 预期：4 条目（super / muse-rd928 / muse-homepc / ssh_fallback_admin），enabled 全 true
vps# grep -c "muse_" /etc/muse-bridge/controllers.json && echo "WARN: plaintext leaked!" || echo "OK: no plaintext stored"
# 预期：OK: no plaintext stored
```

> hot-reload 自动生效（200ms 节流 mtime 检查），**token 变更不需要重启 bridge**。
> 吊销：`python3 revoke.py --muse <name> --reason "..." --yes`（或把条目 `"enabled"` 设 false），
> 最多 200ms 全局生效。

**1d. 改造 VPS 本机 musepc 包装**（vps#，约 3 行）：

```bash
vps# cp -p /usr/local/bin/musepc /usr/local/bin/musepc.bak-$(date +%Y%m%d-%H%M%S)
vps# grep -n "8471\|Authorization\|curl\|python" /usr/local/bin/musepc | head -20
```

根据实际 musepc 实现，在其向 `127.0.0.1:8471` 发请求的位置注入：

```bash
# 在 musepc 脚本顶部附近加入（示意，按实际脚本结构调整）：
FALLBACK_TOKEN="$(cat /etc/musepc/fallback.token 2>/dev/null)"
# 在构造 HTTP 请求处加入 header：
#   -H "Authorization: Bearer $FALLBACK_TOKEN"
```

验证（**此时补丁还没上，应无任何行为变化**）：

```bash
vps# /usr/local/bin/musepc "<一个无害的探测命令，如 echo fallback-ok>" 2>&1 | head -5
# 预期：与改造前行为一致（补丁未上线，header 被忽略）
```

> **本步骤完成标准**：4 token 已生成并分发；`/etc/musepc/fallback.token` 600 就位；
> VPS 本机 musepc 已注入 fallback header 且行为无变化；
> controllers.json 600、4 条目、无明文。

---

### Step 2: VPS 鉴权补丁上线（vps#）

**2a. 备份原文件**：

```bash
vps# cp -p /usr/local/bin/muse-bridge.py /usr/local/bin/muse-bridge.py.bak-$(date +%Y%m%d-%H%M%S)
vps# ls -la /usr/local/bin/muse-bridge.py.bak-* && md5sum /usr/local/bin/muse-bridge.py /usr/local/bin/muse-bridge.py.bak-*
```

预期：两个 md5 一致。**记下备份文件名**（回滚用）。

**2b. dry-run**（只检查锚点，不改文件）：

```bash
vps# BRIDGE_PY=/usr/local/bin/muse-bridge.py \
     PATCH_DIR=/root/muse-vps-deploy/vps-auth-patch \
     bash /root/muse-vps-deploy/scripts/deploy-auth-patch.sh --check-only
```

预期：插入点全命中、一行 `OK: ... compiles`、exit 0。

**如果出现 `FATAL`**（锚点不匹配）→ 停。说明 VPS 上的 bridge 版本与
`vps-auth-patch/muse-bridge-orig.py` 漂移了。先 `diff` 找出差异，修 patch 锚点后再来。
**绝不强行部署**。

**2c. 正式部署**（幂等：重复跑自动跳过已应用部分）：

```bash
vps# BRIDGE_PY=/usr/local/bin/muse-bridge.py \
     PATCH_DIR=/root/muse-vps-deploy/vps-auth-patch \
     SERVICE_NAME=muse-bridge \
     bash /root/muse-vps-deploy/scripts/deploy-auth-patch.sh
```

脚本做的事（按顺序）：
1. 再备份一次 → `/usr/local/bin/muse-bridge.py.bak-<timestamp>`
2. 精确锚点合并（加固版）：全局变量 `CONTROLLERS_PATH` + 鉴权函数
   （`load_controllers`/`verify_controller_token`/`check_target_permission`/`resolve_target`
   + 安全加固：`hmac.compare_digest`、`_valid_controller_entry`、`extract_bearer_token`、
   `_log_safe`、fail-closed 语义）
   + 双层鉴权块替换 + `/cmd`、`/batch`、`/file` 各加 target 权限检查
3. `python3 -m py_compile` 语法检查（失败则中止，已合并不回滚文件但**不重启服务**）
4. 确认 `/etc/muse-bridge/controllers.json` 存在且 600（Step 1 已建；若缺失则 fail-closed 建空文件）
5. `systemctl restart muse-bridge`

> **注意**：`deploy-auth-patch.sh` 已按安全加固重写为**从 `$PATCH_DIR/auth_patch.py` /
> `dispatch_patch.py` 实时提取** patch 代码（单一事实源），不再是脚本内硬编码旧版。
> 若脚本仍是旧版（内嵌代码），先用新版覆盖。

**2d. 服务存活检查**：

```bash
vps# systemctl is-active muse-bridge
# 预期：active
vps# sleep 2 && curl -sk https://127.0.0.1:443/muse-api/status -o /dev/null -w "%{http_code}\n"
# 预期：401（无 token → 鉴权链工作正常）
```

**如果 `is-active` 不是 active** → 看日志（§4），5 分钟修不好 → **回滚**（§3，Step 2 回滚点）。

**2e. 冒烟测试 — 7 个场景**（vps#，**全部绿灯才进 Step 3**）：

```bash
vps# API=https://127.0.0.1:443/muse-api
vps# RD928_TOKEN="<muse-rd928 明文>"      # Step 1 分发时已给操作者，此处从安全渠道取
vps# SUPER_TOKEN="<super 明文>"
vps# FALLBACK_TOKEN="$(cat /etc/musepc/fallback.token)"
```

| # | 场景 | 命令 | 预期 |
|---|------|------|------|
| 2e-1 | 错 token → 401 | `curl -sk -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer muse_wrong-xyz" $API/status` | `401` |
| 2e-2 | 越权 target → 403 | `curl -sk -H "Authorization: Bearer $RD928_TOKEN" -H "Content-Type: application/json" -d '{"target":"homepc","command":"echo hi","wait":true}' $API/cmd` | `403` |
| 2e-3 | 配对 → 200（需 RD-928 在线） | `curl -sk -H "Authorization: Bearer $RD928_TOKEN" -H "Content-Type: application/json" -d '{"target":"RD-928","command":"echo hello-from-token","wait":true}' $API/cmd` | `200`，stdout 含 `hello-from-token` |
| 2e-4 | 超管无 target → 403 | `curl -sk -H "Authorization: Bearer $SUPER_TOKEN" -H "Content-Type: application/json" -d '{"command":"echo hi","wait":true}' $API/cmd` | `403` |
| 2e-5 | 超管有 target → 200 | `curl -sk -H "Authorization: Bearer $SUPER_TOKEN" -H "Content-Type: application/json" -d '{"target":"RD-928","command":"echo super-ok","wait":true}' $API/cmd` | `200`，stdout 含 `super-ok` |
| 2e-6 | fallback token 本机直连 → 200 | `curl -sk -H "Authorization: Bearer $FALLBACK_TOKEN" -H "Content-Type: application/json" -d '{"target":"RD-928","command":"echo fallback-ok","wait":true}' http://127.0.0.1:8471/cmd` | `200`（直连 8471，验证本机 token 有效；注意这是 HTTP 非 HTTPS） |
| 2e-7 | SSH 回退 musepc 可用 | `timeout 30 /usr/local/bin/musepc "echo ssh-fallback-ok" 2>&1 \| head -3` | 输出含 `ssh-fallback-ok`（经 SSH→本机 musepc→8471 全链路，证明回退通道未被锁死） |

> **2e-7 是生死项**：它验证"补丁上线后 SSH 回退通道仍然可用"。
> 若 2e-7 失败 → 说明 fallback token 链路有问题，**立即排查**（查 `/etc/musepc/fallback.token`
> 权限/内容、musepc 注入是否生效）；15 分钟修不好 → **回滚**。

**2f. PC 客户端无感验证**：

```bash
vps# curl -sk -H "Authorization: Bearer $SUPER_TOKEN" "$API/status" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('client_list', d))"
# 预期：RD-928 和 homepc 都在 client_list 里
vps# journalctl -u muse-bridge --since "10 min ago" | grep -ci "1006"
# 预期：0（重启瞬间 1-2 次重连正常；风暴则有问题）
```

> **PC WSS 10 秒恢复判据**：若 2f 发现某台 PC 掉线，等待 10 秒后重查。
> 10 秒内未自动恢复 → **回滚**（Step 2 回滚点）。

---

### Step 3: VM 端 Daemon 旁路上线（本地，不经过 SSH）

> 此时生产流量**仍在 SSH**。Daemon 先作为旁路跑起来，只做验证，不接管流量。

**3a. 安装 daemon**（本地）：

```bash
cd ~/workspace/muse-vps-deploy/mused && bash install.sh
# 预期：unit 装到 ~/.config/systemd/user/，二进制到 ~/.local/bin/，
# 配置模板生成（不覆盖已有），空 token 文件 600
```

**3b. 写入 super token**（从 Secure Vault 取，**不回显**）：

```bash
printf '%s' "$SUPER_TOKEN" > ~/.config/mused/token   # $SUPER_TOKEN 从 vault 取
chmod 600 ~/.config/mused/token
```

**3c. 启动并健康检查**：

```bash
systemctl --user daemon-reload && systemctl --user enable --now mused
sleep 2
museapi --health
# 预期：OK: daemon alive (upstream HTTP 401)（401 也算存活，证明 TLS+nginx+bridge 链路通）
```

**3d. 延迟与重连验证**：

```bash
# 经 daemon 调 RD-928（走 HTTPS，无 SSH）
museapi --target RD-928 POST /cmd '{"command":"echo hello-via-daemon"}'
# 预期：HTTP 200，stdout 含 "hello-via-daemon"
# 第二次调用记时：目标 27-60ms（keep-alive 热连接）

# 断线重连：重启 VPS bridge 服务，观察 daemon 是否自动恢复
# （另开终端）ssh -F /home/hatch/.ssh/config vps38 "systemctl restart muse-bridge"
# 等 10 秒后重跑 museapi --health，应恢复 OK
```

> **本步骤完成标准**：daemon `active (running)`；`--health` OK；
> 经 daemon 的 /cmd 200 且延迟达标；模拟断线后 10 秒内自愈。

---

### Step 4: 切换流量（本地）

前提：Step 2 七场景全绿 + Step 3 旁路验证通过。

**4a. 双跑观察 24h**：
musepc wrapper 优先走 daemon（Unix Socket），失败自动回退 SSH。
（wrapper 改造是独立小任务；在那之前，日常调用手动用 `museapi`，SSH 留作备份。
两种路径并存，互为备份。）

**4b. 切流**（24h 无异常后）：
把 `~/workspace/bin/musepc` 默认改走 daemon：
daemon socket 存在且 `/status` 200 → 走 museapi；否则走 SSH。
（具体改法见实施任务，本 runbook 只定切换策略。）

**4c. 回归测试**（切流后立即执行，19 用例见 `INTEGRATION_TEST.md` T00–T13 全过才算数）：
- /cmd、/batch、/file 三类端到端回归
- 鉴权负例（无 token 401、错 token 401、越权 403、超管无 target 403）

**4d. 7 天观察期**（指标见 §5）：
一切正常 → 才考虑关闭 SSH（删 VPS authorized_keys，**最后一步，不可逆前再三确认**）。

> **SSH 在整个过渡期保留**。只有 §5 的 7 天观察期全绿，才动 SSH key。
> 关闭 SSH 的同时，将 controllers.json 里 `ssh_fallback_admin` 的 `"enabled"` 设为 `false`
> （热加载秒级生效，无需重启），彻底下线回退通道。

---

## 2. 回滚方案

### 2.1 Step 2 回滚点（VPS 补丁上线阶段）

**触发条件（任一即回滚，不犹豫）**：

| # | 信号 | 阈值 |
|---|------|------|
| R2-1 | 8471 服务重启失败 | `systemctl is-active` 非 active，5 分钟内修不好 |
| R2-2 | 冒烟测试不符合预期 | 2e-1 ~ 2e-7 任一失败，15 分钟排查无头绪 |
| R2-3 | PC WSS 未恢复 | 重启后 10 秒内 client_list 缺失且不自动恢复 |
| R2-4 | SSH 回退通道失效 | 2e-7 失败（`/usr/local/bin/musepc` 经 SSH 调用失败率 > 0） |

**回滚动作**（vps#，按顺序）：

```bash
# R1. 恢复 bridge 文件（用 Step 2a 的手动备份）
vps# ls -t /usr/local/bin/muse-bridge.py.bak-* | head -3
vps# cp /usr/local/bin/muse-bridge.py.bak-<Step2a记下的时间戳> /usr/local/bin/muse-bridge.py
vps# md5sum /usr/local/bin/muse-bridge.py
# 对照 0.3 记录的 md5，必须一致

# R2. 语法检查 + 重启（或一键：bash /root/muse-vps-deploy/scripts/deploy-auth-patch.sh --rollback）
vps# python3 -m py_compile /usr/local/bin/muse-bridge.py && echo "syntax OK"
vps# systemctl restart muse-bridge && systemctl is-active muse-bridge
# 预期：active

# R3. 验证回到部署前状态
vps# curl -sk https://127.0.0.1:443/muse-api/status -o /dev/null -w "%{http_code}\n"
# 预期：有响应（401 或 200）；关键是服务活着
vps# curl -sk https://127.0.0.1:443/muse-api/status | python3 -c "import json,sys; print(json.load(sys.stdin).get('client_list'))"
# 预期：两台 PC 重新上线（最多等 1-2 分钟，插件自动重连）
vps# timeout 30 /usr/local/bin/musepc "echo rollback-ok" 2>&1 | head -3
# 预期：rollback-ok（SSH 回退通道恢复）
```

**回滚后**：
1. 终端确认 PC 上线 + SSH 可用后，通知用户"已回滚，服务恢复"
2. 保留现场：`journalctl -u muse-bridge --since "<部署开始时间>" > /root/bridge-fail-$(date +%Y%m%d-%H%M%S).log`
3. 不删除备份文件；分析日志找到根因后再定下一次窗口

### 2.2 Step 4 回滚点（切换流量阶段）

**触发条件（任一即回滚，不犹豫）**：

| # | 信号 | 阈值 |
|---|------|------|
| R4-1 | 端到端回归失败 | /cmd、/batch、/file 任一类回归测试失败 |
| R4-2 | 非预期鉴权/网关错误 | 连续出现非预期的 401/403（凭证热加载异常）或 502/504（Nginx↔8471 超时） |
| R4-3 | 错误率超标 | 5 分钟内非业务级传输错误率 > 1% |
| R4-4 | 延迟退化 | 扣除命令自身执行时间后的网络转发 P95 > 150ms（显著偏离 67ms 目标） |
| R4-5 | Daemon 异常 | 高并发下请求挂死，或 Daemon 内存持续增长显著突破 20MB 基准 |

**回滚动作**（本地，秒级）：
```bash
# 立即将 ~/workspace/bin/musepc 切回 SSH（改 wrapper 默认分支，或临时导出环境变量强制 SSH）
pkill -f mused.py 2>/dev/null; rm -f ~/.config/mused/mused.sock
# musepc wrapper 的 SSH 分支本来就是默认，回退即生效
# 保留 daemon 日志：cp ~/.local/share/mused/mused.log /root/mused-fail-$(date +%Y%m%d-%H%M%S).log
```

**回滚后**：VPS 侧**不动**（补丁和 token 保持上线，只是 VM 不再走 daemon）。
离线分析 daemon 日志找到根因，修好后再走一遍 Step 3→Step 4。

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
| `systemctl restart` 后 inactive | patch 语法错误（py_compile 应已拦截）或端口被占 | `journalctl -u muse-bridge -p err --since "5 min ago"` | 看 traceback；修不好 → 回滚 §2.1 |
| 2e-1 返回 200 而不是 401 | patch 没生效（服务跑的还是旧文件） | `vps# grep -c "verify_controller_token" /usr/local/bin/muse-bridge.py`（应 >0）；`systemctl show muse-bridge -p ExecStart` 确认跑的是 `/usr/local/bin/muse-bridge.py` | 重新跑 Step 2c |
| 2e-3 返回 403 但 target 是对的 | controllers.json 里 pcs 写错（如大小写） | 查 controllers.json 核对 pcs | 修正 json 后等 hot-reload（≤200ms 节流） |
| 2e-3 返回 401 | token 复制错 / 多了换行 | VPS 侧 digest 对不上；本地 `wc -c ~/.config/mused/token` 看长度是否异常 | 从 vault 重取，`printf '%s'` 重写 |
| 2e-6 fallback 返回 401 | `/etc/musepc/fallback.token` 权限/内容不对，或 controllers.json 缺 ssh_fallback_admin | `ls -la /etc/musepc/fallback.token`；查 json 条目 | 重写 token 文件；确认 allowed_ips 含 127.0.0.1/32 |
| 2e-7 SSH 回退失败 | musepc 未正确注入 fallback header | `grep -n "fallback" /usr/local/bin/musepc` | 按 Step 1d 重改；仍不行 → 回滚 §2.1 |
| PC 掉线不重连 | 重启瞬间正常（插件有重连）；超过 10 秒不回 | `journalctl -u muse-bridge --since "10 min ago" \| grep -iE "1006\|disconnect" \| tail` | 让用户 Reload Window；仍不行 → 回滚 |
| daemon 连不上 VPS | egress 代理变量没透 / SNI 错 / CA 路径错 | `curl -v` 看 CONNECT 阶段；`python3 client.py` 裸测 | 对照 `museapi-client/` 的 recipe 逐段排查 |
| `--check-only` 报 FATAL | VPS bridge 版本漂移 | `diff /root/muse-vps-deploy/vps-auth-patch/muse-bridge-orig.py /usr/local/bin/muse-bridge.py`（先备份！） | 修锚点，不强上 |
| 2e-2 越权未被 403 | patch 的 target 检查没生效 | `grep -n "check_target_permission" /usr/local/bin/muse-bridge.py` | 重新跑 Step 2c；检查 dispatch 插入点 |

### 快速诊断一键包（vps#，出问题先跑这个，把输出贴给排查者）

```bash
vps# {
echo "=== service ==="; systemctl is-active muse-bridge
echo "=== md5 ==="; md5sum /usr/local/bin/muse-bridge.py
echo "=== patch marker ==="; grep -c "verify_controller_token" /usr/local/bin/muse-bridge.py
echo "=== controllers ==="; ls -la /etc/muse-bridge/controllers.json 2>/dev/null
echo "=== fallback token ==="; ls -la /etc/musepc/fallback.token 2>/dev/null
echo "=== api ==="; curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1:443/muse-api/status
echo "=== clients ==="; curl -sk https://127.0.0.1:443/muse-api/status | head -c 300; echo
echo "=== errors ==="; journalctl -u muse-bridge -p err --since "15 min ago" | tail -10
} 2>&1 | tee /root/bridge-diag-$(date +%Y%m%d-%H%M%S).txt
```

---

## 4. 部署后验证清单（7 天观察期）

从 Step 4 切流成功当天算起，每天看一次，记到检查表里：

| # | 指标 | 怎么看 | 正常线 | 异常动作 |
|---|------|--------|--------|----------|
| 1 | 服务存活 | `systemctl is-active muse-bridge` | 持续 active | 非 active → 按 §3 排查 |
| 2 | 401/403 比例 | `journalctl -u muse-bridge --since "24 hours ago" \| grep -cE " 401 \| 403 "` vs 总请求 | 401/403 只来自探测；无突发增长 | 突增 → 查是否 token 泄露被刷 |
| 3 | 鉴权审计行 | 日志里 `[bridge] <muse> -> <target>` | 每次 controller 调用都有 | 缺失 → patch 日志被关了，检查 |
| 4 | fallback 使用 | 日志里 ssh_fallback_admin 调用 | 逐日下降 → 0（切流后） | 切流后仍大量 → 有调用方没切，找出并迁移 |
| 5 | legacy 使用 | 日志里 `Legacy global token used` | 逐日下降 → 0 | 7 天后仍大量 → 有调用方没切，找出并迁移 |
| 6 | PC 在线 | `/muse-api/status` 的 client_list | 两台常年在线 | 掉线 → 先看插件侧，再看 VPS |
| 7 | 1006 计数 | `journalctl ... \| grep -c 1006`（每天） | 接近 0 | 风暴 → 回滚 §2.1 |
| 8 | 端到端延迟 | `museapi --target RD-928 POST /cmd '{"command":"echo t"}'` 记时 | 稳态 <100ms（热连接 27-60ms） | 持续 >300ms → 查 VPS 负载 / 网络 |
| 9 | controllers.json 完整性 | `ls -la` 权限 + 条目数 | 600、4 条、enabled 全 true | 权限变 → 立刻改回 600 并查原因 |
| 10 | fallback.token 完整性 | `ls -la /etc/musepc/fallback.token` | 600、root:root | 异常 → 立刻修复；SSH 回退依赖它 |
| 11 | 磁盘/日志 | `df -h /` + `journalctl --disk-usage` | 余量充足 | journal 膨胀 → 设 `SystemMaxUse` |

**7 天全绿 → 才允许动 SSH key**（删 VPS authorized_keys，**最后一步，不可逆前再三确认**）。
同时将 controllers.json 里 `ssh_fallback_admin` 的 `"enabled"` 设为 `false`
（热加载秒级生效，无需重启），彻底下线回退通道。
任一天出现"异常动作"列的回滚级事件，先回滚再分析，不带病运行。

---

## 5. 附：文件与路径总表

| 路径（VPS） | 说明 |
|-------------|------|
| `/usr/local/bin/muse-bridge.py` | 被 patch 的目标 |
| `/usr/local/bin/muse-bridge.py.bak-<ts>` | 自动+手动备份 |
| `/usr/local/bin/musepc` | VPS 本机 musepc（已注入 fallback header，备份 `.bak-<ts>`） |
| `/etc/muse-bridge/controllers.json` | 600，只存 SHA-256，4 条目 |
| `/etc/muse-bridge/token` | legacy 全局 token（过渡期） |
| `/etc/musepc/fallback.token` | 600，ssh_fallback_admin 明文（**永不出 VPS**） |
| `/root/muse-vps-deploy/` | 本次部署材料 |
| `/etc/ssl/certs/vps38-root-ca.pem` | VPS38 Root CA（VM 侧校验用） |

| 路径（本地 VM） | 说明 |
|-----------------|------|
| `~/workspace/muse-vps-deploy/` | 材料总库 |
| `~/workspace/bin/musepc` | 现有 SSH wrapper（过渡期保留） |
| `~/workspace/bin/museapi` | daemon CLI（Step 3 部署） |
| `~/.config/mused/config.json` | daemon 配置（600，无 token） |
| `~/.config/mused/token` | super 明文 token（600，vault 分发） |
| `~/.local/share/mused/mused.log` | daemon 日志（轮转 10MB×5） |

---

## 6. v1 → v2 差异对照表

| 维度 | v1（RUNBOOK.md） | v2（本文件） | 原因 |
|------|------------------|-------------|------|
| 部署顺序 | Step 2 先打补丁 → Step 3 再生成 token | Step 1 先生成 token → Step 2 再打补丁 | v1 存在"强制鉴权但无凭证"空窗期；v2 生成时服务未动，零影响 |
| Step 0 | 仅"传材料到 VPS" | 资产固化确认 + SSH 回退兼容确认 | /tmp 丢失风险；回退自锁风险（Gemini 4 Q3） |
| 回退 token | 无（依赖 legacy 全局 token） | 新增 `ssh_fallback_admin`（pcs=*，allowed_ips=127.0.0.1/32，`/etc/musepc/fallback.token` 600） | legacy 是过渡期的全局 token，权限过大；专用 token 可一键下线（enabled=false）；**严禁 127.0.0.1 白名单**（=全网裸奔，Gemini 4 Q8） |
| 本机 musepc | 未提及 | Step 1d 约 3 行改造，注入 fallback header | 否则补丁上线后 SSH 回退立即 401 自锁 |
| 冒烟测试 | 6 场景（4a–4f） | 7 场景（2e-1–2e-7），新增 fallback 本机直连 + SSH 回退全链路 | 2e-7 是生死项：验证回退通道未被锁死 |
| 回滚点 | 笼统 4 类触发 | Step 2 / Step 4 双回滚点，量化阈值（10s / 5min / 1% / 150ms / 20MB） | Gemini 4 Q3 要求明确判据，不靠感觉 |
| Step 4 回滚 | 切回 SSH（笼统） | 切回 SSH，VPS 侧不动（补丁+token 保持），离线分析 daemon 日志 | 缩小回滚 blast radius；VPS 鉴权层本身已验证 OK |
| 观察期指标 | 9 项 | 11 项（+fallback 使用、+fallback.token 完整性） | 回退通道本身也要被监控 |
| 下线回退 | 只提删 SSH key | 删 SSH key **同时**把 ssh_fallback_admin enabled=false（热加载秒级） | 一键平滑下线，无需改代码/重启 |
| patch 版本 | 旧版（80 行） | 加固版（含 SECURITY_REVIEW.md 7 修复：fail-closed、条目校验、权限检查、类型守卫、日志脱敏、Bearer 解析） | 安全加固 agent 已落地，30/30 测试 |
| token 工具 | gen-tokens.sh（批量） | token-manager/ 全套（gen/verify/rotate/revoke/common + SCHEMA + SECURITY，21/21 测试） | 生产级：轮换、吊销、审计 |
