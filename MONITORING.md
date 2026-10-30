# 监控告警方案 — 去 SSH 架构

轻量、可落地：VM + VPS 两台机器，Python 标准库，cron 每分钟跑，无 Prometheus。

## 一、监控指标

### 1.1 VM daemon（mused）

| 指标 | 来源 | 说明 |
|------|------|------|
| `process_alive` | pidfile / pgrep | 进程是否存在 |
| `socket_ok` | unix socket `{"op":"health"}` | 2s 内有响应 |
| `memory_rss_mb` | `/proc/<pid>/status` | 常驻内存，正常约 20MB |
| `fd_count` | `/proc/<pid>/fd` | 文件描述符数 |
| `uptime_s` | health 响应 | 进程启动至今秒数（判断是否频繁重启） |
| `req_total` | stats 响应 | 累计请求数 |
| `req_qps` | stats 差分 | 每分钟请求速率 |
| `lat_p50_ms` / `lat_p95_ms` / `lat_p99_ms` | stats 响应 | 上游请求延迟分布（滚动窗口） |
| `err_4xx` / `err_5xx` / `err_transport` | stats 响应 | 状态码分类计数 |
| `err_rate` | 计算 | (5xx + transport) / total，5 分钟窗口 |
| `reconnect_count` | stats 响应 | 上游重连次数（突增 = 网络/VPS 抖动） |
| `last_heartbeat_ok` | stats 响应 | 上次心跳成功时间戳 |

### 1.2 VPS（muse-bridge.py + nginx）

| 指标 | 来源 | 说明 |
|------|------|------|
| `process_alive` | pgrep muse-bridge.py | 桥进程是否存在 |
| `memory_rss_mb` | `/proc/<pid>/status` | 正常 < 200MB |
| `cpu_pct` | `/proc/<pid>/stat` 差分 | 1 分钟平均 CPU |
| `api_qps` | `/muse-api/health` 或 access log | 命令接口请求速率 |
| `api_lat_p99_ms` | health 响应或 log | 接口延迟 |
| `http_401` / `http_403` | access log / health | 鉴权失败计数（401 突增 = 攻击或 token 泄露） |
| `wss_clients` | `/muse-api/health` | 在线 PC 数 |
| `wss_client_list` | `/muse-api/health` | 在线 PC 名单 + 最后心跳 |
| `pending_futures` | `/muse-api/health` | 未完成的 PC 请求数（堆积 = PC 卡死） |

### 1.3 PC 端（经 VPS 间接监控）

| 指标 | 来源 | 说明 |
|------|------|------|
| `pc_online` | wss_client_list | PC 是否在连接名单中 |
| `pc_last_seen` | wss_client_list | 最后心跳时间 |
| `pc_offline_min` | 计算 | 离线时长 |

## 二、健康检查端点设计

### 2.1 VM daemon：unix socket 本地 ops

在现有帧协议上加两个**本地处理**的 op（不转发上游）：

**请求**
```json
{"op": "health"}
{"op": "stats"}
```

**`health` 响应**
```json
{
  "status": "ok",
  "uptime_s": 86400,
  "version": "1.0.0",
  "upstream_ok": true,
  "last_heartbeat_ok": 1730000000
}
```

**`stats` 响应**
```json
{
  "req_total": 15230,
  "req_2xx": 15000,
  "req_4xx": 180,
  "req_5xx": 12,
  "req_transport_err": 38,
  "lat_p50_ms": 28,
  "lat_p95_ms": 95,
  "lat_p99_ms": 410,
  "reconnect_count": 3,
  "window_s": 300
}
```

- 延迟用滚动窗口（如最近 5 分钟或最近 1000 次）的近似分位数，daemon 内存里维护即可，无需精确。
- 实现位置：`handle_client()` 开头先判断 `req.get("op")`，命中则本地返回，不走 `upstream.request()`。
- 详见 `monitoring/health_endpoint_spec.md`。

### 2.2 VPS：`GET /muse-api/health`（免鉴权）

**设计原则**：给监控/cron 用，不鉴权，但**只暴露非敏感信息**（不含 token、不含命令内容）。

```json
{
  "status": "ok",
  "version": "1.1.0",
  "uptime_s": 86400,
  "wss_clients": 2,
  "client_list": [
    {"name": "RD-928", "last_seen": 1730000000, "ip": "1.2.3.4"},
    {"name": "homepc", "last_seen": 1730000005, "ip": "5.6.7.8"}
  ],
  "pending_futures": 0,
  "api": {
    "req_total": 50000,
    "http_401": 12,
    "http_403": 3,
    "lat_p99_ms": 120
  }
}
```

