#!/usr/bin/env bash
# backup.sh — 一键备份（VM 端执行）
#
#   ./backup.sh            每日全量：VPS(V1-V5) + VM(M1,M3)，不含私钥明文
#   ./backup.sh --full     上述 + 私钥加密包(V6/V8，需交互输入加密口令)
#   ./backup.sh --tokens   只备份 controllers.json（token 变更后立即跑）
#   ./backup.sh --mock-vps DIR  测试模式：用本地目录模拟 VPS 文件系统
#
# 落盘：~/backups/vps/<YYYYMMDD-HHMMSS>/ + latest 软链接；30天滚动清理。
# 私钥/口令永不进命令行历史（read -s 交互输入）。
set -euo pipefail

SSH_F="${SSH_F:-/home/hatch/.ssh/config}"
VPS="${VPS:-vps38}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/vps}"
MODE="daily"
MOCK_VPS=""

usage() {
  cat <<EOF
用法: $0 [--full] [--tokens] [--mock-vps DIR] [--backup-root DIR]
  --full        含私钥加密包（V6/V8），需交互输入加密口令
  --tokens      只备份 controllers.json（token 变更后立即跑）
  --mock-vps DIR 测试：用本地目录模拟 VPS（不走 SSH）
  --backup-root DIR 覆盖备份根目录（默认 ~/backups/vps）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) MODE="full"; shift ;;
    --tokens) MODE="tokens"; shift ;;
    --mock-vps) MOCK_VPS="$2"; shift 2 ;;
    --backup-root) BACKUP_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

TS="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$TS"
mkdir -p "$DEST/vps" "$DEST/vm"

log() { echo "[backup $TS] $*"; }
die() { echo "[backup $TS] ERROR: $*" >&2; exit 1; }

# ---- VPS 拉取：vps_pull <远程路径> <本地名> ----
vps_pull() {
  local remote="$1" localname="$2"
  if [[ -n "$MOCK_VPS" ]]; then
    [[ -e "$MOCK_VPS$remote" ]] || { log "WARN: mock VPS 缺 $remote，跳过"; return 0; }
    cp -a "$MOCK_VPS$remote" "$DEST/vps/$localname"
  else
    ssh -F "$SSH_F" "$VPS" "cat '$remote'" > "$DEST/vps/$localname" 2>/dev/null \
      || { log "WARN: VPS 缺 $remote，跳过"; rm -f "$DEST/vps/$localname"; return 0; }
  fi
  log "VPS 备份: $remote -> $localname"
}

# ---- VM 本地拷贝：vm_cp <源> <本地名> ----
vm_cp() {
  local src="$1" localname="$2"
  # shellcheck disable=SC2086
  src_expanded="${src/#\~/$HOME}"
  [[ -e "$src_expanded" ]] || { log "WARN: VM 缺 $src，跳过"; return 0; }
  cp -a "$src_expanded" "$DEST/vm/$localname"
  log "VM 备份: $src -> $localname"
}

log "模式=$MODE 目标=$DEST"

# ============ VPS 端 ============
if [[ "$MODE" != "tokens" ]]; then
  # V1: muse-bridge.py + 校验
  vps_pull "/usr/local/bin/muse-bridge.py" "muse-bridge.py"
  if [[ -f "$DEST/vps/muse-bridge.py" ]]; then
    (cd "$DEST/vps" && "$HOME/.local/bin/b3sum" muse-bridge.py > muse-bridge.py.b3)
    # V2: .bak-* 一并打包
    if [[ -n "$MOCK_VPS" ]]; then
      cp -a "$MOCK_VPS"/usr/local/bin/muse-bridge.py.bak-* "$DEST/vps/" 2>/dev/null || true
    else
      ssh -F "$SSH_F" "$VPS" 'ls /usr/local/bin/muse-bridge.py.bak-* 2>/dev/null' | while read -r f; do
        ssh -F "$SSH_F" "$VPS" "cat '$f'" > "$DEST/vps/$(basename "$f")" 2>/dev/null || true
      done
    fi
  fi
  # V4: nginx 站点配置（含 muse-api 的）
  vps_pull "/etc/nginx/sites-enabled/default" "nginx-default" 2>/dev/null || true
  if [[ -n "$MOCK_VPS" ]]; then
    mkdir -p "$DEST/vps/nginx-sites"
    cp -a "$MOCK_VPS"/etc/nginx/sites-enabled/. "$DEST/vps/nginx-sites/" 2>/dev/null || true
    cp -a "$MOCK_VPS"/etc/nginx/conf.d/. "$DEST/vps/nginx-confd/" 2>/dev/null || true
  else
    mkdir -p "$DEST/vps/nginx-sites" "$DEST/vps/nginx-confd"
    for f in $(ssh -F "$SSH_F" "$VPS" 'ls /etc/nginx/sites-enabled/ 2>/dev/null'); do
      ssh -F "$SSH_F" "$VPS" "cat '/etc/nginx/sites-enabled/$f'" > "$DEST/vps/nginx-sites/$f" 2>/dev/null || true
    done
    for f in $(ssh -F "$SSH_F" "$VPS" 'ls /etc/nginx/conf.d/ 2>/dev/null'); do
      ssh -F "$SSH_F" "$VPS" "cat '/etc/nginx/conf.d/$f'" > "$DEST/vps/nginx-confd/$f" 2>/dev/null || true
    done
  fi
  # V5: 服务器证书（公钥）
  vps_pull "/etc/ssl/certs/vps38-server.pem" "vps38-server.pem"
  # V7: Root CA 公钥
  vps_pull "/etc/ssl/certs/vps38-root-ca.pem" "vps38-root-ca.pem"
