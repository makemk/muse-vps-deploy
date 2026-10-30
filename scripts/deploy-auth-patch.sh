#!/usr/bin/env bash
# =============================================================================
# deploy-auth-patch.sh — Deploy controller-token auth patch to muse-bridge.py
#
# RUN ON THE VPS (as root). Deploys the two-tier auth:
#   Tier 1 (new): per-controller tokens from /etc/muse-bridge/controllers.json
#   Tier 2 (legacy): existing global token (transition period, logged)
#
# Patch source: ~/workspace/muse-vps-deploy/vps-auth-patch/
#   (auth_patch.py + dispatch_patch.py, HARDENED - see SECURITY_REVIEW.md)
# The actual merge is done by an embedded Python script (exact anchor matching)
# which READS the patch code from $PATCH_DIR (single source of truth).
# Override: PATCH_DIR=/path/to/vps-auth-patch bash deploy-auth-patch.sh
#
# Idempotent: re-running skips already-applied insertions (marker check).
#
# Rollback:
#   cp /usr/local/bin/muse-bridge.py.bak-<DATE> /usr/local/bin/muse-bridge.py
#   systemctl restart muse-bridge
#   (or run: bash deploy-auth-patch.sh --rollback)
# =============================================================================
set -euo pipefail

# ---------------- VPS paths (edit here if your layout differs) ----------------
BRIDGE_PY="${BRIDGE_PY:-/usr/local/bin/muse-bridge.py}"
# PATCH_DIR: where the HARDENED patch files live ON THE VPS.
# Copy the hardened files there first:
#   scp -r ~/workspace/muse-vps-deploy/vps-auth-patch/{auth_patch.py,dispatch_patch.py} \
#       root@vps:/tmp/bridge-auth-impl/
PATCH_DIR="${PATCH_DIR:-/tmp/bridge-auth-impl}"
CONTROLLERS_JSON="${CONTROLLERS_JSON:-/etc/muse-bridge/controllers.json}"
SERVICE_NAME="${SERVICE_NAME:-muse-bridge}"   # NOTE: confirm with `systemctl list-units | grep -i bridge`
BACKUP_SUFFIX="$(date +%Y%m%d-%H%M%S)"
# ------------------------------------------------------------------------------

usage() {
    echo "Usage: $0 [--check-only] [--rollback [backup-file]]"
    echo "  --check-only   Verify patch would apply cleanly, make no changes"
    echo "  --rollback     Restore from latest (or given) backup and restart"
}

if [[ "${1:-}" == "--rollback" ]]; then
    BACKUP="${2:-}"
    if [[ -z "$BACKUP" ]]; then
        BACKUP="$(ls -t "${BRIDGE_PY}".bak-* 2>/dev/null | head -1 || true)"
    fi
    if [[ -z "$BACKUP" || ! -f "$BACKUP" ]]; then
        echo "ERROR: no backup found (looked for ${BRIDGE_PY}.bak-*)" >&2
        exit 1
    fi
    echo "Rolling back: $BACKUP -> $BRIDGE_PY"
    cp "$BACKUP" "$BRIDGE_PY"
    python3 -m py_compile "$BRIDGE_PY" && echo "syntax OK"
    systemctl restart "$SERVICE_NAME"
    systemctl is-active "$SERVICE_NAME"
    echo "Rollback complete."
    exit 0
fi

CHECK_ONLY=0
[[ "${1:-}" == "--check-only" ]] && CHECK_ONLY=1

echo "== muse-bridge auth patch deploy =="
echo "target : $BRIDGE_PY"
echo "patch  : $PATCH_DIR"

if [[ ! -f "$BRIDGE_PY" ]]; then
    echo "ERROR: $BRIDGE_PY not found" >&2; exit 1
fi
for f in auth_patch.py dispatch_patch.py; do
    [[ -f "$PATCH_DIR/$f" ]] || { echo "ERROR: $PATCH_DIR/$f missing" >&2; exit 1; }
done

# ---- 0. Idempotency pre-check (BEFORE backup) ----
# If the patch is already applied, exit now so we don't snapshot the
# patched file as a "backup" (which would poison --rollback's latest-backup pick).
if grep -q "CONTROLLERS_PATH" "$BRIDGE_PY" 2>/dev/null; then
    echo "patch already applied (marker CONTROLLERS_PATH found) - nothing to do"
    exit 0
fi

