# 混沌测试（故障注入）方案

**版本**: v1.0 (2026-10-19)
**适用架构**: 去 SSH v2 架构（VM daemon → HTTPS → VPS 8471 → WSS → PC）
**前置条件**: 部署完成后执行；所有测试在低峰期进行；SSH 回退通道保持可用
**原则**: 每次只注入一个故障；注入前记录基线；注入后验证恢复；任何场景触发回滚判据立即停止

---

## 测试环境基线（注入前记录）

| 指标 | 记录命令 | 基线值 |
|------|----------|--------|
| Daemon 进程 | `systemctl --user status mused` | active (running) |
| 连接池状态 | `museapi --health` | OK |
| VPS 8471 进程 | VPS: `systemctl status muse-api` | active |
| WSS 在线 PC | `curl /muse-api/status` | RD-928, homepc 在线 |
| Daemon 内存 | `ps -o rss= -p $(pgrep -f mused.py)` | ~20MB |
| 端到端延迟 | `museapi --target RD-928 "echo chaos-baseline"` | 记录 P50 |

---

## C-01: VPS 8471 进程被 kill -9

### 注入方法
```bash
# 在 VPS 上执行（经 SSH，最后一次允许使用 SSH 做故障注入）
ssh -F /home/hatch/.ssh/config vps38 "sudo pkill -9 -f 'muse-bridge.py|8471'"
```

### 预期行为
1. **Daemon 侧**: HTTPS 请求收到连接拒绝（ECONNREFUSED）或超时；`Upstream.request()` 捕获异常后丢弃坏连接；heartbeat 线程检测到失败，进入指数退避（1s→2s→4s...上限30s）；连接池中 16 条连接逐条被标记 broken 并丢弃
2. **Nginx 侧**: 返回 502 Bad Gateway 给 Daemon（upstream 不可用）
3. **WSS 侧**: 所有 PC 的 WSS 长连接断开；PC 插件进入重连循环（指数退避）
4. **request_id 状态机**: VPS 进程被杀，内存中的 `DedupManager` 全部丢失；处于 `IN_FLIGHT` 的请求：
   - 若 Daemon 重试时 VPS 已重启 → 无记录 → 视为新请求正常执行（at-most-once 语义下这是可接受的，因为旧进程已死，命令是否执行未知）
   - Daemon 必须向上层返回 `EXECUTION_UNCERTAIN` 而非静默重发

### 判定标准（PASS/FAIL）
- [ ] Daemon 在 8471 重启后 **60 秒内**自动恢复（heartbeat 探测成功，无需人工干预）
- [ ] Daemon 日志中有明确的 `heartbeat failed` → `upstream warm OK` 恢复记录
- [ ] 连接池重建后 `museapi --health` 返回 OK
- [ ] 所有 PC 的 WSS 在 **120 秒内**自动重连（查 VPS 日志 `PC ... connected`）
- [ ] 注入期间发出的请求：要么返回明确错误（502/504/EXECUTION_UNCERTAIN），**绝不静默丢失或重复执行**
- [ ] Daemon 内存无泄漏（恢复后 RSS 与基线差值 < 5MB）

### 恢复步骤
1. 在 VPS 上重启服务：`sudo systemctl restart muse-api`（或等待 systemd 自动拉起，视配置而定）
2. 验证：`curl -k https://127.0.0.1/muse-api/status` 返回 200/401（服务活着）
3. 在 VM 上：`museapi --health` 确认 Daemon 恢复
4. 端到端验证：`museapi --target RD-928 "echo chaos-c01-recovered"`
5. 检查 WSS：确认两台 PC 重新在线

---

## C-02: Nginx 重启（连接池 16 条全断）

### 注入方法
```bash
ssh -F /home/hatch/.ssh/config vps38 "sudo systemctl restart nginx"
# Nginx 重启约 2-5 秒，期间所有 TLS 连接被 RST
```