fi

# V3: controllers.json（daily 和 tokens 模式都要；600 权限保持）
vps_pull "/etc/muse-bridge/controllers.json" "controllers.json"
if [[ -f "$DEST/vps/controllers.json" ]]; then
  chmod 600 "$DEST/vps/controllers.json"
  python3 -c "import json; json.load(open('$DEST/vps/controllers.json'))" \
    && log "controllers.json JSON 校验通过" \
    || die "controllers.json 损坏（JSON 解析失败），备份中止"
fi

# ============ VM 端 ============
if [[ "$MODE" != "tokens" ]]; then
  vm_cp "~/.config/mused/config.json" "mused-config.json"
  vm_cp "~/.config/systemd/user/mused.service" "mused.service"
  # M4 可从仓库重装，只记版本信息
  [[ -x "$HOME/.local/bin/mused" ]] && "$HOME/.local/bin/b3sum" "$HOME/.local/bin/mused" > "$DEST/vm/mused.b3" || true
fi
# M2: token 明文（600）——只在 daily/full 备份；tokens 模式不管它（那是加密包的事）
if [[ "$MODE" != "tokens" && -f "$HOME/.config/mused/token" ]]; then
  cp -a "$HOME/.config/mused/token" "$DEST/vm/token.plaintext"
  chmod 600 "$DEST/vm/token.plaintext"
  log "WARN: token 明文已备份到 $DEST/vm/token.plaintext —— 确保备份根目录权限安全"
fi

# ============ --full: 私钥加密包 ============
if [[ "$MODE" == "full" ]]; then
  command -v openssl >/dev/null || die "需要 openssl 做加密"
  echo -n "输入加密口令（V6/V8 私钥包，不回显）: "
  read -rs ENC_PW; echo
  [[ -n "$ENC_PW" ]] || die "口令不能为空"
  echo -n "再输一次确认: "
  read -rs ENC_PW2; echo
  [[ "$ENC_PW" == "$ENC_PW2" ]] || die "两次口令不一致"

  enc_pull() { # <远程路径> <输出名>
    local remote="$1" out="$2" tmp
    tmp="$(mktemp)"
    if [[ -n "$MOCK_VPS" ]]; then
      [[ -e "$MOCK_VPS$remote" ]] || { log "WARN: mock VPS 缺私钥 $remote，跳过"; rm -f "$tmp"; return 0; }
      cp "$MOCK_VPS$remote" "$tmp"
    else
      ssh -F "$SSH_F" "$VPS" "cat '$remote'" > "$tmp" 2>/dev/null \
        || { log "WARN: VPS 缺私钥 $remote，跳过"; rm -f "$tmp"; return 0; }
    fi
    openssl enc -aes-256-cbc -pbkdf2 -in "$tmp" -out "$DEST/vps/$out" -pass "pass:$ENC_PW"
    chmod 600 "$DEST/vps/$out"
    shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
    log "私钥加密备份: $remote -> $out (aes-256-cbc)"
  }
  enc_pull "/etc/ssl/private/vps38-server.key" "vps38-server.key.enc"
  enc_pull "/etc/ssl/private/vps38-root-ca.key" "vps38-root-ca.key.enc"
  unset ENC_PW ENC_PW2
fi

# ============ 收尾 ============
# latest 软链接
ln -sfn "$DEST" "$BACKUP_ROOT/latest"
# 备份清单
(cd "$DEST" && find . -type f | sort > MANIFEST.txt)
# 30 天滚动清理（私钥 .enc 包不删）
if [[ "$MODE" == "daily" ]]; then
  find "$BACKUP_ROOT" -maxdepth 1 -type d -name "20*" -mtime +30 | while read -r old; do
    if compgen -G "$old/vps/*.enc" > /dev/null; then
      log "保留含私钥包的备份: $old（手动清理）"
    else
      rm -rf "$old" && log "清理过期备份: $old"
    fi
  done
fi

# 备份根目录权限
chmod 700 "$BACKUP_ROOT"

log "完成。清单: $DEST/MANIFEST.txt"
log "latest -> $DEST"
