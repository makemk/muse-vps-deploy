# mused — VM 端常驻守护进程

VM 侧的 HTTPS 命令转发 daemon：替代 SSH，成为 `musepc` 的新传输层。

## 用途

- 常驻进程，持有到 VPS `/muse-api/` 的 HTTPS 长连接（keep-alive）
- 本地 unix socket 接收命令，转发到 VPS，返回结果
- 纯 Python 标准库，约 20MB 内存，无第三方依赖

## 文件清单

| 文件 | 说明 |
|------|------|
| `mused.py` | daemon 主体：配置加载、HTTPS 连接池、unix socket 服务、SIGTERM 优雅退出 |
| `pool.py` | **ConnectionPool**：线程安全 HTTP/1.1 keep-alive 连接池（纯标准库）。min 4 预热 / max 16；空闲超 45s 丢弃（防 nginx 65s 超时竞态）；满池等待 5s 超时 |
| `tls_resume.py` | **TLS 会话复用**（Gemini-4 第二轮 §3.3）：`ResumableHTTPSConnection` + 线程安全 `TLSSessionCache`。重建连接时注入缓存 session（1-RTT），新 session 在**第一次 HTTP 响应后**提取（TLS 1.3 ticket 握手后异步下发，connect() 内绝不读取）。0-RTT 永不开启 |
| `test_tls_resume.py` | mock 单元测试 14 项（注入时机/提取时机/线程安全/握手失败不泄漏 socket） |
| `museapi` | CLI 客户端：从文件读 token，`--target` 快捷指定目标 PC |
| `config.json.example` | 配置示例（见下方） |

## 连接池（Gemini-4 第二轮 §1.2）

`Upstream` 不再持有单条长连接，改为 `ConnectionPool`：

- **预热**：启动时后台线程异步建立 4 条连接（CONNECT+TLS+GET /status 验证），首条业务请求零握手等待
- **扩容**：空闲为 0 且总数 <16 时按需新建；到 16 上限后调用方排队等 5s，超时抛 `TimeoutError`
- **防竞态**：归还时记 `last_used_at`；取用时若 `now-last_used > 45s` 直接丢弃重建（nginx `keepalive_timeout=65s`，不断临近超时的连接）
- **断线**：`request()` 抛 `HTTPException/OSError/SSLError` 或响应 `will_close` 时，该连接标记 broken 丢弃
- **观测**：`Upstream.stats()` → `{idle, total, min_size, max_size}`

## TLS 会话复用（Gemini-4 第二轮 §3.3）

`Upstream` 持有全局 `TLSSessionCache`（线程安全，只存最新一条 session）：

- **注入**：每次重建连接（预热/扩容/45s 丢弃后重建）时，`connect()` 经 `_tls_wrap()` 把缓存 session 传给 `wrap_socket(session=...)`，无缓存时传 `None`（= 普通全握手）
- **提取**：`getresponse()` 在**第一次 HTTP 响应返回后**从 `sock.session` 提取并覆盖缓存 —— TLS 1.3 的 `NewSessionTicket` 是握手完成后异步下发的，`connect()` 内读取只会拿到 `None`
- **统计**：每次握手后读 `sock.session_reused`，`tls_cache.stats()` → `{resumed, full, has_cached}`；`_new_verified_conn` 每次建连打一条 `upstream connected (tls resumed=N full=M)` 日志
- **安全**：0-RTT early data 永不开启（重放攻击会重执行非幂等 /cmd）；握手失败时 raw socket 必关闭，不泄漏

测试：`python3 test_tls_resume.py`（14 项，全 mock，零网络）

## 配置

`~/.config/mused/config.json`（权限 600）：

```json
{
  "vps_host": "38.207.167.51",
  "vps_port": 443,
  "sni": "ai.vps38.local",
  "ca_cert_path": "/path/to/vps38-root-ca.pem",
  "token_file": "~/.config/mused/token",
  "socket_path": "~/.local/share/mused/mused.sock",
  "heartbeat_interval": 30
}
```

Token 单独存 `~/.config/mused/token`（600），**永不进 config.json**。

## 日志

`~/.local/share/mused/mused.log` — RotatingFileHandler，10MB × 5。