- nginx 层面限流：`limit_req` 限制该路径每秒最多 10 次，防滥用。
- 实现：在 `handle_connection()` 的鉴权块**之前**加 `if path == "/muse-api/health"` 分支。
- 现有的 `/status`（需鉴权）保留给人工排查，信息更全。

## 三、告警规则

### 3.1 规则表

| ID | 条件 | 级别 | 说明 |
|----|------|------|------|
| `daemon_down` | 连续 2 次检查进程不存在或 socket 无响应 | CRITICAL | daemon 挂了，所有命令走不了 |
| `daemon_mem_high` | RSS > 100MB | WARNING | 正常约 20MB，泄漏嫌疑 |
| `daemon_fd_high` | FD > 200 | WARNING | 连接泄漏嫌疑 |
| `daemon_restart_loop` | uptime_s < 120 且连续 3 次检查都如此 | WARNING | 频繁重启，systemd 在拉起 |
| `daemon_err_rate` | 5 分钟 err_rate > 5% | WARNING | 上游或 VPS 异常 |
| `daemon_p99_high` | 5 分钟 P99 > 2000ms | WARNING | 链路明显变慢 |
| `daemon_reconnect_storm` | 5 分钟 reconnect > 10 | WARNING | 网络抖动或 VPS 重启 |
| `vps_bridge_down` | muse-bridge.py 进程不存在 | CRITICAL | VPS 桥挂了，全线瘫痪 |
| `vps_401_spike` | 1 分钟 401 > 20 | WARNING（安全） | 可能有人在扫 token |
| `vps_403_spike` | 1 分钟 403 > 20 | WARNING | 越权尝试或配置错误 |
| `vps_no_pcs` | wss_clients == 0 持续 2 分钟 | CRITICAL | 所有 PC 离线 |
| `vps_pending_buildup` | pending_futures > 50 | WARNING | PC 端堆积，结果回不来 |
| `pc_offline` | 某 PC 离线 > 5 分钟 | WARNING | 插件掉了或 PC 关机 |
| `vps_mem_high` | bridge RSS > 500MB | WARNING | 内存泄漏嫌疑 |

### 3.2 告警方式

1. **必做**：追加 JSON 到本地 `alerts.log`（`~/.local/share/mused/alerts.log` / VPS `/var/log/muse-bridge/alerts.log`）。
2. **可选**：`alert_hook.sh` —— 用户自备的可执行脚本，收到告警 JSON（stdin），想干嘛干嘛：
   - `curl` 发到 webhook（企业微信/钉钉/Telegram）
   - `logger` 进 syslog
   - 经 agent-mail 发给超管 Muse
3. **防抖**：每个告警 ID 有状态（firing/resolved），只在状态变化时触发一次，不每分钟轰炸。状态存 `~/.local/share/mused/monitor_state.json`。

### 3.3 告警 JSON 格式

```json
{
  "ts": 1730000000,
  "id": "daemon_down",
  "severity": "CRITICAL",
  "host": "vm",
  "message": "mused process not found (2 consecutive checks)",
  "details": {"pidfile": "/home/hatch/.local/share/mused/mused.pid"},
  "state": "firing"
}
```

## 四、日志规范

### 4.1 格式

单行文本，grep 友好：

```
2026-10-19T11:00:01+08:00 INFO mused: request ok method=POST path=/muse-api/cmd target=RD-928 status=200 latency_ms=31 req_id=a1b2c3
2026-10-19T11:00:02+08:00 WARNING mused: upstream reconnect attempt=3 reason=connection_reset
2026-10-19T11:00:03+08:00 ERROR mused: heartbeat failed error=timeout after_ms=5000
```

Python logging 格式串：
```
%(asctime)s %(levelname)s %(name)s: %(message)s
```
`asctime` 用 ISO8601（含时区）。

请求日志**关键字段**（kv 形式，便于 `grep` + `awk`）：
`req_id` `method` `path` `target` `status` `latency_ms`

**绝不记录**：token 明文、请求 body（可能含敏感命令输出）、PC 的完整 IP（health 端点可给网段脱敏）。

### 4.2 级别

| 级别 | 用途 |
|------|------|
| DEBUG | 帧收发、连接池细节，默认关闭 |
| INFO | 请求完成、心跳成功、启动/停止 |
| WARNING | 重连、心跳失败、延迟突增、可恢复的错误 |
| ERROR | 上游不可用、socket 异常、配置错误 |

### 4.3 保留期

