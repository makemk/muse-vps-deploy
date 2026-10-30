#!/usr/bin/env bash
# restore.sh — 一键恢复（VM 端执行）
#
#   ./restore.sh --list                          列出可用备份
#   ./restore.sh --date 20261019-030000          dry-run：打印恢复清单，不动手
#   ./restore.sh --date 20261019-030000 --apply   真正执行恢复
#   ./restore.sh --controllers-only               只恢复 controllers.json（场景 B1，用 latest）
#   ./restore.sh --mock-vps DIR                   测试：用本地目录模拟 VPS（不走 SSH）
#
# 默认 dry-run，必须显式 --apply 才写文件。私钥 .enc 包永不自动恢复（手动解密）。
set -euo pipefail

SSH_F="${SSH_F:-/home/hatch/.ssh/config}"
VPS="${VPS:-vps38}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/vps}"
MOCK_VPS=""
DATE=""
APPLY=0
CONTROLLERS_ONLY=0

usage() {
  cat <<EOF
用法: $0 [--list] [--date YYYYMMDD-HHMMSS] [--apply] [--controllers-only]
          [--mock-vps DIR] [--backup-root DIR]
  --list              列出可用备份
  --date TS           指定备份（默认 dry-run，只打印清单）
  --apply             真正执行（必须与 --date 或 --controllers-only 同用）
  --controllers-only  只恢复 controllers.json（从 latest）
  --mock-vps DIR      测试：用本地目录模拟 VPS
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) MODE="list"; shift ;;
    --date) DATE="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --controllers-only) CONTROLLERS_ONLY=1; shift ;;
    --mock-vps) MOCK_VPS="$2"; shift 2 ;;
    --backup-root) BACKUP_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

log() { echo "[restore] $*"; }
die() { echo "[restore] ERROR: $*" >&2; exit 1; }
DRY=""; [[ "$APPLY" -eq 0 ]] && DRY="[dry-run] "

# ---- --list ----
if [[ "${MODE:-}" == "list" ]]; then
  log "可用备份（$BACKUP_ROOT）："
  ls -1 "$BACKUP_ROOT" 2>/dev/null | grep -E "^20" | sort || log "（无）"
  [[ -L "$BACKUP_ROOT/latest" ]] && log "latest -> $(readlink "$BACKUP_ROOT/latest")"
  exit 0
fi

# ---- 确定备份目录 ----
if [[ "$CONTROLLERS_ONLY" -eq 1 ]]; then
  SRC="$BACKUP_ROOT/latest"
  [[ -d "$SRC" ]] || die "没有 latest 备份"
else
  [[ -n "$DATE" ]] || { usage; die "--date 必填（或用 --list 查看）"; }
  SRC="$BACKUP_ROOT/$DATE"
  [[ -d "$SRC" ]] || die "备份不存在: $SRC"
fi
[[ "$APPLY" -eq 1 ]] || log "dry-run 模式：只打印清单，不写任何文件。加 --apply 真正执行。"
log "备份源: $SRC"

# ---- VPS 推送：vps_push <备份内相对路径> <远程路径> [mode] ----
vps_push() {
  local rel="$1" remote="$2" mode="${3:-}"
  local src="$SRC/$rel"
  [[ -f "$src" ]] || { log "WARN: 备份中缺 $rel，跳过"; return 0; }
  if [[ -n "$MOCK_VPS" ]]; then
    if [[ "$APPLY" -eq 1 ]]; then
      mkdir -p "$(dirname "$MOCK_VPS$remote")"
      cp -a "$src" "$MOCK_VPS$remote"
      [[ -n "$mode" ]] && chmod "$mode" "$MOCK_VPS$remote"
    fi
    log "${DRY}MOCK-VPS 恢复: $rel -> $remote"
  else
    if [[ "$APPLY" -eq 1 ]]; then
      ssh -F "$SSH_F" "$VPS" "mkdir -p '$(dirname "$remote")'"
      # shellcheck disable=SC2029
      ssh -F "$SSH_F" "$VPS" "cat > '$remote'" < "$src"
      [[ -n "$mode" ]] && ssh -F "$SSH_F" "$VPS" "chmod $mode '$remote'"
    fi
    log "${DRY}VPS 恢复: $rel -> $remote"
  fi
}