# ---- 1. Backup ----
BACKUP="${BRIDGE_PY}.bak-${BACKUP_SUFFIX}"
if [[ "$CHECK_ONLY" -eq 0 ]]; then
    cp -p "$BRIDGE_PY" "$BACKUP"
    echo "backup : $BACKUP"
else
    echo "(check-only: skipping backup)"
fi

# ---- 2. Merge patch (embedded Python, exact-anchor insertion) ----
# The patch code is read from $PATCH_DIR/auth_patch.py and dispatch_patch.py
# (single source of truth). The script only handles anchors and placement.
export BRIDGE_PY PATCH_DIR CHECK_ONLY
python3 << 'PYEOF'
import os, re, sys

bridge_py = os.environ["BRIDGE_PY"]
patch_dir = os.environ["PATCH_DIR"]
check_only = os.environ["CHECK_ONLY"] == "1"

with open(bridge_py, "r", encoding="utf-8") as f:
    src = f.read()
with open(os.path.join(patch_dir, "auth_patch.py"), "r", encoding="utf-8") as f:
    auth_src = f.read()
with open(os.path.join(patch_dir, "dispatch_patch.py"), "r", encoding="utf-8") as f:
    disp_src = f.read()

MARKER = "CONTROLLERS_PATH"          # present iff patch already applied
if MARKER in src:
    print("patch already applied (marker CONTROLLERS_PATH found) - nothing to do")
    sys.exit(0)

# ---- extract globals block from auth_patch.py ----
# Module-level assignments starting at CONTROLLERS_PATH, up to NEW FUNCTIONS header
g_start = auth_src.index('CONTROLLERS_PATH = ')
g_end = auth_src.index('# NEW FUNCTIONS')
globals_body = auth_src[g_start:g_end].rstrip() + "\n"
assert "_controllers_fail_closed" in globals_body, "globals block missing fail-closed flag"

# ---- extract functions block from auth_patch.py ----
# From first def to the MERGE INSTRUCTIONS header
f_start = auth_src.index('def _log_safe')
f_end = auth_src.index('# MERGE INSTRUCTIONS')
functions_body = auth_src[f_start:f_end].rstrip() + "\n"
for fn in ("load_controllers", "controllers_fail_closed", "extract_bearer_token",
           "verify_controller_token", "check_target_permission", "resolve_target",
           "_log_safe", "_valid_controller_entry"):
    assert f"def {fn}" in functions_body, f"function {fn} missing from auth_patch.py"

# ---- extract NEW auth block from dispatch_patch.py ----
m = re.search(r'# NEW:\n"""(.*?)"""', disp_src, re.S)
assert m, "NEW auth block not found in dispatch_patch.py"
new_auth = m.group(1)
assert "controllers_fail_closed" in new_auth, "NEW auth block missing fail-closed check"
assert "extract_bearer_token" in new_auth, "NEW auth block missing hardened Bearer parsing"
# strip the trailing newline structure: block is indented for in-function use already
if not new_auth.endswith("\n"):
    new_auth += "\n"

# ---- extract 3 INSERT snippets from dispatch_patch.py (order: /cmd, /batch, /file) ----
inserts = re.findall(r'# INSERT:\n"""(.*?)"""', disp_src, re.S)
assert len(inserts) == 3, f"expected 3 INSERT blocks, found {len(inserts)}"
check_snippets = dict(zip(("/cmd", "/batch", "/file"), inserts))
for h, s in check_snippets.items():
    assert "check_target_permission" in s, f"{h} snippet missing permission check"
    assert "_log_safe" in s, f"{h} snippet missing log sanitizer"

# ---- 2a. Globals: insert after the CONFIGURED_TOKEN assignment line ----
# (module-level: line starting with CONFIGURED_TOKEN at column 0;
#  note: file has `CONFIGURED_TOKEN = ""`, real value loaded at runtime)
m = re.search(r'(?m)^CONFIGURED_TOKEN\s*=', src)
if m is None:
    print("FATAL: globals anchor not found", file=sys.stderr)
    sys.exit(1)
eol = src.find("\n", m.end())
globals_block = (
    '\n\n'
    '# ---- controller token auth (added by deploy-auth-patch.sh) ----\n'
    'import re  # hardened auth needs re.fullmatch (no-op if already imported)\n'
    + globals_body
)
src = src[:eol] + globals_block + src[eol:]
print("inserted: globals (CONTROLLERS_PATH + fail-closed flag + import re)")

