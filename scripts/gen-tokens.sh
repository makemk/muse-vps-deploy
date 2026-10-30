#!/usr/bin/env bash
# =============================================================================
# gen-tokens.sh — Generate controller tokens for muse-bridge
#
# RUN ON THE VPS (as root). Creates:
#   /etc/muse-bridge/controllers.json  (600 root:root, SHA-256 only, NO plaintext)
#
# Tokens generated:
#   1. super        — super-admin muse, pcs=["*"]  (target mandatory)
#   2. muse-rd928   — bound to RD-928 (1:1)
#   3. muse-homepc  — bound to homepc (1:1)
#
# The PLAINTEXT tokens are printed ONCE to stderr. They can NEVER be recovered
# from controllers.json. Distribute them NOW via a secure channel:
#   - super token    -> the operator's secure vault (super-admin muse)
#   - muse-rd928     -> RD-928's muse (via existing secure file-drop)
#   - muse-homepc    -> homepc's muse
#
# NEVER: commit to git, paste in chat, log to files.
# To add more:  ./gen-tokens.sh --add --muse <id> --pcs <pc1,pc2> --label "..."
# To revoke:    set "enabled": false for the entry (hot-reloaded, no restart)
# =============================================================================
set -euo pipefail

# ---------------- VPS paths (edit here if your layout differs) ----------------
CONTROLLERS_JSON="${CONTROLLERS_JSON:-/etc/muse-bridge/controllers.json}"
GEN_SCRIPT="${GEN_SCRIPT:-/tmp/token-mgr-proto/gen_token.py}"
BACKUP_SUFFIX="$(date +%Y%m%d-%H%M%S)"
# ------------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage:
  $0                       Generate the 3 standard tokens (super, muse-rd928, muse-homepc)
  $0 --add --muse <id> --pcs <pc1,pc2|*> [--label <text>]
                           Add one more controller (merges into existing file)
  $0 --list                List controllers (muse/pcs/label/enabled, NO digests)
EOF
}

# --- --list: show controllers without exposing digests ---
if [[ "${1:-}" == "--list" ]]; then
    if [[ ! -f "$CONTROLLERS_JSON" ]]; then
        echo "(no controllers.json yet)"
        exit 0
    fi
    python3 - "$CONTROLLERS_JSON" << 'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
for digest, e in data.items():
    print(f"muse={e.get('muse')} pcs={e.get('pcs')} enabled={e.get('enabled')} "
          f"label={e.get('label')} created={e.get('created')} digest={digest[:12]}...")
PYEOF
    exit 0
fi

# --- --add: single extra controller ---
if [[ "${1:-}" == "--add" ]]; then
    shift
    MUSE=""; PCS=""; LABEL=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --muse)  MUSE="$2"; shift 2;;
            --pcs)   PCS="$2";  shift 2;;
            --label) LABEL="$2"; shift 2;;
            *) echo "unknown arg: $1"; usage; exit 1;;
        esac
    done
    [[ -n "$MUSE" && -n "$PCS" ]] || { echo "ERROR: --muse and --pcs required"; usage; exit 1; }
    SPECS=("$MUSE|$PCS|$LABEL")
else
    # Default: the 3 standard tokens
    SPECS=(
        "super|*|Super-admin muse (ser) - all PCs, target mandatory"
        "muse-rd928|RD-928|RD-928 bound muse (1:1)"
        "muse-homepc|homepc|homepc bound muse (1:1)"
    )
fi

[[ -f "$GEN_SCRIPT" ]] || { echo "ERROR: $GEN_SCRIPT not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 required" >&2; exit 1; }

# ---- Backup existing file ----
if [[ -f "$CONTROLLERS_JSON" ]]; then
    cp -p "$CONTROLLERS_JSON" "${CONTROLLERS_JSON}.bak-${BACKUP_SUFFIX}"
    echo "backup: ${CONTROLLERS_JSON}.bak-${BACKUP_SUFFIX}"
fi

# ---- Generate + merge (Python: preserves existing entries, refuses dup muse ids) ----
export CONTROLLERS_JSON
PLAINTEXT_OUT="$(mktemp)"
trap 'rm -f "$PLAINTEXT_OUT"' EXIT

for spec in "${SPECS[@]}"; do
    IFS='|' read -r MUSE PCS LABEL <<< "$spec"
    echo "generating: muse=$MUSE pcs=[$PCS]"
    # gen_token.py prints JSON entry to stdout, plaintext token to stderr
    ENTRY_FILE="$(mktemp)"
    python3 "$GEN_SCRIPT" --muse "$MUSE" --pcs "$PCS" --label "$LABEL" \
        >"$ENTRY_FILE" 2>"$PLAINTEXT_OUT".tmp
    TOKEN="$(grep -oP 'muse_\S+' "$PLAINTEXT_OUT".tmp | head -1)"
    echo "$MUSE :: $TOKEN" >> "$PLAINTEXT_OUT"
    rm -f "$PLAINTEXT_OUT".tmp

    CONTROLLERS_JSON="$CONTROLLERS_JSON" ENTRY_FILE="$ENTRY_FILE" \
    python3 - "$MUSE" << 'PYEOF'
import json, os, sys
muse_id = sys.argv[1]
path = os.environ["CONTROLLERS_JSON"]
with open(os.environ["ENTRY_FILE"]) as f:
    new_entry = json.load(f)
existing = {}
if os.path.exists(path):
    with open(path) as f:
        existing = json.load(f)
for _d, e in existing.items():
    if e.get("muse") == muse_id:
        print(f"ERROR: muse id '{muse_id}' already exists - refusing duplicate", file=sys.stderr)
        sys.exit(1)
existing.update(new_entry)
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(existing, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(f"merged: {muse_id}")
PYEOF
    rm -f "$ENTRY_FILE"
done

chmod 600 "$CONTROLLERS_JSON"
echo ""
echo "wrote: $CONTROLLERS_JSON ($(python3 -c "import json;print(len(json.load(open('$CONTROLLERS_JSON'))))") entries, 600)"
echo ""
echo "======================================================================"
echo " PLAINTEXT TOKENS — DISPLAYED ONCE. Distribute NOW via secure channel."
echo " They can NEVER be recovered from controllers.json."
echo " Do NOT paste in chat / commit to git / write to logs."
echo "======================================================================"
cat "$PLAINTEXT_OUT"
echo "======================================================================"
echo ""
echo "Next:"
echo "  1. Store each token securely (super -> operator vault)"
echo "  2. Hot-reload is automatic (mtime check) - no restart needed"
echo "  3. Verify: see DEPLOY.md section 4"