# ---- VM 恢复：vm_restore <备份内相对路径> <目标路径> [mode] ----
vm_restore() {
  local rel="$1" dest="${2/#\~/$HOME}" mode="${3:-}"
  local src="$SRC/$rel"
  [[ -f "$src" ]] || { log "WARN: 备份中缺 $rel，跳过"; return 0; }
  if [[ "$APPLY" -eq 1 ]]; then
    mkdir -p "$(dirname "$dest")"
    # 先备份现有文件
    [[ -e "$dest" ]] && cp -a "$dest" "$dest.pre-restore-$(date +%Y%m%d-%H%M%S)"
    cp -a "$src" "$dest"
    [[ -n "$mode" ]] && chmod "$mode" "$dest"
  fi
  log "${DRY}VM 恢复: $rel -> $dest"
}

if [[ "$CONTROLLERS_ONLY" -eq 1 ]]; then
  # 场景 B1：只恢复 controllers.json
  vps_push "vps/controllers.json" "/etc/muse-bridge/controllers.json" 600
  if [[ "$APPLY" -eq 1 ]]; then
    log "下一步（手动）：ssh 到 VPS 执行 systemctl restart muse-bridge，然后跑验证"
  else
    log "加 --apply 执行后，记得手动 restart muse-bridge 服务"
  fi
  exit 0
fi

# ---- 全量恢复清单 ----
log "=== VPS 文件 ==="
vps_push "vps/muse-bridge.py" "/usr/local/bin/muse-bridge.py"
vps_push "vps/controllers.json" "/etc/muse-bridge/controllers.json" 600
vps_push "vps/vps38-server.pem" "/etc/ssl/certs/vps38-server.pem"
vps_push "vps/vps38-root-ca.pem" "/etc/ssl/certs/vps38-root-ca.pem"
# nginx 配置（目录形式）
if [[ -d "$SRC/vps/nginx-sites" ]]; then
  for f in "$SRC"/vps/nginx-sites/*; do
    [[ -f "$f" ]] || continue
    vps_push "vps/nginx-sites/$(basename "$f")" "/etc/nginx/sites-enabled/$(basename "$f")"
  done
fi
if [[ -d "$SRC/vps/nginx-confd" ]]; then
  for f in "$SRC"/vps/nginx-confd/*; do
    [[ -f "$f" ]] || continue
    vps_push "vps/nginx-confd/$(basename "$f")" "/etc/nginx/conf.d/$(basename "$f")"
  done
fi

log "=== VM 文件 ==="
vm_restore "vm/mused-config.json" "~/.config/mused/config.json" 600
vm_restore "vm/mused.service" "~/.config/systemd/user/mused.service"
# token 明文：默认不自动恢复（敏感），只提示
if [[ -f "$SRC/vm/token.plaintext" ]]; then
  log "备份中有 token.plaintext —— 出于安全考虑不自动恢复。"
  log "如需恢复请手动：cp '$SRC/vm/token.plaintext' ~/.config/mused/token && chmod 600 ~/.config/mused/token"
fi
# 私钥包：永不自动恢复
if compgen -G "$SRC/vps/*.enc" > /dev/null; then
  log "备份中有私钥加密包（*.enc）—— 请手动解密恢复："
  for f in "$SRC"/vps/*.enc; do
    log "  openssl enc -d -aes-256-cbc -pbkdf2 -in '$f' -out <目标路径>"
  done
fi

if [[ "$APPLY" -eq 1 ]]; then
  log "恢复执行完毕。原有 VM 文件已备份为 *.pre-restore-*。"
  log "VPS 端记得：systemctl restart muse-bridge && nginx -t && systemctl reload nginx"
else
  log "dry-run 结束。确认无误后加 --apply 执行。"
fi
