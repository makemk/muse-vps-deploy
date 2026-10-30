# nginx/ — VPS 命令通道 Nginx 生产配置

本目录交付 VPS 38.207.167.51 上 `/muse-api/`（VM Daemon → VPS HTTPS 命令通道）
的 Nginx 生产配置，依据 Gemini 4 两轮架构评审逐项落地。

## 文件清单

| 文件 | 说明 |
|------|------|
| `muse-api.conf` | 完整 server 块：upstream + TLS + `location /muse-api/` |
| `test-nginx.sh` | 本机 mock 校验：结构检查 30 项；有 nginx 则跑真实 `nginx -t` |
| `README.md` | 本文件：每条指令的作用、取值理由、与评审的对应关系 |

## 部署步骤（VPS 上执行）

```bash
# 1. 备份现有配置
sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak-$(date +%Y%m%d)
sudo nginx -T > /tmp/nginx-full-backup.txt 2>&1   # 导出当前全量有效配置

# 2. 先看一眼：现有 443 server 块里 WSS location 的确切写法，记下来
sudo grep -n -A 15 "location.*wss\|location /" /etc/nginx/conf.d/*.conf | head -60

# 3. 部署（把 muse-api.conf 传到 VPS 后）
./test-nginx.sh muse-api.conf          # 先在本机确认 ALL PASS（已做过）
sudo cp muse-api.conf /etc/nginx/conf.d/muse-api.conf
# 4. 把第 2 步记下的 WSS location 段填入 muse-api.conf 的
#    "EXISTING WSS LOCATION" 占位处（不要删、不要改）
sudo nginx -t && sudo systemctl reload nginx

# 5. 验证
curl -k -s -o /dev/null -w "%{http_code}\n" https://ai.vps38.local/muse-api/status
# 预期：401（链路通，鉴权层拒绝）——不是 502/000
```

## 指令逐条说明

### upstream 段（对应第一轮评审 §3.2）

| 指令 | 取值 | 为什么 |
|------|------|--------|
| `server 127.0.0.1:8471 max_fails=0` | 本机回环 | `max_fails=0` = 永不摘除本机后端；8471 是单进程 Python 服务，摘了就没备选，摘除只会把故障放大 |
| `keepalive 32` | 32 | 每个 worker 保持的空闲长连接数。Daemon 侧连接池上限 16 条，Nginx 侧 32 留余量。**回环也必须配**：否则 Nginx 默认 HTTP/1.0 短连接，每请求一次 TCP 握手/挥手，堆积 TIME_WAIT 并给 Python accept() 造成 1~3ms 抖动 |
| `keepalive_requests 100000` | 10 万 | 单连接复用上限，防 Python 服务端长期运行的内存微泄漏累积 |
| `keepalive_timeout 60s` | 60s | 空闲保持。**必须略小于 8471 服务端的 idle timeout**，让 Nginx 侧先断，避免"服务端已发 FIN、Nginx 恰好复用"的竞态 |

### server / TLS 段（对应第二轮评审 §3.3、§3.4）

| 指令 | 取值 | 为什么 |
|------|------|--------|
| `listen 443 ssl http2` | — | HTTP/2 给 VM→Nginx 段；**绝不能加 `default_server`**：无 SNI 的请求会被路由到 xray 掐掉（2026-10-17 的 1006 循环故障根因），SNI=`ai.vps38.local` 是路由依据 |
| `server_name ai.vps38.local` | — | 与 Daemon 侧 SNI 一致 |
| `ssl_certificate{,_key}` | vps38-server.pem | 自建 PKI（VPS38 Root CA，2026-10-18 已切换）。key 路径若与 VPS 实际不一致，改这一行即可 |
| `ssl_protocols TLSv1.2 TLSv1.3` | — | 兼顾兼容与性能；TLS 1.3 握手本身 1-RTT |
| `ssl_session_cache shared:MuseSSL:10m` | 10m | 共享内存会话缓存，约 4 万个会话，供 TLS Session Ticket / 1.3 PSK 快速恢复（1-RTT），Daemon 连接池重建连接时省去完整握手 |
| `ssl_session_timeout 1h` | 1h | 会话有效期 |
| `ssl_session_tickets on` | on | 开启 Session Ticket 下发 |
| `ssl_early_data off` | **off** | ⛔ **红线**：TLS 1.3 0-RTT 无防重放保护，中间设备重传 0-RTT 包会导致非幂等 `/cmd` 被重放执行。Nginx 默认 off，此处显式写出是防后人手贱打开 |
| `keepalive_requests 100000` | 10 万 | 客户端侧单连接复用上限，避免频繁重连 |
| `keepalive_timeout 75s` | 75s | **必须 > upstream 的 60s**：保证总是上游先断，Nginx 侧不踩"复用半关闭连接"竞态 |

