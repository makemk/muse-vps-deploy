# 压力测试方案（LOAD TEST）

目标：量化新架构（VM → HTTPS → VPS）在并发下的真实表现，找到天花板，
并与 SSH 基线做 apples-to-apples 对比。

**前置条件**：VPS 鉴权 patch 已部署、token 已生成、至少一台 PC 在线。
未部署前不要跑——请求会 401/404，数字无意义。

脚本：[`loadtest.py`](sandbox:///home/hatch/workspace/muse-vps-deploy/loadtest.py)
（Python 标准库，无第三方依赖）

---

## 1. 测试场景矩阵

| 场景 | mode | 并发 | 时长 | 测的是哪一段 |
|------|------|------|------|--------------|
| S-10 | status | 10 | 60s | VPS HTTP 层，低并发基线 |
| S-50 | status | 50 | 60s | VPS HTTP 层，中并发 |
| S-100 | status | 100 | 60s | VPS HTTP 层，高并发 |
| S-200 | status | 200 | 60s | VPS HTTP 层，找拐点 |
| C-10 | cmd | 10 | 60s | 端到端（含 PC 执行），低并发 |
| C-50 | cmd | 50 | 60s | 端到端，高并发（PC 很可能先撑不住） |
| B-10 | ssh 基线 | 10 | 60s | 旧链路对比 |

`status` 模式只打 VPS 的 `/muse-api/status`（纯 HTTP，无 PC 参与），
隔离出"传输层"本身的性能。
`cmd` 模式打 `/muse-api/cmd`（`echo loadtest-ping`，`wait=true`），
走完整链路：VPS → WSS → PC 执行 → 返回。

### 运行命令

```bash
export MUSE_API_TOKEN=$(cat ~/.config/mused/token)

# S-10 .. S-200（VPS 层）
for c in 10 50 100 200; do
  python3 loadtest.py --mode status --concurrency $c --duration 60 \
      --output results/status_c${c}.csv
done

# C-10 / C-50（端到端，要求 RD-928 在线）
for c in 10 50; do
  python3 loadtest.py --mode cmd --target RD-928 --concurrency $c \
      --duration 60 --output results/cmd_c${c}.csv
done

# B-10（SSH 基线对比）
python3 loadtest.py --baseline-ssh --target RD-928 --concurrency 10 \
    --duration 60 --output results/ssh_baseline_c10.csv
```

### 采集指标

每个场景记录：

| 指标 | 含义 | 合格线（status 模式） |
|------|------|----------------------|
| QPS | 总吞吐 | S-10 ≥ 300（10 并发 × ~30ms） |
| ok QPS | 成功吞吐 | ≈ QPS（错误率 < 1%） |
| p50 延迟 | 中位数 | ≤ 40ms |
| p95 延迟 | 95 分位 | ≤ 80ms |
| p99 延迟 | 99 分位 | ≤ 150ms |
| 错误率 | 非 200 占比 | < 1% |
| 超时数 | status=-1 的计数 | 0 |

`cmd` 模式的延迟天然包含 PC 执行时间（Windows 上 `echo` 约 50–200ms），
合格线放宽：p50 ≤ 300ms，p95 ≤ 600ms。重点看它**不随并发线性恶化**。

---

## 2. 脚本说明

### 工作原理

- 每个 worker 线程持有一条 HTTPS keep-alive 长连接（复刻 mused daemon 的行为），
  在 `--duration` 内循环发请求。连接断了自动重建。
- 链路：TCP → egress 代理 CONNECT → TLS（SNI=`ai.vps38.local`，VPS38 Root CA 校验）
  → HTTP/1.1 keep-alive。`--mode cmd` 时每个请求带 `Authorization: Bearer` + JSON body。
- 结果先攒在各 worker 的本地 list（避免锁竞争），结束后再合并统计。

### 参数

```
--mode {status,cmd}     status=VPS 层，cmd=端到端（默认 status）
--concurrency N         并发 worker 数（默认 10）
--duration S            时长秒数（默认 60）
--target NAME           cmd 模式的目标 PC（默认 RD-928）
--token T               覆盖 $MUSE_API_TOKEN
--output PATH.csv       写 CSV 明细 + 同名 _summary.json
--baseline-ssh          走旧 SSH 链路（~/workspace/bin/musepc）做基线
```

### 输出

- 终端：汇总表（QPS、p50/p95/p99、错误率、超时数）。
- CSV 列：`wall_time, rel_time_s, latency_ms, ok, status, error`。
  `status` 为 HTTP 状态码；`-1`=超时，`-2`=连接/协议异常。
- 退出码：错误率 < 5% 返回 0，否则返回 3（方便 CI 判红）。

### 安全注意

- token 只从 `$MUSE_API_TOKEN` 或 `--token` 读，**永不写进 CSV**。
- 错误 body 只截前 120 字符进 CSV，避免把敏感回显落盘。

---

## 3. VPS 容量评估（1 vCPU）

基于前期调研的实测数据推算：

| 数据来源 | 数值 |
|----------|------|
| asyncio 单进程吞吐（调研结论） | 2k–8k req/s（纯 HTTP，无业务逻辑） |
| muse-bridge 实际处理 | 每个请求做 token 校验（hmac）+ JSON 解析 + 路由，约 0.2–0.5ms CPU |
| 100 条 WSS 长连接内存 | ~7MB（可忽略） |
| 本机原型实测（VM→VPS 一跳） | 27ms（含代理 CONNECT + TLS，keep-alive 复用后） |

**推算**：

- **`/status` 这类轻请求**：VPS 端 CPU 每请求约 0.3ms → 单核理论上限约
  **3000 req/s**。考虑 Python GIL 与 nginx 转发开销，保守估计
  **1500–2500 QPS** 可用，p99 仍 < 100ms。
- **`/cmd` 端到端**：瓶颈不在 VPS，而在 PC 执行 + WSS 往返。
  VPS 只是转发，QPS 上限与 status 模式同量级，但**有效并发受 PC 侧串行执行限制**
  （见 §4 瓶颈分析）。

**结论**：单台 1 vCPU 的 VPS 扛我们这套系统的命令转发绰绰有余。
真实的日常负载（人工操作）峰值也就几十 QPS，离天花板有两个数量级。
即使未来 10 台 PC 全开压测，VPS 也不是瓶颈。

---

## 4. 瓶颈分析：哪里会先撑不住

按"先倒下"的顺序排列：

### ① PC 端命令执行（最先）

- VS Code 插件收到命令后是**串行执行**的（一次一个 shell）。
  50 并发打同一台 PC，命令在插件侧排队，p99 会线性膨胀。
- 这是**设计如此**，不是 bug：同一台机器本来就不该并行跑 50 个 shell。
- 缓解：`cmd` 压测只做 C-10/C-50 验证"不雪崩"即可；生产环境按 PC 分片，
  不要把高并发压到单台 PC。

### ② VM 端 egress 代理

- 所有 HTTPS 都要经过代理做 CONNECT。代理的并发连接数上限未知，
  S-200 如果出现大量 `-2` 连接错误而 VPS 侧 CPU 还很空，凶手就是代理。
- 诊断：对比直连（bypass 代理）的 S-100 数字；或看错误是否集中在测试前 5 秒
  （连接建立风暴）。

### ③ VPS 端 Python GIL（最后）

- 只有当 QPS > 1500 且全是轻请求时才摸得到。我们的场景到不了。
- 真到了：nginx 后面起 2 个 muse-bridge 进程（`upstream` 轮询）即可，
  无需改代码（无共享状态，controllers.json 只读）。

### 不用担心的

- **WSS 连接数**：100 连接才 7MB，即使 1000 台 PC 也没压力。
- **内存**：muse-bridge 常驻 < 100MB。
- **带宽**：命令都是 KB 级，VPS 上行 31Mbps 够几千 QPS。

---

## 5. 结果记录表

跑完把数字填这里（目标值供参考）：

| 场景 | QPS | p50 | p95 | p99 | 错误率 | 备注 |
|------|-----|-----|-----|-----|--------|------|
| S-10 | ≥300 | ≤40ms | ≤80ms | ≤150ms | <1% | |
| S-50 | ≥1200 | ≤45ms | ≤90ms | ≤200ms | <1% | |
| S-100 | ≥1800 | ≤60ms | ≤120ms | ≤300ms | <2% | |
| S-200 | — | — | — | — | — | 找拐点，记录现象 |
| C-10 | ≥25 | ≤300ms | ≤600ms | ≤1s | <1% | 含 PC 执行 |
| C-50 | — | — | — | — | — | 观察 PC 排队 |
| B-10 (SSH) | ~30 | ~320ms | ~500ms | ~800ms | <1% | 基线：单次 ~300ms |

**判定**：S-10 的 p50 ≤ 40ms 且错误率 < 1% 即证明"去 SSH"达到设计目标
（SSH 基线 p50 ~320ms，提升约 8–10 倍，与原型 27ms vs 300ms 一致）。

---

## 6. 故障排查

| 现象 | 可能原因 | 查什么 |
|------|----------|--------|
| 全部 `-2`，`CONNECT rejected` | egress 代理没透 | `echo $https_proxy`，对照 client.py 的 recipe |
| 全部 401 | token 错 / 没部署 patch | 先跑 `curl` 手动验证 4a 场景（见 RUNBOOK） |
| cmd 模式全部超时 | PC 不在线 | `/muse-api/status` 看 `client_list` |
| S-200 后期 p99 飙升 | 代理连接数上限 | 降到 S-100 看是否恢复 |
| CSV 里 `error` 含 traceback | 脚本 bug | 贴 traceback 回来修 |