- VM：`RotatingFileHandler`，10MB × 5（已在 daemon 实现）。
- VPS：同规格，`/var/log/muse-bridge/bridge.log`，logrotate 兜底。
- alerts.log：100MB 上限，超了轮转，保留 30 天。

## 五、故障排查手册

### `daemon_down`（CRITICAL）
1. `systemctl --user status mused` 看状态
2. `journalctl --user -u mused -n 50` 看最后日志
3. `tail -50 ~/.local/share/mused/mused.log` 看 daemon 日志
4. 常见因：OOM（dmesg 查）、配置错误（config.json 语法）、token 文件权限
5. 手动拉起：`systemctl --user restart mused`，观察 2 分钟

### `daemon_err_rate` / `daemon_p99_high`（WARNING）
1. 先看是 4xx 还是 5xx/transport：4xx 多 = token 或 target 参数问题；5xx/transport 多 = VPS 或网络问题
2. `curl https://38.207.167.51/muse-api/health` 看 VPS 是否正常
3. VPS 正常但延迟高：查 VPS 负载、nginx 错误日志

### `vps_bridge_down`（CRITICAL）
1. SSH 上 VPS（最后一次用 SSH 的场景之一）：`systemctl status muse-bridge`
2. `journalctl -u muse-bridge -n 100`
3. 检查是否部署 patch 后起不来：`python3 -m py_compile /usr/local/bin/muse-bridge.py`
4. 回滚见 `RUNBOOK.md`

### `vps_401_spike`（安全 WARNING）
1. 查 nginx access log：`grep " 401 " /var/log/nginx/access.log | tail -20`
2. 看来源 IP 是否集中：集中 = 攻击，nginx 临时封禁
3. 分散 = 可能 token 泄露，轮换 token（`gen-tokens.sh --add` + 更新分发）

### `vps_no_pcs` / `pc_offline`（CRITICAL/WARNING）
1. `/muse-api/health` 确认名单
2. PC 侧：VS Code 插件是否运行、网络是否通、Clash 规则是否还在（homepc）
3. VPS 侧：`ss -tnp | grep :443` 看连接数

### `daemon_reconnect_storm`
1. VM 到 VPS 网络：`curl -o /dev/null -w "%{time_total}" https://38.207.167.51/muse-api/health`
2. VPS 是否在重启：`uptime`、`journalctl -u muse-bridge`

## 六、实现

`monitoring/` 目录：

| 文件 | 说明 |
|------|------|
| `alert.py` | 告警库：状态机（防抖）、写 alerts.log、调 hook |
| `vm_monitor.py` | VM 端采集 + 阈值判定，cron 每分钟 |
| `vps_monitor.py` | VPS 端采集 + 阈值判定，cron 每分钟 |
| `v2_metrics.py` | **v2 组件指标采集**：连接池 / FastAuth / 微批 / 压缩 / 去重 / WSS 调度；`--side vm|vps|local`，`--check-alerts` 评估 v2 告警规则 |
| `alert_hook.sh.example` | hook 示例（webhook + logger） |
| `health_endpoint_spec.md` | daemon `health`/`stats` op 和 VPS `/muse-api/health` 的实现规格 |

## 七、v2 组件指标（Gemini-4 评审新增）

v2 组件（连接池、FastAuthEngine、微批聚合、压缩、去重状态机、WSS 双优先级队列）
各自暴露 `stats()`，由 `v2_metrics.py` 统一采集。采集口径：

- **VM 侧**：daemon 在 unix socket 上新增本地 op `{"op": "v2stats"}`，
  返回 `{"pool": {...}, "microbatch": {...}, "compress": {...}}`（不转发上游）。
- **VPS 侧**：`GET /muse-api/health` 增加免鉴权的 `"v2"` 块，
  `{"fastauth": {...}, "dedup": {...}, "wss": {"PC-A": {...}, ...}}`。
- 组件未实现某计数器时该指标为 `None`，采集脚本跳过不告警。

### 7.1 指标表

#### 连接池（`pool_*`，VM）

| 指标 | 来源 | 说明 |
|------|------|------|
| `pool_total` | `pool.stats()["total"]` | 当前持有连接数 |
| `pool_idle` | `pool.stats()["idle"]` | 空闲可复用数 |
| `pool_in_use` | 派生 total - idle | 正在承载请求的连接数 |
| `pool_discard_stale` | 计数器（待加） | 累计 45s 空闲丢弃数（突增 = 流量低谷或 Nginx 超时配置漂移） |
| `pool_discard_broken` | 计数器（待加） | 累计坏连接丢弃数（突增 = 网络抖动） |
| `pool_wait_queue_len` | 计数器（待加） | 当前阻塞在 `acquire()` 的线程数（>0 = 池已满） |
| `pool_acquire_wait_ms_p95` | 计数器（待加） | 池满时等待 P95（>1000ms = 容量不足） |