### 预期行为
1. **连接池**: 16 条 HTTPS 长连接同时收到 RST/FIN；下次 `get_connection()` 取用时：
   - 若在 `request()` 中抛出 `RemoteDisconnected`/`BrokenPipeError` → 捕获 → 丢弃该连接 → 从池新建（触发 TLS 握手 + session resume）
   - 若连接在池中空闲（未被取用），下次取用时检查 `last_used_at`，Nginx 重启必然导致旧连接失效，重建
2. **TLS 会话复用**: 重建时使用缓存的 `SSLSession` 做 1-RTT 快速握手（而非完整 2-RTT）
3. **微批**: 正在 1.5ms 收集窗口中的请求不受影响（窗口在 Daemon 本地内存）
4. **无雪崩**: 16 条连接不会同时重建——按需懒重建，只有实际有请求的槽位才建新连接

### 判定标准（PASS/FAIL）
- [ ] Nginx 重启后 **30 秒内** Daemon 自动恢复（无需重启 Daemon）
- [ ] 重建的连接使用 TLS session resume（查 Daemon 日志 `session resumed` 或握手时间 < 100ms）
- [ ] 恢复后 `benchmark.py` 热连接 P50 仍 < 60ms
- [ ] 无连接泄漏：`ss -tn | grep 443 | wc -l` 在 VM 上稳定在 ≤16+ε
- [ ] 注入期间的请求：失败的返回明确错误，**不卡死**（5s 池等待超时必须触发）

### 恢复步骤
1. Nginx 重启是自恢复的，无需手动干预
2. 若 Daemon 未在 30s 内恢复：`systemctl --user restart mused`，查日志定位
3. 验证连接池：`museapi --health` + 跑一次 `benchmark.py` 确认延迟

---

## C-03: controllers.json 被写坏（无效 JSON）

### 注入方法
```bash
# 在 VPS 上（先备份！）
ssh -F /home/hatch/.ssh/config vps38 "cp /etc/muse-bridge/controllers.json /tmp/controllers.json.bak && echo '{invalid json{{{' > /etc/muse-bridge/controllers.json"
```

### 预期行为
1. **FastAuthEngine**: 下次 200ms 节流检查发现 `st_mtime_ns` 变化 → 尝试 `json.load` → 抛异常 → **保留旧快照继续服务**（`except Exception: pass`），打告警日志
2. **决策缓存**: 不清空（因为 reload 失败，`_controllers_by_sha256` 和 `_decision_cache` 保持原样）
3. **现网流量**: 零影响——所有合法 token 继续通过快路径（<0.8μs）
4. **新 token**: 写坏期间生成的 token 无法生效（预期行为，fail-closed 对"读坏"但 fail-open 对"服务连续性"——这是正确权衡）

### 判定标准（PASS/FAIL）
- [ ] 写坏后 **5 分钟内**所有已有 token 的请求全部正常（200/403 语义不变）
- [ ] VPS 日志中有 `controllers.json reload failed, keeping old snapshot` 告警（或类似）
- [ ] 无 500 错误（鉴权层绝不崩溃）
- [ ] 恢复文件后 **200ms 内**新配置生效（验证：吊销一个测试 token，确认 401）

### 恢复步骤
1. 恢复文件：`sudo cp /tmp/controllers.json.bak /etc/muse-bridge/controllers.json && sudo chmod 600 /etc/muse-bridge/controllers.json`
2. 验证：用测试 token 确认鉴权正常
3. **根因**: 检查是哪个脚本写坏的——必须走 `os.replace()` 原子写入（见 token-manager/common.py）；禁止直接覆写

---

## C-04: 某条 WSS 断开 30 秒（单 PC 失联）

### 注入方法
```bash
# 方法 A（推荐）: 在 VPS 上 iptables 丢弃到该 PC 的包 30 秒
ssh -F /home/hatch/.ssh/config vps38 "sudo iptables -A OUTPUT -d <PC公网IP> -j DROP && sleep 30 && sudo iptables -D OUTPUT -d <PC公网IP> -j DROP"

# 方法 B: 让 PC 插件主动断开（在 RD-928 上执行）
# VS Code 命令面板 → "Muse Bridge: Disconnect" → 等 30 秒 → "Muse Bridge: Reconnect"
```

