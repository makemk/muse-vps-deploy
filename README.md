# muse-vps-deploy — 去 SSH 超高性能命令转发（v2）

VM → VPS 的 SSH 一跳替换为 HTTPS 直连的全部物料：VPS 鉴权补丁、VM 常驻 daemon、token 管理、Nginx 配置、部署 runbook。

**目标**：单次命令往返从约 730ms 降到约 67ms。延迟拆解（Gemini 4 修正）：690ms = SSH 握手 300ms + VPS 远端 Shell 初始化/musepc 冷启动 390ms；替换为常驻 daemon 后 VM↔VPS 段降至约 27ms（1×RTT + Nginx 转发）。

## 架构拓扑

```
┌─────────┐  Unix Socket   ┌──────────────┐  HTTPS/1.1 keep-alive  ┌───────┐  proxy_pass   ┌────────────┐  WSS长连接  ┌─────┐
│ AI助手   │ ──────────── │ mused daemon │ ────────────────────── │ Nginx │ ─────────── │ 8471命令服务 │ ───────── │ PC  │
│ (VM)    │  JSON帧      │ 连接池4-16条  │  Bearer Token          │ :443  │  keepalive32 │ Token鉴权   │  双优先级  │插件 │
└─────────┘              │ 微批1.5ms    │                        └───────┘              │ 去重状态机 │  队列     └─────┘
                         │ 压缩zlib/L1  │                                              └────────────┘
                         └──────────────┘
                              │
                    ┌─────────┴─────────┐
                    │ controllers.json  │  (VPS, 600权限, 只存SHA-256)
                    │ {muse,pcs[],     │
                    │  allowed_ips[]}   │
                    └───────────────────┘
```

- **VM 侧**：`mused` 常驻进程，对内 Unix Socket，对外 HTTPS 长连接池。零第三方依赖（纯标准库）。
- **VPS 侧**：Nginx（TLS终结 + upstream keepalive）→ 8471 Python 命令服务（鉴权 + 去重 + WSS 调度）。
- **PC 侧**：VS Code 插件，WSS 长连接，**本次改造零改动**（见 `PC_COMPATIBILITY.md`）。

## v2 组件清单

| 组件 | 用途 | 文件 | 测试 |
|------|------|------|------|
| FastAuthEngine | Token 鉴权快路径：blake2b 复合决策缓存，<0.8μs；慢路径 SHA-256+CIDR；mtime 热加载原子切换 | `vps-auth-patch/fast_auth.py` | `test_fastauth.py` |
| DedupManager | request_id 去重：IN_FLIGHT/COMPLETED/UNCERTAIN 三态；300s TTL（从完成起算）；5000条/64MB上限；IN_FLIGHT永不淘汰 | `vps-auth-patch/dedup.py` | 22/22 通过 |
| WSS Scheduler | 每PC双优先级队列：high（/cmd//batch）+ bulk（/file）；同PC最多1个/file分片在途；/cmd零等待插队 | `vps-auth-patch/wss_scheduler.py` | 14/14 通过 |
| ConnectionPool | HTTPS连接池：min 4 / max 16；启动预热4条；空闲超45s丢弃重建（防Nginx 65s FIN竞态） | `mused/pool.py` | 9/9 通过 + 真机冒烟 |
| TLS Resume | TLS 1.3 session复用：getresponse()后提取session（ticket是握手后异步下发的）；**严禁0-RTT** | `mused/tls_resume.py` | `test_tls_resume.py` |
| MicroBatch | 在途水位触发微批：in_flight=0时0ms立发；>0时开1.5ms窗口，上限10条；64KB截断；子请求错误隔离 | `mused/microbatch.py` | `test_microbatch.py` |
| SmartCompressor | 应用层端到端压缩：<2KB不压；19后缀黑名单+魔数；>64KB用1KB探针（省<15%跳过）；zlib level=1；VPS只透传 | `mused/compress.py` | 25/25 通过 |
| Token Manager | 生成/验证/轮换/吊销；`--allowed-ips` CIDR白名单；轮换继承旧IP限制；原子写入os.replace | `token-manager/` | 45/45 通过 |