#### FastAuthEngine（`fastauth_*`，VPS）

| 指标 | 来源 | 说明 |
|------|------|------|
| `fastauth_controllers` | `engine.stats()` | 启用中的控制器条目数 |
| `fastauth_cache_size` | `engine.stats()["decision_cache_size"]` | (token_tag, ip, target) 决策缓存条目数 |
| `fastauth_fast_hits` | 计数器（待加） | 快路径（缓存命中）累计 |
| `fastauth_slow_path` | 计数器（待加） | 慢路径（完整校验）累计 |
| `fastauth_hit_rate` | 派生 | 快路径命中率，健康时应 >95% |
| `fastauth_slow_us_p50` | 计数器（待加） | 慢路径 P50（微秒级，~50μs） |
| `fastauth_reloads` | 计数器（待加） | controllers.json 热重载累计（频繁重载 = 运维脚本在抖动写文件） |
| `fastauth_fail_closed` | `engine.stats()` | 当前是否 fail-closed（1 = 拒绝一切，立即排查） |

#### 微批聚合（`mb_*`，VM）

| 指标 | 来源 | 说明 |
|------|------|------|
| `mb_batches` | 计数器 | 累计发出的 /batch 数 |
| `mb_merged_requests` | 计数器 | 累计被合并的子请求数 |
| `mb_single_fast` | 计数器 | in_flight==0 时 0ms 立发的请求数 |
| `mb_window_triggered` | 计数器 | 1.5ms 收集窗口打开累计 |
| `mb_window_rate` | 派生 | 窗口触发率（并行扇出多则高，串行工作流接近 0 正常） |
| `mb_avg_batch_size` | 派生 | 平均合并 batch 大小 |
| `mb_saved_roundtrips` | 派生 merged - batches | 节省的 VPS→PC 往返数（核心收益指标） |
| `mb_truncated_64k` | 计数器 | 因 64KB 上限被截断发车的累计 |

#### 压缩（`compress_*`，VM）

| 指标 | 来源 | 说明 |
|------|------|------|
| `compress_attempts` | 计数器 | should_compress() 评估累计 |
| `compress_applied` | 计数器 | 实际压缩累计 |
| `compress_skipped_small` | 计数器 | 跳过：<2KB |
| `compress_skipped_blacklist` | 计数器 | 跳过：二进制/已压缩后缀 |
| `compress_skipped_high_entropy` | 计数器 | 跳过：1KB 探针节省 <15% |
| `compress_skip_rate` | 派生 | 跳过率（文本多则低，二进制多则高） |
| `compress_ratio_p50` | 计数器 | P50 压缩比（压缩后/原大小，文本约 0.15-0.22） |
| `compress_us_p50` | 计数器 | P50 压缩耗时（zlib level=1 约 280μs/64KB） |
| `compress_bytes_saved` | 计数器 | 累计节省字节数（带宽收益） |

#### 去重状态机（`dedup_*`，VPS）

| 指标 | 来源 | 说明 |
|------|------|------|
| `dedup_in_flight` | `dedup.stats()` | 正在 PC 上执行的请求数 |
| `dedup_settled` | `dedup.stats()` | COMPLETED/UNCERTAIN 缓存数 |
| `dedup_total_bytes` | `dedup.stats()` | 缓存响应体总字节（上限 64MB） |
| `dedup_hits` | 计数器（待加） | request_id 命中缓存累计 |
| `dedup_attaches` | 计数器（待加） | 重试挂载到 IN_FLIGHT 累计 |
| `dedup_uncertain` | 计数器（待加） | UNCERTAIN 累计（WSS 中途断开，突增 = 网络不稳） |
| `dedup_hit_rate` | 派生 | 去重命中率（骤降 = request_id 生成坏了） |
| `dedup_evicted_lru` | 计数器（待加） | LRU 淘汰累计 |

#### WSS 调度（`wss_*`，VPS，按 PC）

| 指标 | 来源 | 说明 |
|------|------|------|
| `wss_high_queued[PC]` | `scheduler.stats(pc)` | /cmd+/batch 排队数（>50 = 控制面拥塞 CRITICAL） |
| `wss_bulk_queued[PC]` | `scheduler.stats(pc)` | /file 分片排队数（>20 = 传输积压 WARNING） |
| `wss_bulk_in_flight[PC]` | `scheduler.stats(pc)` | 在途 /file 分片数（正常 0/1） |
| `wss_sent_high[PC]` | 计数器 | 累计发出的高优先级消息 |
| `wss_sent_bulk[PC]` | 计数器 | 累计发出的 bulk 分片 |