### 预期行为
1. **VPS 侧**: 8471 检测到 WSS 心跳超时（或 TCP 超时）→ 标记该 PC 离线 → 该 PC 的 `high_prio`/`bulk` 队列暂停调度
2. **request_id 状态机**: 该 PC 上处于 `IN_FLIGHT` 的请求 → 标记为 `UNCERTAIN`（WSS 断开时命令执行状态未知）
3. **Daemon 重试**: 若 Daemon 用相同 request_id 重试 → VPS 查到 `UNCERTAIN` → **拒绝重发**，返回 `504 EXECUTION_UNCERTAIN`
4. **新请求**: 发往该 PC 的新 /cmd → 立即返回 `PC_OFFLINE`（fail-fast，不排队等待）
5. **30 秒后重连**: PC 插件自动重连 WSS → VPS 恢复该 PC 的队列调度 → 新请求正常

### 判定标准（PASS/FAIL）
- [ ] 断开期间发往该 PC 的请求在 **<1s 内**返回 `PC_OFFLINE`（不 hang）
- [ ] 断开前已在飞的请求返回 `504 EXECUTION_UNCERTAIN`（**不是**静默重发，也**不是**假装成功）
- [ ] 另一台 PC 的请求完全不受影响（隔离性）
- [ ] 重连后 **10 秒内**该 PC 恢复在线，新请求正常执行
- [ ] `UNCERTAIN` 状态的 request_id 在 TTL 后被清理，不泄漏内存

### 恢复步骤
1. 30 秒后自动恢复（iptables 规则已删除 / 插件已重连）
2. 若未自动恢复：检查 VPS 日志确认 WSS 握手；在 PC 上手动重连插件
3. 对返回 `UNCERTAIN` 的命令：人工确认 PC 侧实际执行状态后再决定是否重发（**严禁自动重发非幂等命令**）

---

## C-05: Daemon 被 kill（systemd 拉起）

### 注入方法
```bash
# 在 VM 上
pkill -9 -f "mused.py"
# 或
kill -9 $(pgrep -f mused.py)
```

### 预期行为
1. **systemd**: `Restart=always` + `RestartSec=5` → 5 秒内拉起新进程
2. **Unix Socket**: 旧进程被 -9 杀死时未来得及 `unlink` socket 文件；新进程启动时 `bind()` 前先 `os.unlink(sock_path)`（已实现），不会报 `Address already in use`
3. **连接池**: 全新进程 → 4 条连接预热重建（TLS 握手，约 250ms/条，可并行）
4. **微批**: 内存中的微批窗口丢失；kill 瞬间在 1.5ms 窗口中的请求 → 客户端（museapi CLI）收到连接断开错误 → 由调用方决定重试（CLI 是短命进程，不自动重试）
5. **request_id**: Daemon 侧不存状态（状态机在 VPS），无影响

### 判定标准（PASS/FAIL）
- [ ] systemd 在 **10 秒内**拉起新进程（`systemctl --user status mused` 显示 active，新 PID）
- [ ] Unix Socket 可用：`museapi --health` 在拉起后 **15 秒内**返回 OK（含 4 条连接预热时间）
- [ ] 无 `Address already in use` 错误（socket 清理正确）
- [ ] 拉起后内存回到 ~20MB 基线（无泄漏累积）
- [ ] `journalctl --user -u mused` 中有清晰的 kill → restart → warm OK 日志链

### 恢复步骤
1. systemd 自动恢复，无需手动干预
2. 若未自动拉起：`systemctl --user start mused`，检查 `systemctl --user status mused` 的错误信息
3. 若 socket 残留导致 bind 失败：`rm -f ~/.local/share/mused/mused.sock`（或配置的 socket_path）后重启

---

## C-06: 网络分区（VM→VPS 丢包 50%）

### 注入方法
```bash
# 在 VM 上用 tc 模拟 50% 丢包（ egress 方向）
sudo tc qdisc add dev eth0 root netem loss 50%
# 测试完成后务必删除：
# sudo tc qdisc del dev eth0 root
```

