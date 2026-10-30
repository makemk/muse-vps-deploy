# 健康检查端点实现规格

给实施阶段用：daemon 加两个本地 op，VPS 加一个免鉴权路径。

## 1. VM daemon：`health` / `stats` ops（unix socket）

### 位置

`mused.py` → `handle_client()` 开头，在 `upstream.request()` 之前：

```python
def handle_client(client_sock, upstream):
    try:
        req = recv_frame(client_sock)
        op = req.get("op")
        if op == "health":
            send_frame(client_sock, daemon_health())
            return
        if op == "stats":
            send_frame(client_sock, daemon_stats())
            return
        # ... existing token/method/path handling ...
```

### `daemon_health()` 返回

```python
{
    "status": "ok",                 # "ok" | "degraded"
    "version": "1.0.0",             # daemon 版本
    "uptime_s": int,                # 启动至今秒数
    "upstream_ok": bool,            # 到 VPS 的长连接是否健康
    "last_heartbeat_ok": int,       # 上次心跳成功 epoch
}
```

- `upstream_ok=False` 时 `status="degraded"`（socket 本地正常，但上游断了）。

### `daemon_stats()` 返回

```python
{
    "req_total": int,               # 累计请求数
    "req_2xx": int,
    "req_4xx": int,
    "req_5xx": int,
    "req_transport_err": int,       # status=0（连接异常）
    "lat_p50_ms": float | None,
    "lat_p95_ms": float | None,
    "lat_p99_ms": float | None,
    "reconnect_count": int,         # 上游重连累计
    "window_s": 300,                # 延迟统计窗口
}
```

### 延迟统计实现建议

daemon 里维护一个 `collections.deque(maxlen=1000)` 存最近请求的延迟 ms。
每次 `stats` 被调用时排序取分位数。1000 个 float 排序 < 1ms，可忽略开销。

计数器用 `threading.Lock` 保护（handle_client 可能是多线程的，看 daemon 实现）。

### 测试

```bash
python3 - <<'EOF'
import json, socket, struct
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(os.path.expanduser("~/.local/share/mused/mused.sock"))
for op in ("health", "stats"):
    blob = json.dumps({"op": op}).encode()
    s.sendall(struct.pack("!I", len(blob)) + blob)
    n = struct.unpack("!I", s.recv(4))[0]
    print(op, json.loads(s.recv(n)))
EOF
```

## 2. VPS：`GET /muse-api/health`（免鉴权）

### 位置

`muse-bridge.py` → `handle_connection()`，在鉴权块**之前**：

```python
if path == "/muse-api/health":
    clients = list_active_clients()
    await send_json({
        "status": "ok",
        "version": BRIDGE_VERSION,
        "uptime_s": int(time.time() - START_TIME),  # 需要加 START_TIME = time.time()
        "wss_clients": len(clients),
        "client_list": [
            {"name": c.client_info.get("hostname", "?"),
             "last_seen": int(c.last_seen),          # 需要 client 记录心跳时间
             "ip": anonymize_ip(c.peer_ip)}          # 脱敏：只留 /24
            for c in clients
        ],
        "pending_futures": len(pending_futures),
        "api": {
            "req_total": API_REQ_TOTAL,
            "http_401": API_401_COUNT,
            "http_403": API_403_COUNT,
            "lat_p99_ms": api_lat_p99(),
        },
    })
    return
```

### 需要新增的全局状态

| 变量 | 说明 |
|------|------|
| `START_TIME = time.time()` | 进程启动时间 |
| `API_REQ_TOTAL` / `API_401_COUNT` / `API_403_COUNT` | 在现有鉴权/分发处 `+= 1` |
| 每个 WSS client 的 `last_seen` | 现有心跳/ping 处更新 |
| API 延迟 deque | `/cmd`、`/batch`、`/file` handler 记录耗时 |

### nginx 限流

```nginx
limit_req_zone $binary_remote_addr zone=health:10m rate=10r/s;
location = /muse-api/health {
    limit_req zone=health burst=20 nodelay;
    proxy_pass http://127.0.0.1:8471;
}
```

### 安全注意

- 不返回 token、不返回命令内容、不返回完整 IP。
- `client_list` 的 `ip` 脱敏到 /24（如 `1.2.3.0/24`）。
- 该路径**不计入** 401 统计（它是免鉴权的）。

### 测试

```bash
# 本机
curl -s http://127.0.0.1:8471/muse-api/health | python3 -m json.tool
# 经 nginx（VM 上）
curl -sk --resolve ai.vps38.local:443:38.207.167.51 \
  https://ai.vps38.local/muse-api/health | python3 -m json.tool
```

## 3. 实施顺序

1. daemon 加 `health`/`stats`（约 40 行）→ `vm_monitor.py` 自动生效
2. VPS 加 `/muse-api/health`（约 50 行 + nginx 限流）→ `vps_monitor.py` 自动生效
3. 两边 cron 配好，观察 1 天 alerts.log 无误报再配 hook
