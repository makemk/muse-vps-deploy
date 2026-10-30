# VPS Controller Token Auth - Implementation Summary

## Deliverables (in /tmp/bridge-auth-impl/)

| File | Description |
|------|-------------|
| `muse-bridge-orig.py` | Original 637-line file from VPS (reference, DO NOT deploy) |
| `auth_patch.py` | New: `load_controllers()`, `verify_controller_token()`, `check_target_permission()`, `resolve_target()` + globals. 17/17 unit tests pass. |
| `dispatch_patch.py` | Patch instructions for `handle_connection()`: 2-tier auth + per-handler target checks for /cmd, /batch, /file. |
| `IMPLEMENTATION.md` | This file. |

## Changes Required (to /usr/local/bin/muse-bridge.py)

**Total: ~80 lines added, 0 lines removed, 5 insertion points.**

1. **Globals** (+4 lines): `CONTROLLERS_PATH`, `_controllers_cache`, `_controllers_mtime` after line 49.
2. **Functions** (+~70 lines): 4 new functions after `check_auth()` (line 88).
3. **Auth block** (~line 454): Replace single `check_auth()` with 2-tier (controller token → legacy fallback).
4. **/cmd handler** (~line 477): Insert 8-line permission check after `target = ...`.
5. **/batch handler** (~line 507): Insert 8-line permission check after `target = ...`.
6. **/file handler** (~line 545): Insert 8-line permission check after `target = ...`.

**No new imports needed** - `hashlib`, `hmac`, `json`, `os`, `sys` all already imported.

## Structural Issues Found in Original Code

### 1. WSS and HTTP share auth but shouldn't (by design, not a bug)
- Line 392 (WSS): `check_auth(token)` - for PC clients
- Line 456 (HTTP): `check_auth(token)` - for controllers
- **Issue**: Same token for both. If a PC is compromised, its token can be used as a controller.
- **Fix in patch**: Controllers use `controllers.json`, PCs keep global token. Two credential spaces.

### 2. `dispatch_request_to_pc` pops target (line 315)
```python
target = req_obj.pop("target", None) or None
```
- The `target` is removed from the request object before forwarding to PC.
- **Impact**: Our patch must resolve/check target BEFORE calling this function (which we do - checks are in the handler, before dispatch).
- **Not a bug**, just a constraint on patch placement.

### 3. Three handlers duplicate target→client logic (lines 477-503, 507-533, 545-571)
- `/cmd`, `/batch`, `/file` each have ~25 lines of identical `pick_client` + error handling.
- **Issue**: Our permission check must be added in 3 places (not DRY).
- **Recommendation**: Future refactor to extract `authorize_and_pick_client()` helper. Not done in this patch to minimize change surface.

### 4. No request ID in wait=false path for /cmd (line 493)
- `/cmd` wait=false generates `req_id = f"cmd_..."` but `/batch` uses `bat_` and `/file` uses `fil_`.
- **Not a bug**, just inconsistent prefixes. Our patch doesn't touch this.

### 5. `check_auth` strips token (line 88)
```python
return hmac.compare_digest(CONFIGURED_TOKEN, provided_token.strip())
```
- Our `verify_controller_token` also strips. Consistent behavior.

## Security Properties

| Property | Implementation |
|----------|----------------|
| Timing-safe compare | `hmac.compare_digest` for token digests |
| No plaintext storage | Only SHA-256 hex in controllers.json |
| Hot reload | mtime check, no restart needed for token changes |
| Fail-closed | Missing/corrupt controllers.json → empty (deny new, allow legacy) |
| Audit trail | `print(f"[bridge] {muse_id} -> {target}...")` to stderr |
| Super-admin safety | Target mandatory, no silent default |

## Testing

- `auth_patch.py`: 17/17 unit tests pass (token verify, permission checks, target resolution)
- `dispatch_patch.py`: Syntax valid (it's documentation, not executable)
- **Not tested**: Integration with actual bridge (requires VPS deployment)

## Deployment Checklist (for parent agent)

- [ ] Backup `/usr/local/bin/muse-bridge.py`
- [ ] Apply auth_patch.py (globals + functions)
- [ ] Apply dispatch_patch.py (5 insertion points)
- [ ] `python3 -m py_compile` to verify syntax
- [ ] Create `/etc/muse-bridge/controllers.json` (600, root:root) with super-admin token
- [ ] `systemctl restart muse-bridge` (or equivalent)
- [ ] Test with super-admin token: `curl -H "Authorization: Bearer muse_..." https://.../muse-api/status`
- [ ] Test 403: normal token with wrong target
- [ ] Keep SSH as fallback during transition