> ⚠️ **警告**: 这会影响 VM 上所有出站流量（包括 SSH）。确保有带外管理通道，或设置 `at` 定时任务自动删除规则：
> ```bash
> echo "sudo tc qdisc del dev eth0 root" | at now + 5 minutes
> ```

### 预期行为
1. **连接池**: 约一半请求遇到超时/重传；TCP 重传退避；部分连接被标记 broken 后重建
2. **重试**: `museapi.py` 的重试逻辑**只重试 GET/HEAD/OPTIONS**，POST（/cmd）**永不自动重试**（防非幂等重复执行）
3. **微批**: 1.5ms 窗口内的请求若遇到网络超时，整个 /batch 可能失败 → 按 sub_request_id 拆分后，调用方看到部分失败
4. **无重试风暴**: 因为 POST 不重试 + 指数退避，请求量不会放大；VPS 侧 QPS 不会因重试而翻倍
5. **TLS**: 丢包 50% 时 TLS 握手可能多次重传，新连接建立时间从 250ms 恶化到数秒

### 判定标准（PASS/FAIL）
- [ ] 50% 丢包下，GET /status 类请求的**最终成功率 > 80%**（靠 TCP 重传，非应用层重试）
- [ ] POST /cmd **不自动重试**（查 Daemon 日志，确认无重复 POST）
- [ ] VPS 侧观测到的 QPS **不超过** VM 侧发出 QPS 的 1.2 倍（无重试风暴）
- [ ] 恢复网络后（删除 tc 规则）**60 秒内**延迟回到基线
- [ ] Daemon 不崩溃、不 OOM（丢包期间内存稳定）

### 恢复步骤
1. 删除 tc 规则：`sudo tc qdisc del dev eth0 root`
2. 验证：`museapi --health` + `benchmark.py` 确认延迟恢复
3. 若 Daemon 状态异常：`systemctl --user restart mused`

---

## C-07: Token 被吊销（enabled=false，200ms 全网生效）

### 注入方法
```bash
# 在 VPS 上，用 revoke.py 吊销一个测试 token（不要吊销生产 token！）
ssh -F /home/hatch/.ssh/config vps38 "cd /root/muse-vps-deploy/token-manager && python3 revoke.py --muse test-chaos --reason 'chaos test C-07' --yes"
# revoke.py 内部用 os.replace() 原子更新 controllers.json
```

### 预期行为
1. **原子写入**: `revoke.py` 写临时文件 → `os.replace()` → `st_mtime_ns` 变化
2. **FastAuthEngine**: 下次 200ms 节流检查发现 mtime 变化 → 重载 → 原子切换 `_controllers_by_sha256` → **清空 `_decision_cache`**
3. **生效延迟**: 最多 200ms（节流窗口）+ 1 次请求 = 吊销后下一个请求即 401
4. **已缓存的决策**: 旧缓存被清空，不会用过期缓存放行
5. **IN_FLIGHT 请求**: 吊销前已通过鉴权进入 IN_FLIGHT 的请求**继续执行**（RCU 语义：旧快照），吊销只影响新请求

### 判定标准（PASS/FAIL）
- [ ] 吊销后 **500ms 内**，用该 token 的新请求返回 401（从执行 revoke.py 开始计时）
- [ ] 吊销前已在飞的请求正常完成（不被中途杀死）
- [ ] 其他 token 完全不受影响
- [ ] VPS 日志中有 `controllers.json reloaded` + `decision cache cleared` 记录
- [ ] 重新启用（enabled=true）后，200ms 内恢复 200

### 恢复步骤
1. 恢复测试 token：`python3 revoke.py` 没有 un-revoke，用 `gen_token.py --muse test-chaos ...` 重新生成，或手动改 controllers.json 把 enabled 改回 true（走原子写入）
2. 验证：`verify.py` 确认 token 有效
3. **生产注意**: 吊销生产 token 前必须先分发新 token 并确认 Daemon 已加载，否则会造成服务中断

---

## C-08: /file 传输中 VPS 重启（分片重传 + 去重）

