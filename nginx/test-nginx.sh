#!/usr/bin/env bash
# test-nginx.sh — muse-api.conf 语法/结构校验（VM 本机 mock，不实际部署）
#
# 用法：./test-nginx.sh [path/to/muse-api.conf]
#   - 本机有 nginx：生成自签 dummy 证书，跑真实 `nginx -t`
#   - 本机无 nginx（当前 VM）：跑结构化静态校验，并打印 VPS 上执行的确切命令
# 退出码：0 全部通过；1 有失败项。
set -u

CONF="${1:-$(dirname "$0")/muse-api.conf}"
FAIL=0

say()  { printf '%s\n' "$*"; }
pass() { say "  [PASS] $*"; }
fail() { say "  [FAIL] $*"; FAIL=1; }

say "== 检查目标: $CONF =="
[ -f "$CONF" ] || { fail "配置文件不存在"; exit 1; }

# ---------------------------------------------------------------------------
# A. 通用结构校验（无论有无 nginx 都跑）
# ---------------------------------------------------------------------------
say "== A. 结构校验 =="

# A1. 花括号配平
open=$(grep -o '{' "$CONF" | wc -l)
close=$(grep -o '}' "$CONF" | wc -l)
if [ "$open" -eq "$close" ] && [ "$open" -gt 0 ]; then
    pass "花括号配平 ($open 对)"
else
    fail "花括号不配平: { x$open  } x$close"
fi

# A2. 指令行必须以 ; / { / } 结尾（忽略空行、纯注释行）
bad=$(grep -vE '^\s*(#|$)' "$CONF" | grep -vE '[;{}]\s*$' | head -5)
if [ -z "$bad" ]; then
    pass "所有指令行以 ;/{/} 结尾"
else
    fail "发现未正常结尾的行:"; say "$bad"
fi

# A3. 必需指令存在性检查（与 Gemini 评审逐项对应）
need=(
  "upstream muse_cmd_backend"          # §3.2 upstream 定义
  "server 127.0.0.1:8471"              # §3.2 上游地址
  "keepalive 32;"                      # §3.2 upstream keepalive
  "keepalive_requests 100000;"         # §3.2 上下游复用上限
  "keepalive_timeout 60s;"             # §3.2 上游空闲超时
  "keepalive_timeout 75s;"             # §3.2 客户端空闲超时（>上游）
  "listen 443 ssl http2;"              # TLS 入口
  "server_name ai.vps38.local;"        # SNI 路由依据
  "ssl_protocols TLSv1.2 TLSv1.3;"     # §3.4
  "ssl_session_cache shared:MuseSSL:10m;" # §3.4 会话复用
  "ssl_session_timeout 1h;"            # §3.4
  "ssl_session_tickets on;"            # §3.4
  "ssl_early_data off;"                # §3.3 ⛔禁 0-RTT
  "location /muse-api/"                # 命令通道
  "proxy_pass http://muse_cmd_backend;" # 转发目标
  "proxy_http_version 1.1;"            # §3.2 核心三行之一
  'proxy_set_header Connection "";'    # §3.2 核心三行之二
  "proxy_socket_keepalive on;"         # §3.2 核心三行之三
  "proxy_set_header X-Real-IP"         # allowed_ips 校验依据
  "proxy_buffering off;"               # 低延迟直通
  "proxy_request_buffering off;"       # 低延迟直通
  "proxy_connect_timeout 2s;"          # 超时三件套
  "proxy_send_timeout 60s;"
  "proxy_read_timeout 300s;"
)
for n in "${need[@]}"; do
    if grep -qF "$n" "$CONF"; then pass "必需指令: $n"
    else fail "缺失必需指令: $n"; fi
done

# A4. 安全红线：绝不能是 default_server（无 SNI 会被 xray 掐 → 1006 故障重演）
if grep -qE 'listen\s+443[^;]*default_server' "$CONF"; then
    fail "红线：443 listen 带 default_server，无 SNI 请求会被误路由！"
else
    pass "无 default_server（SNI 路由安全）"
fi

# A5. ssl_early_data 必须显式 off（防后人手贱打开）
if grep -qE '^\s*ssl_early_data\s+off;' "$CONF"; then
    pass "ssl_early_data 显式 off（防 0-RTT 重放）"
else
    fail "ssl_early_data 未显式关闭！"
fi

# ---------------------------------------------------------------------------
# B. 真实 nginx -t（本机有 nginx 才跑）
# ---------------------------------------------------------------------------
say "== B. nginx -t 真实语法检查 =="
if command -v nginx >/dev/null 2>&1; then
    TMPD=$(mktemp -d /tmp/nginx-test.XXXXXX)
    trap 'rm -rf "$TMPD"' EXIT
    # 自签 dummy 证书（仅用于 -t 能读到文件，不用于真实流量）
    openssl req -x509 -newkey rsa:2048 -keyout "$TMPD/dummy.key" -out "$TMPD/dummy.crt" \
        -days 1 -nodes -subj "/CN=ai.vps38.local" >/dev/null 2>&1
    sed -e "s|/etc/ssl/certs/vps38-server.pem|$TMPD/dummy.crt|" \
        -e "s|/etc/ssl/private/vps38-server.key|$TMPD/dummy.key|" \
        "$CONF" > "$TMPD/muse-api-test.conf"
    cat > "$TMPD/nginx.conf" <<EOF
events { worker_connections 64; }
http {
    access_log off;
    include $TMPD/muse-api-test.conf;
}
EOF
    if nginx -t -c "$TMPD/nginx.conf" -p "$TMPD/" 2>&1; then
        pass "nginx -t 语法检查通过"
    else
        fail "nginx -t 语法检查未通过（见上）"
    fi
else
    say "  [SKIP] 本机无 nginx 可执行文件，跳过真实 -t。"
    say "  请在 VPS 上执行以下确切命令完成真实校验："
    say "    sudo cp muse-api.conf /etc/nginx/conf.d/muse-api.conf"
    say "    sudo nginx -t && sudo systemctl reload nginx"
fi

say "== 结果: $([ "$FAIL" -eq 0 ] && echo ALL PASS || echo 有失败项) =="
exit "$FAIL"