## 测试方法

```bash
cd ~/workspace/muse-vps-deploy/tests
python3 -m pytest test_daemon.py -v
# 6/6 通过
```

测试用隔离 HOME，不碰真实 `~/.config/mused/`；用错误 token（预期 VPS 返回 401，证明链路通）。

## 实测结果（2026-10-19）

- 错误 token → HTTP 401（链路通，无 SSH）
- `--target RD-928` → 401，body 转发正常，CLI flag 覆盖 body.target
- SIGTERM → socket 清理、上游关闭、进程退出、日志记录
- keep-alive：第二次请求约 27ms（vs SSH 约 300ms）

## 微批聚合（microbatch.py，默认关闭）

`MicroBatcher` 实现 In-Flight Triggered Coalescing（Gemini-4 评审 §4.2-4.3）：

- 串行请求：`in_flight==0` 且队列空 → **0ms 立即发出**，绝不增加延迟
- 并行扇出：`in_flight>0` 时开 1.5ms 收集窗，打包成 1 个 `POST /muse-api/batch`
  （上限 10 条子请求、总 payload ≤64KB，超了立即截断发车）
- 5 条铁律：同 `(token, target)` 才合并；仅 `batchable=true`（socket 协议可选字段，
  缺省时按 `is_read_only` 推断）或只读操作才合并；外层固定 200、子请求独立成败；
  每个 `/batch` 带 `batch_id` + 子 `request_id` 供 VPS 去重

socket 协议新增可选字段：`"target"`、`"batchable"`、`"request_id"`
（`target` 缺省时从 `body.target` 取）。

```bash
cd ~/workspace/muse-vps-deploy/mused
python3 -m pytest test_microbatch.py -v   # 12/12 通过
```

**开启前置条件**：VPS 端必须先实现新的 multiplexing `/batch` 线路格式
`{"batch_id":..., "requests":[{"id","method","path","body"},...]}` →
`200 [{"id","status","body"},...]`（不是旧的 `{"commands":[...]}` shell 批处理）。
实现后把 `config.json` 里 `"microbatch_enabled"` 设为 `true` 并重启 daemon。

## 部署（systemd）

| 文件 | 说明 |
|------|------|
| `mused.service` | systemd unit：`User=hatch`、`Restart=always`、`RestartSec=5`，stderr→journald |
| `install.sh` | 一键安装（默认 user 模式；`--system` 装到 /etc/systemd/system，需 root） |
| `uninstall.sh` | 干净卸载（默认保留配置与日志；`--purge` 全删） |
| `config.json.example` | 配置模板（`//` 注释版，install.sh 自动去注释；逐字段说明见文件内） |

```bash
cd ~/workspace/muse-vps-deploy/mused
./install.sh            # 装到 ~/.local/bin + ~/.config/systemd/user，enable --now
~/.local/bin/museapi --health   # 健康检查：OK 即 daemon 存活
./uninstall.sh          # 卸载（保留配置/日志）
./uninstall.sh --purge   # 连配置带日志全删
```

健康检查：`museapi --health` 连 unix socket 发 `GET /muse-api/status`（空 token）。
daemon 只要回包——哪怕是 401——即判存活（401 本身证明 TLS+nginx+bridge 链路通）。
exit 0=OK，3=daemon 没跑，1=异常。

日志双写（无需 logrotate）：
- `~/.local/share/mused/mused.log`：daemon 内建 `RotatingFileHandler`（10MB × 5）
- journald：daemon 的 stderr，`journalctl --user -u mused -f` 查看

注意：
- user 模式的 unit **不能**写 `User=`（systemd 禁止），install.sh 会自动删掉该行；
  `--system` 模式保留 `User=hatch`。
- daemon 经 egress proxy 做 CONNECT，**必须**有 `https_proxy`/`HTTPS_PROXY` 环境变量；
  install.sh 会把当前 shell 的值注入到安装后的 unit 里（`Environment=`，unit 设 600）。
- token 填到 `~/.config/mused/token`（600）后再启动，否则转发请求会被 VPS 拒（401）。

## 未做（后续）

- `~/workspace/bin/musepc` 切换到走 daemon（保留 SSH 回退）