### 注入方法
```bash
# 步骤 1: 在 VM 上启动一个大文件传输（>1MB，会被拆成多个 64KB 分片）
museapi --target RD-928 --write /tmp/chaos-bigfile.bin < /tmp/bigfile.bin &
TRANSFER_PID=$!

# 步骤 2: 传输进行中（约 2 秒后），在 VPS 上重启 8471
sleep 2 && ssh -F /home/hatch/.ssh/config vps38 "sudo systemctl restart muse-api"

# 步骤 3: 等待传输结束，检查结果
wait $TRANSFER_PID; echo "exit: $?"
```

### 预期行为
1. **传输中断**: VPS 重启时，正在传输的 /file 分片丢失（WSS 断开，VPS 内存中的分片组装状态丢失）
2. **Daemon 侧**: 收到连接断开 → 该 /file 请求返回错误（**不自动重传整个文件**，因为 /file 写操作非幂等——部分分片可能已写入 PC）
3. **去重状态机**: VPS 重启后 `DedupManager` 丢失；若 Daemon 用**新的** request_id 重试整个 /file → 正常执行（这是正确的，因为旧的分片状态已丢失，无法续传）
4. **PC 侧**: PC 插件收到不完整的分片序列 → 丢弃不完整文件（或保留 .partial，由调用方决定）
5. **正确做法**: 调用方（AI 助手）收到传输失败后，应该用**新的 request_id** 重新发起整个 /file 传输，而不是试图续传

### 判定标准（PASS/FAIL）
- [ ] 传输中断后，调用方在 **5 秒内**收到明确错误（不是 hang）
- [ ] PC 侧没有残留损坏文件（或损坏文件有 `.partial` 后缀可识别）
- [ ] 用新 request_id 重传后，文件完整且 md5 一致
- [ ] VPS 重启后，`DedupManager` 从空开始，无旧状态残留（无内存泄漏）
- [ ] **绝不出现**: 同一个分片被写入两次导致文件损坏

### 恢复步骤
1. 确认 VPS 8471 已重启：`curl -k https://127.0.0.1/muse-api/status`
2. 确认 PC WSS 重连
3. 重新传输文件并验证 md5
4. 清理 PC 侧的 `.partial` 残留文件

---

## 执行顺序与组合

| 阶段 | 场景 | 说明 |
|------|------|------|
| 第一轮（单故障） | C-03, C-07 | 只动配置文件，不杀进程，风险最低 |
| 第二轮（进程级） | C-01, C-02, C-05 | 杀进程/重启服务，验证自愈 |
| 第三轮（网络级） | C-04, C-06 | 断网/丢包，验证降级 |
| 第四轮（复合） | C-08 | 传输中重启，最复杂 |

**组合测试**（全部单项通过后）：
- C-01 + C-07：VPS 重启的同时吊销 token，验证恢复后新鉴权生效
- C-06 + C-04：丢包的同时某 PC 断开，验证隔离性

---

## 回滚触发（任一出现立即停止测试）

1. 任一场景的恢复时间超过判定标准的 **3 倍**
2. 出现**非预期的重复命令执行**（at-most-once 被破坏）
3. Daemon 或 VPS 进程崩溃后**无法自动恢复**
4. PC 侧出现**文件损坏**且无法通过重传修复
5. 监控告警中出现 **CRITICAL** 且 5 分钟内未自动消除

回滚动作：按 `RUNBOOK_V2.md` 切回 SSH，保留所有日志离线分析。

---

## 测试记录表

| 场景 | 执行日期 | 执行人 | 结果 | 恢复时间 | 备注 |
|------|----------|--------|------|----------|------|
| C-01 | | | ☐PASS ☐FAIL | | |
| C-02 | | | ☐PASS ☐FAIL | | |
| C-03 | | | ☐PASS ☐FAIL | | |
| C-04 | | | ☐PASS ☐FAIL | | |
| C-05 | | | ☐PASS ☐FAIL | | |
| C-06 | | | ☐PASS ☐FAIL | | |
| C-07 | | | ☐PASS ☐FAIL | | |
| C-08 | | | ☐PASS ☐FAIL | | |