# ---- 2b. Functions: insert before 'class WSConnection:' ----
anchor_cls = "\nclass WSConnection:"
idx = src.find(anchor_cls)
if idx == -1:
    print("FATAL: class anchor not found: 'class WSConnection:'", file=sys.stderr)
    sys.exit(1)
functions_block = (
    '\n# ---- controller token auth functions (added by deploy-auth-patch.sh) ----\n\n'
    + functions_body
)
src = src[:idx] + "\n" + functions_block + src[idx:]
print("inserted: 8 hardened auth functions before class WSConnection")

# ---- 2c. Two-tier auth block: replace the legacy check ----
old_auth = '''        # Authenticated endpoints
        if not check_auth(token):
            await send_json({"ok": False, "error": "Unauthorized: invalid bridge token"}, 401)
            return
'''
if old_auth not in src:
    print("FATAL: auth block anchor not found", file=sys.stderr)
    sys.exit(1)
if src.count(old_auth) != 1:
    print(f"FATAL: auth block anchor found {src.count(old_auth)}x, expected exactly 1",
          file=sys.stderr)
    sys.exit(1)
src = src.replace(old_auth, new_auth)
print("replaced: two-tier auth block (hardened: fail-closed 503 + Bearer parsing)")

# ---- 2d. Target permission checks in /cmd, /batch, /file handlers ----
# Each handler has: `            target = body_json.get("target", "") or None`
# Distinguish handlers by the `if path == ...` line above each occurrence.
target_line = '            target = body_json.get("target", "") or None\n'
occurrences = []
start = 0
while True:
    i = src.find(target_line, start)
    if i == -1:
        break
    occurrences.append(i)
    start = i + len(target_line)

if len(occurrences) != 3:
    print(f"FATAL: expected 3 target lines, found {len(occurrences)}", file=sys.stderr)
    sys.exit(1)

# Insert from LAST to FIRST so earlier offsets stay valid
for pos in sorted(occurrences, reverse=True):
    # Look backwards (up to 1200 chars) for the nearest handler path marker
    # (/file handler has ~900 chars of op/type_map setup before its target line)
    window = src[max(0, pos - 1200):pos]
    handler, best_pos = None, -1
    for h in ("/cmd", "/batch", "/file"):
        p = window.rfind(f'if path == "{h}"')
        if p > best_pos:
            handler, best_pos = h, p
    if handler is None:
        print(f"FATAL: cannot determine handler for target line at offset {pos}",
              file=sys.stderr)
        sys.exit(1)
    insert_at = pos + len(target_line)
    src = src[:insert_at] + check_snippets[handler] + src[insert_at:]
    print(f"inserted: hardened target permission check in {handler} handler")

if not check_only:
    with open(bridge_py, "w", encoding="utf-8") as f:
        f.write(src)
    print("written: " + bridge_py)
else:
    print("(check-only: not writing)")
PYEOF

# ---- 3. Syntax check ----
echo "== syntax check =="
python3 -m py_compile "$BRIDGE_PY" && echo "OK: $BRIDGE_PY compiles"

if [[ "$CHECK_ONLY" -eq 1 ]]; then
    echo "(check-only: stopping before restart)"
    exit 0
fi

# ---- 4. Ensure controllers.json exists (fail-closed: empty = legacy only) ----
if [[ ! -f "$CONTROLLERS_JSON" ]]; then
    echo "NOTE: $CONTROLLERS_JSON not found - creating empty (legacy token still works)"
    mkdir -p "$(dirname "$CONTROLLERS_JSON")"
    echo '{}' > "$CONTROLLERS_JSON"
    chmod 600 "$CONTROLLERS_JSON"
else
    echo "controllers.json exists: $CONTROLLERS_JSON"
fi
# Enforce 600 regardless
chmod 600 "$CONTROLLERS_JSON" || true

# ---- 5. Restart service ----
echo "== restarting $SERVICE_NAME =="
# NOTE: confirm the unit name first:  systemctl list-units | grep -i -E 'bridge|muse'
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active "$SERVICE_NAME"

echo ""
echo "== deploy complete =="
echo "backup: $BACKUP"
echo "verify:"
echo "  curl -sk -H 'Authorization: Bearer wrong' https://127.0.0.1:443/muse-api/status"
echo "    -> expect 401 Unauthorized"
echo "Rollback:"
echo "  $0 --rollback            # restores latest backup"
echo "  $0 --rollback <file>     # restores given backup"
