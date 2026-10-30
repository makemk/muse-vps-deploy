#!/usr/bin/env bash
# install.sh — 一键安装 mused daemon
#
#   ./install.sh              # user 模式（默认）：~/.config/systemd/user/ + systemctl --user
#   ./install.sh --system      # system 模式：/etc/systemd/system/ + systemctl（需 root）
#
# 做的事：
#   1. mused.py   -> ~/.local/bin/mused   (0755)
#   2. museapi    -> ~/.local/bin/museapi  (0755)
#   3. config.json.example（去注释） -> ~/.config/mused/config.json（600，不覆盖已有）
#   4. 空 token 文件 -> ~/.config/mused/token（600，不覆盖已有；用户需自行填入）
#   5. mused.service -> systemd user/system 目录
#        - user 模式：删掉 User= 行（user unit 不允许写 User=）
#        - 注入当前 shell 的 https_proxy / HTTPS_PROXY（daemon CONNECT 必需）
#        - 安装后的 unit 设 600（proxy URL 可能含认证信息）
#   6. daemon-reload && enable --now
#
# 无 user bus（如容器/无 lingering）时跳过 systemctl 步骤，只做文件安装并给出手动命令。
set -u
cd "$(dirname "$0")"

MODE="user"
for a in "$@"; do
  case "$a" in
    --system) MODE="system" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

BIN_DIR="$HOME/.local/bin"
CONF_DIR="$HOME/.config/mused"
CONF_FILE="$CONF_DIR/config.json"
TOKEN_FILE="$CONF_DIR/token"

if ! command -v python3 >/dev/null; then
  echo "ERROR: python3 not found" >&2; exit 1
fi

echo "==> [1/6] 安装可执行文件 -> $BIN_DIR"
mkdir -p "$BIN_DIR"
cp mused.py "$BIN_DIR/mused"
cp museapi  "$BIN_DIR/museapi"
chmod 755 "$BIN_DIR/mused" "$BIN_DIR/museapi"
python3 -m py_compile "$BIN_DIR/mused" && echo "    py_compile OK"

echo "==> [2/6] 写配置 -> $CONF_FILE"
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
if [ -f "$CONF_FILE" ]; then
  echo "    已存在，不覆盖（如需重置请手动删除）"
else
  # 去掉 // 整行注释，转为纯 JSON
  sed -E '/^[[:space:]]*\/\//d' config.json.example > "$CONF_FILE"
  chmod 600 "$CONF_FILE"
  python3 -c "import json; json.load(open('$CONF_FILE'))" \
    && echo "    config.json 已生成（JSON 合法）" \
    || { echo "ERROR: 生成的 config.json 非法" >&2; exit 1; }
fi

echo "==> [3/6] token 文件 -> $TOKEN_FILE"
if [ -f "$TOKEN_FILE" ]; then
  echo "    已存在，不覆盖"
else
  : > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "    已创建空文件（600），请填入控制器 token 后再启动："
  echo "      echo -n 'muse_...' > $TOKEN_FILE"
fi

echo "==> [4/6] 安装 systemd unit（$MODE 模式）"
TMP_UNIT="$(mktemp)"
cp mused.service "$TMP_UNIT"
if [ "$MODE" = "user" ]; then
  # user unit 禁止 User= 指令，删掉
  sed -i -E '/^User=/d' "$TMP_UNIT"
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT_DEST="$UNIT_DIR/mused.service"
  SYSTEMCTL=(systemctl --user)
else
  [ "$(id -u)" = "0" ] || { echo "ERROR: --system 需要 root" >&2; exit 1; }
  UNIT_DIR="/etc/systemd/system"
  UNIT_DEST="$UNIT_DIR/mused.service"
  SYSTEMCTL=(systemctl)
fi
# 注入代理环境（daemon 经 egress proxy 做 CONNECT，缺了起不来）。
# 必须插在 [Service] 段内、[Install] 段之前（[Install] 段里的 Environment= 会被忽略）。
ENV_TMP="$(mktemp)"
for var in https_proxy HTTPS_PROXY; do
  val="${!var:-}"
  if [ -n "$val" ]; then
    printf 'Environment=%s=%s\n' "$var" "$val" >> "$ENV_TMP"
    echo "    已注入 $var（值不打印）"
  fi
done
if [ -s "$ENV_TMP" ]; then
  awk -v envf="$ENV_TMP" '
    BEGIN { n=0; while ((getline line < envf) > 0) env[++n]=line; close(envf) }
    /^\[Install\]$/ { for (i=1;i<=n;i++) print env[i] }
    { print }
  ' "$TMP_UNIT" > "$TMP_UNIT.new" && mv "$TMP_UNIT.new" "$TMP_UNIT"
fi
rm -f "$ENV_TMP"
mkdir -p "$UNIT_DIR"
cp "$TMP_UNIT" "$UNIT_DEST"
chmod 600 "$UNIT_DEST"   # proxy URL 可能含认证信息
rm -f "$TMP_UNIT"
echo "    unit -> $UNIT_DEST"

echo "==> [5/6] daemon-reload + enable --now"
if "${SYSTEMCTL[@]}" daemon-reload 2>/dev/null; then
  "${SYSTEMCTL[@]}" enable --now mused.service
  echo "    已 enable --now"
else
  echo "    WARN: 无 systemd $MODE bus，跳过 enable（文件已装好）"
  echo "    手动启动： ${SYSTEMCTL[*]} enable --now mused.service"
  echo "    或直接跑： $BIN_DIR/mused &"
fi

echo "==> [6/6] 状态"
if "${SYSTEMCTL[@]}" is-active --quiet mused.service 2>/dev/null; then
  "${SYSTEMCTL[@]}" --no-pager status mused.service | head -12
  echo
  echo "健康检查："
  "$BIN_DIR/museapi" --health || true
else
  echo "    （service 未在 systemd 下运行，见上方 WARN）"
fi

echo
echo "常用命令："
if [ "$MODE" = "user" ]; then
  echo "  状态  systemctl --user status mused"
  echo "  日志  journalctl --user -u mused -f        # journald（daemon 的 stderr）"
else
  echo "  状态  systemctl status mused"
  echo "  日志  journalctl -u mused -f"
fi
echo "  文件  tail -f ~/.local/share/mused/mused.log  # daemon 内建轮转日志"
echo "  健康  ~/.local/bin/museapi --health"
echo "  卸载  ./uninstall.sh$([ "$MODE" = system ] && echo ' --system')"