配套：`nginx/muse-api.conf`（upstream keepalive + TLS session配置）、`pc-shell-pool/`（Phase 2：PC常驻Shell池方案 + 压缩解压规范）、`monitoring/`（v2指标采集）、`benchmarks/`（性能基准）、`openapi.yaml`（API规范）。

## Gemini 4 评审 → 落地对照

| 评审结论 | 落地文件 |
|----------|----------|
| Python标准库无HTTP/2 → 改4-16条HTTP/1.1连接池 | `mused/pool.py` |
| Nginx到8471默认短连接 → upstream keepalive 32 | `nginx/muse-api.conf` |
| 单WSS队头阻塞 → 64KB分块+双优先级队列+在途限流 | `vps-auth-patch/wss_scheduler.py` |
| 部署顺序倒置 → Step0固化→Step1先Token→Step2补丁 | `RUNBOOK_V2.md` |
| 严禁127.0.0.1白名单 → ssh_fallback_admin专用token | `RUNBOOK_V2.md` Step 1 |
| at-most-once + request_id两阶段状态机 | `vps-auth-patch/dedup.py` |
| /muse-api/与/mail-api/不合并（故障隔离） | 架构决策（独立进程） |
| controllers.json原子替换 + 200ms节流mtime检查 | `vps-auth-patch/fast_auth.py` |
| allowed_ips CIDR白名单 | `token-manager/` + `fast_auth.py` |
| TLS session复用（getresponse后提取） | `mused/tls_resume.py` |
| 微批用在途水位触发（非固定5ms窗） | `mused/microbatch.py` |
| 应用层压缩（VPS透传）+ zlib level=1 | `mused/compress.py` |
| Token决策缓存<0.8μs（blake2b+进程盐） | `vps-auth-patch/fast_auth.py` |
| PC常驻Shell池（Phase 2，原生管道非pty） | `pc-shell-pool/DESIGN.md` |

## 部署顺序（RUNBOOK_V2.md）

- **Step 0**：资产固化确认（/tmp→workspace已完成）+ SSH回退兼容确认
- **Step 1**：Token生成与分发（super / muse-rd928 / muse-homepc / ssh_fallback_admin；8471未打补丁，零影响）
- **Step 2**：VPS鉴权补丁上线（备份→dry-run→部署→重启→7个冒烟场景→SSH回退验证）
- **Step 3**：Daemon旁路上线（systemd，生产流量仍在SSH）
- **Step 4**：切换流量（双跑24h→切流→回归→7天观察）→ 观察期满关闭SSH

回滚点：Step 2（重启失败/冒烟失败/PC 10s未恢复/SSH失败率>0→还原备份）；Step 4（回归失败/非预期4xx5xx/错误率>1%/P95>150ms→切回SSH）。

## 性能数据

| 指标 | 实测/设计值 |
|------|-------------|
| VM→VPS keep-alive稳态 | 27-32ms（vs SSH 300ms，约9-11倍） |
| FastAuthEngine快路径 | <0.8μs（blake2b决策缓存命中） |
| 压缩（zlib level=1, 64KB文本） | 0.28ms，体积→15-22% |
| 连接池 | 16条支撑约240 QPS（67ms往返） |
| VPS单进程 | 2500-4000 QPS（uvloop+orjson+C扩展，<3000 QPS无需扩容） |
| 去重缓存 | 5000条/64MB上限，IN_FLIGHT永不淘汰 |

基准套件：`benchmarks/run_all.sh`（bench_fastauth/pool/compress/dedup）。

## 安全状态（SECURITY_V2.md）

审计发现 7 个问题：**1 高 / 2 中 / 3 低 / 1 信息**。

- **高（未修复，部署前必须修）**：C1 解压炸弹 — `decompress()`无输出上限，42KB可炸出5GB→OOM。修复：分块解压+64MB上限。
- **中（部署窗口期修）**：D1 IN_FLIGHT无上限（加10000上限）；S1 WSS队列无深度上限（加10000上限）。
- 低/信息：S2 /file饿死（可选加权公平）、D2 wait_for硬上限600s、M1 batch子请求权限校验、P1连接池双重归还。