### location /muse-api/ 段（对应第一轮评审 §3.2）

| 指令 | 取值 | 为什么 |
|------|------|--------|
| `proxy_http_version 1.1` | 1.1 | **核心三行之一**：不设则 Nginx 对上游用 HTTP/1.0 + `Connection: close`，keepalive 全部失效 |
| `proxy_set_header Connection ""` | 空 | **核心三行之二**：清空 Connection 头是激活 upstream keepalive 的前提 |
| `proxy_socket_keepalive on` | on | **核心三行之三**：开启上游连接的 TCP keepalive 探测 |
| `proxy_set_header X-Real-IP $remote_addr` | — | 8471 鉴权层用它校验 `allowed_ips`。注意：经 Nginx 转发后，8471 看到的 TCP 源永远是 127.0.0.1，**绝不能拿它做白名单 bypass**（见评审 8：那等于对全公网免鉴权） |
| `proxy_set_header X-Forwarded-For/Proto/Host` | — | 透传客户端信息；Host 透传保证 SNI/Host 一致性检查不误杀 |
| `proxy_buffering off` | off | 小包命令直通，降转发延迟。`/file` 大包走应用层 64KB 分块 + 端到端压缩，不依赖 Nginx 缓冲 |
| `proxy_request_buffering off` | off | 同上，请求体也不缓冲 |
| `proxy_connect_timeout 2s` | 2s | 连本机回环必须极快，2s 足够，超时即异常 |
| `proxy_send_timeout 60s` | 60s | 发往上游超时 |
| `proxy_read_timeout 300s` | 300s | 读超时需容纳长耗时命令（`/batch` 上限 300s 对齐） |

### 可选限流（默认关闭，对应安全加固评审）

`muse-api.conf` 内以注释形式给出，按需取消注释：

- `limit_req_zone $binary_remote_addr zone=museapi:10m rate=10r/s`（http{} 顶层）+ `limit_req zone=museapi burst=20 nodelay`：AI 并行扇出 5~15 并发下 burst 20 够用，误杀再调大
- `client_max_body_size 1m`：配合鉴权层 `MAX_BODY_SIZE`，防未认证内存 DoS（评审残留风险 P1）

## 与 8471 Python 服务端的配套要求

Nginx 侧配好 keepalive 只是半边天，服务端必须真正支持 HTTP/1.1 持久连接，
否则 Python 标准库服务端会在每次响应后强制断开：

- 若用 `http.server.BaseHTTPRequestHandler`：显式设置 `protocol_version = "HTTP/1.1"`
  并正确返回 `Content-Length` 头
- 服务端的 idle timeout 必须 **> 60s**（Nginx upstream 的 `keepalive_timeout`），
  推荐 70~75s，让 Nginx 侧先断

## 未来扩展钩子（暂不实施，评审已定案）

- **QPS > 3000 才考虑**：按 `$http_x_muse_target` 一致性哈希分片多进程
  （`hash $http_x_muse_target consistent` + 4 个 8471~8474 端口）。
  现阶段单进程 + uvloop/orjson 足够（2500~4000 QPS），**严禁**直接用
  `SO_REUSEPORT` 或轮询扩多进程——WSS 连接是进程内存态，跨进程路由会丢。
- 详见 `~/workspace/user/files/code_artifact__2.md` §2。

## 测试方法

```bash
cd ~/workspace/muse-vps-deploy/nginx
./test-nginx.sh
# 预期：30 项 [PASS]，结果 ALL PASS；B 段 SKIP（本机无 nginx）
# 真实 nginx -t 留到 VPS 部署时执行（脚本已打印确切命令）
```
