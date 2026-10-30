# VPS Deploy — Controller Token Auth for muse-bridge

**Goal**: replace the VM→VPS SSH hop with HTTPS + per-controller tokens.
**Scope**: VPS only (`/usr/local/bin/muse-bridge.py` + `/etc/muse-bridge/controllers.json`).
The `/mail-api/` gateway (agent-mail over VPS) is a separate future phase — not in this deploy.

**Files** (in `/tmp/vps-deploy/`, copy to VPS before running):

| File | Run where | Purpose |
|------|-----------|---------|
| `deploy-auth-patch.sh` | VPS as root | Backup + merge auth patch + syntax check + restart |
| `gen-tokens.sh` | VPS as root | Generate super + muse-rd928 + muse-homepc tokens |
| `DEPLOY.md` | — | This checklist |

**Prerequisites on VPS**:
- `/tmp/bridge-auth-impl/` present (auth_patch.py, dispatch_patch.py)
- `/tmp/token-mgr-proto/gen_token.py` present
- Confirm service unit name: `systemctl list-units | grep -i -E 'bridge|muse'`
  (scripts default to `muse-bridge`; override with `SERVICE_NAME=xxx`)

> **Safety**: SSH stays as fallback during the whole transition.
> Do NOT remove the VPS SSH key until the new path is verified end-to-end.

---

## 1. Dry-run the patch (no changes)

```bash
BRIDGE_PY=/usr/local/bin/muse-bridge.py \
PATCH_DIR=/tmp/bridge-auth-impl \
bash /tmp/vps-deploy/deploy-auth-patch.sh --check-only
```

Expected: 6 "inserted/replaced" lines, `OK: ... compiles`, exit 0.
If any `FATAL`, stop — do not proceed (anchor mismatch = bridge version drift).

## 2. Deploy the patch

```bash
bash /tmp/vps-deploy/deploy-auth-patch.sh
```

What it does:
1. Backup → `/usr/local/bin/muse-bridge.py.bak-<timestamp>`
2. Merge: globals (`CONTROLLERS_PATH`) + 4 functions + two-tier auth block
   + target permission checks in `/cmd`, `/batch`, `/file` handlers
3. `python3 -m py_compile` — aborts on syntax error (backup untouched)
4. Creates empty `/etc/muse-bridge/controllers.json` (600) if missing
   (empty = legacy global token still works; fail-closed for new tokens)
5. `systemctl restart muse-bridge`

Verify the service is back:
```bash
systemctl is-active muse-bridge
curl -sk https://127.0.0.1:443/muse-api/status | head -c 200
```

## 3. Generate controller tokens

```bash
bash /tmp/vps-deploy/gen-tokens.sh
```

Creates `/etc/muse-bridge/controllers.json` (600, root:root) with 3 entries
(SHA-256 digests only — **no plaintext stored**):

| muse id | pcs | Role |
|---------|-----|------|
| `super` | `["*"]` | Super-admin (ser). `target` mandatory on every call. |
| `muse-rd928` | `["RD-928"]` | 1:1 bound. Target auto-resolves if omitted. |
| `muse-homepc` | `["homepc"]` | 1:1 bound. Target auto-resolves if omitted. |

**The plaintext tokens print ONCE to the terminal.** Distribute immediately:
- `super` → operator's secure vault (never in chat/logs/git)
- `muse-rd928` / `muse-homepc` → their respective muses via secure file-drop

Hot-reload is automatic (mtime check) — no bridge restart needed for token changes.
Revoke: set `"enabled": false` on the entry (or use `--add` for new controllers).

```bash
# later: add / list
bash /tmp/vps-deploy/gen-tokens.sh --add --muse muse-pc03 --pcs PC-03 --label "..."
bash /tmp/vps-deploy/gen-tokens.sh --list
```

## 4. Verify (on VPS)

```bash
API=https://127.0.0.1:443/muse-api
```

**4a. Wrong token → 401** (proves auth chain works):
```bash
curl -sk -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer muse_wrong-token-xyz" $API/status
# expect: 401
```

**4b. Controller token, wrong target → 403** (proves 1:1 isolation):
```bash
curl -sk -H "Authorization: Bearer <muse-rd928-token>" \
  -H "Content-Type: application/json" \
  -d '{"target":"homepc","command":"echo hi","wait":true}' \
  $API/cmd
# expect: 403 {"ok":false,"error":"Forbidden: Target 'homepc' not authorized ..."}
```

**4c. Controller token, correct target → 200** (proves dispatch works):
```bash
curl -sk -H "Authorization: Bearer <muse-rd928-token>" \
  -H "Content-Type: application/json" \
  -d '{"target":"RD-928","command":"echo hello-from-token","wait":true}' \
  $API/cmd
# expect: 200 with stdout "hello-from-token" (RD-928 must be online)
```

**4d. Super-admin without target → 403** (proves mandatory-target rule):
```bash
curl -sk -H "Authorization: Bearer <super-token>" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo hi","wait":true}' $API/cmd
# expect: 403 "Super-admin must specify target explicitly"
```

**4e. Legacy global token still works** (transition safety):
```bash
curl -sk -H "X-Bridge-Token: <legacy-token-from-/etc/muse-bridge/token>" \
  $API/status
# expect: 200 (and a "[bridge] Legacy global token used" line in the service log)
```

**4f. PC clients unaffected** (WSS path untouched):
- RD-928 / homepc stay online in `/muse-api/status` → `client_list`
- No 1006 storm in plugin logs

## 5. Verify from the VM (no SSH)

With the super token stored at `~/.config/mused/token` (600) and the
daemon from `/tmp/mused-prod/` running:

```bash
/tmp/mused-prod/museapi --target RD-928 POST /cmd '{"command":"echo hi"}'
# expect: HTTP 200 with RD-928's reply, ~30-60ms on warm connection
```

(Or raw: `curl` through the egress proxy with `SNI=ai.vps38.local` —
see `/tmp/museapi-proto/client.py` for the CONNECT+TLS recipe.)

## 6. Cutover

1. All 4a–4f green, and 5 green from the VM.
2. Point `~/workspace/bin/musepc` at the daemon (or keep both, daemon first).
3. Run normal workload for 24h on the new path.
4. Only then: remove the VPS SSH key (`s
...[truncated 1136 chars]