已确认安全：FastAuthEngine（真随机盐、零明文内存、防投毒）、TLS（0-RTT从未启用）、微批（无跨权限合并）。

## v2 集成状态（integration_v2.py）

`python3 integration_v2.py` —— **50/50 全绿**。用纯内存 mock 把 v2 全链路拼起来跑通：

```
MockVM --MicroBatcher--> dispatch --compress--> pool.get_connection()
    --> MockVPS.handle() --FastAuthEngine--> --DedupManager-->
    --WSSScheduler--> MockPC.execute()
```

| 测试 | 覆盖 |
|------|------|
| T1 auth | 200/决策缓存命中/401/403-target/403-super无target/403-IP/热加载 |
| T2 dedup | 重复request_id单执行/COMPLETED回放/IN_FLIGHT不重发/wait_for唤醒/UNCERTAIN/超大body不缓存 |
| T3 scheduler | 高优先于bulk/bulk在途=1限流/response释放/队列排空 |
| T4 compress | <2KB跳过/黑名单/魔数/探针/round-trip |
| T5 microbatch | 空闲0ms立发/并发凑批/非batchable独行 |
| T6 pool | 预热/扩容/超限Timeout/坏连接丢弃/45s过期重建/close |
| T7 tls_resume | 缓存get/set/session注入/session提取 |
| T8 e2e | 鉴权/去重/凑批全链路 |

**⚠️ 集成发现的 P0 问题**（详见 `INTEGRATION_ISSUES.md`，接线前必须修）：

1. **WSS scheduler 命名空间**：`enqueue()` 只认裸名 `"cmd"/"batch"/"file"`，
   传 `"/file"` 会被 fail-safe 判为未知→进 high_prio，**bulk 限流被静默绕过**。
   修复：在 `enqueue()` 入口归一化（`lstrip("/")`）。
2. **FastAuthEngine 丢 `grace_until`**：轮换宽限期内旧 token 在快路径被误判 401，
   与慢路径行为分裂。修复：预编译时加入宽限期判断。
3. **多路复用 `/batch` 是新协议**：VPS 需新增 `handle_batch_multiplex()`，
   dedup 需按 `(batch_id, sub_id)` 粒度；与旧 shell-batch 不是同一接口。

## 目录速查

```
muse-vps-deploy/
├── README.md              # 本文件（v2）
├── RUNBOOK_V2.md          # 部署步骤（Step 0-4，Gemini修正顺序）
├── RUNBOOK.md             # v1（已废弃，保留对照）
├── SECURITY_V2.md         # v2安全审计（7问题）
├── INTEGRATION_ISSUES.md  # v2集成问题清单（3×P0）
├── integration_v2.py      # v2端到端mock集成测试（50/50）
├── INTEGRATION_TEST.md     # 19个集成用例
├── CHAOS_TEST.md          # 8个混沌场景
├── LOAD_TEST.md + loadtest.py
├── DISASTER_RECOVERY.md + backup.sh/restore.sh
├── MONITORING.md + monitoring/   # v1+v2指标
├── PC_COMPATIBILITY.md    # PC零改动结论
├── openapi.yaml           # API规范
├── vps-auth-patch/        # fast_auth.py, dedup.py, wss_scheduler.py, auth_patch.py
├── mused/                 # mused.py, pool.py, tls_resume.py, microbatch.py, compress.py + systemd
├── museapi-client/        # museapi.py(452行生产库), cli.py, benchmark.py
├── token-manager/         # gen/verify/rotate/revoke + allowed_ips
├── nginx/                 # muse-api.conf + test-nginx.sh
├── pc-shell-pool/         # Phase 2方案（常驻Shell + 解压规范）
├── benchmarks/            # 性能基准套件
├── scripts/               # deploy-auth-patch.sh, gen-tokens.sh（v1脚本）
└── tests/                 # test_daemon.py, test_pool.py
```
