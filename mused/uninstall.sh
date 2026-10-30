#!/usr/bin/env bash
# uninstall.sh — 干净卸载 mused daemon
#
#   ./uninstall.sh              # 卸载 user 模式安装
#   ./uninstall.sh --system     # 卸载 system 模式安装（需 root）
#   ./uninstall.sh --purge      # 额外删除 ~/.config/mused 与 ~/.local/share/mused
#   可组合：./uninstall.sh --system --purge
#
# 默认保留 ~/.config/mused（配置+token）与 ~/.local/share/mused（日志），
# 只删：systemd unit、~/.local/bin/mused、~/.local/bin/museapi。
set -u
cd "$(dirname "$0")"

MODE="user"; PURGE=0
for a in "$@"; do
  case "$a" in
    --system) MODE="system" ;;
    --purge)  PURGE=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "user" ]; then
  UNIT_DEST="$HOME/.config/systemd/user/mused.service"
  SYSTEMCTL=(systemctl --user)
else
  [ "$(id -u)" = "0" ] || { echo "ERROR: --system 需要 root" >&2; exit 1; }
  UNIT_DEST="/etc/systemd/system/mused.service"
  SYSTEMCTL=(systemctl)
fi

echo "==> [1/4] 停服务并 disable"
"${SYSTEMCTL[@]}" disable --now mused.service 2>/dev/null \
  && echo "    已 disable --now" \
  || echo "    （service 未运行或无 bus，跳过）"

echo "==> [2/4] 删 unit 文件"
if [ -f "$UNIT_DEST" ]; then
  rm -f "$UNIT_DEST" && echo "    已删 $UNIT_DEST"
else
  echo "    不存在，跳过"
fi
"${SYSTEMCTL[@]}" daemon-reload 2>/dev/null || true

echo "==> [3/4] 删可执行文件"
for f in "$HOME/.local/bin/mused" "$HOME/.local/bin/museapi"; do
  if [ -e "$f" ]; then rm -f "$f" && echo "    已删 $f"; fi
done
# 顺手清掉可能残留的 socket（daemon 正常退出时会自己删）
[ -S /tmp/mused.sock ] && rm -f /tmp/mused.sock && echo "    已清 /tmp/mused.sock"
sock_cfg="$(python3 -c "
import json,os
p=os.path.expanduser('~/.config/mused/config.json')
try: print(json.load(open(p)).get('socket_path',''))
except Exception: print('')" 2>/dev/null)"
if [ -n "$sock_cfg" ] && [ -S "$sock_cfg" ]; then
  rm -f "$sock_cfg" && echo "    已清 $sock_cfg"
fi

echo "==> [4/4] 配置与日志"
if [ "$PURGE" = "1" ]; then
  rm -rf "$HOME/.config/mused" "$HOME/.local/share/mused"
  echo "    已 purge：~/.config/mused 与 ~/.local/share/mused（含 token 与日志）"
else
  echo "    保留 ~/.config/mused（配置+token）与 ~/.local/share/mused（日志）"
  echo "    如需全删，加 --purge"
fi

echo
echo "卸载完成。残留检查："
"${SYSTEMCTL[@]}" --no-pager status mused.service 2>/dev/null | head -3 || true
ls "$HOME/.local/bin/mused" "$UNIT_DEST" 2>/dev/null || echo "(无残留)"
exit 0