### 7.2 v2 告警规则

| ID | 条件 | 级别 | 说明 |
|----|------|------|------|
| `fastauth_hit_rate_low` | hit_rate < 90% | WARNING | 决策缓存颠簸：controllers.json 被频繁重写或 key 设计使缓存失效 |
| `pool_exhausted` | in_use >= max_size | WARNING | 连接池满，新请求在排队 |
| `pool_waiters` | wait_queue_len > 0 | WARNING | 有线程阻塞等连接，容量不足 |
| `dedup_hit_rate_drop` | hit_rate < 50% | WARNING | request_id 生成可能坏了，重试全被当新请求 |
| `dedup_memory_high` | total_bytes > 48MB | WARNING | 接近 64MB 硬上限，检查大响应缓存 |
| `dedup_uncertain_spike` | uncertain 5 分钟增量 > 10 | WARNING | WSS 中途断开频繁，网络不稳 |
| `wss_bulk_starved` | bulk_queued > 20 | WARNING | /file 传输被控制流量饿死 |
| `wss_high_backlog` | high_queued > 50 | CRITICAL | 控制面拥塞，/cmd 发不出去 |
| `mb_no_coalescing` | window_rate < 1%（持续 1h） | INFO | 微批几乎不触发：检查 X-Muse-Batchable 是否设置 |
| `compress_all_skipped` | skip_rate = 100%（持续 1h） | INFO | 压缩全跳过：检查阈值/黑名单配置 |

v2 告警同样走 `alert.py` 的防抖状态机，`v2_metrics.py --check-alerts` 输出可接入
`vm_monitor.py` / `vps_monitor.py` 的 alerter（`Alerter.check(...)`）。

### 7.3 采集命令

```bash
# VM 侧（daemon 需实现 {"op": "v2stats"}）
python3 monitoring/v2_metrics.py --side vm --check-alerts
# VPS 侧（需 /muse-api/health 带 v2 块）
python3 monitoring/v2_metrics.py --side vps --check-alerts
# 离线检查：各模块是否存在、缺哪些计数器
python3 monitoring/v2_metrics.py --side local
# 原始 JSON（给外部系统拉取）
python3 monitoring/v2_metrics.py --side vm --json
```

### 7.4 组件待补计数器清单

以下计数器在组件实现 `stats()` 时**必须**补上，否则对应指标为 None：

- `pool.py`: `discard_stale`, `discard_broken`, `wait_queue_len`, `acquire_wait_ms_p95`
- `fast_auth.py`: `fast_hits`, `slow_path`, `slow_us_p50`, `reloads`, `decision_cache_max`
- `microbatch.py`（待交付）: `batches`, `merged_requests`, `single_fast`, `window_triggered`,
  `batch_size_p50/p95`, `truncated_64k`
- `compress.py`: 模块级计数器 `attempts`, `applied`, `skipped_small/blacklist/high_entropy`,
  `ratio_p50`, `compress_us_p50`, `bytes_saved`
- `dedup.py`: `hits`, `attaches`, `uncertain`, `evicted_lru`, `dedup_new`
- daemon `mused.py`: `{"op": "v2stats"}` 本地 op，聚合 pool/microbatch/compress
- VPS `muse-bridge.py`: `/muse-api/health` 的 `"v2"` 块，聚合 fastauth/dedup/wss

### 部署

**VM**（cron）：
```bash
mkdir -p ~/.local/share/mused
# 每分钟跑，输出进 cron 日志，告警走 alerts.log + hook
* * * * * /usr/bin/python3 /home/hatch/workspace/muse-vps-deploy/monitoring/vm_monitor.py >> ~/.local/share/mused/monitor.log 2>&1
```

**VPS**（cron，root）：
```bash
* * * * * /usr/bin/python3 /opt/muse-bridge/monitoring/vps_monitor.py >> /var/log/muse-bridge/monitor.log 2>&1
```

两个脚本互相独立，VM 挂了不影响 VPS 监控，反之亦然。

### 设计取舍

- 不用 Prometheus：两台机器，指标不到 30 个，拉个 TSDB 是杀鸡用牛刀。
- 状态文件做差分：QPS、err_rate 需要前后两次的计数器差，state.json 存上次值。
- 延迟分位数：daemon 内部维护滚动窗口，monitor 只读结果，不自己算。
- 健康端点免鉴权但信息最小化：监控需要高频调，不能每次都走 token，也不能泄露敏感信息。
