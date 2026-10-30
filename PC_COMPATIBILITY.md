# PC 插件侧兼容性验证

**日期**: 2026-10-19
**结论**: **PC 插件无需任何修改** — VPS 鉴权 patch 与 PC 侧零耦合

---

## 一、结论

| 插件 | 版本 | 需改？ | 原因 |
|------|------|--------|------|
| muse-bridge | 1.1.0 | **否** | WSS 客户端逻辑与 `/muse-api/` HTTP 鉴权 patch 无交集 |
| http-proxy-ai | 3.0.0 | **否** | 纯代理插件，零 muse 相关代码（grep 零命中） |

---

## 二、证据

### 2.1 通信路径隔离

```
VM ──HTTPS──> VPS /muse-api/ (/cmd, /batch, /file)   ← patch 只动这里
                  │
                  │ WSS (port 443)
                  ▼
              PC 插件 (muse-bridge-client.js)
```

- PC 插件**只**通过 WSS 长连接与 VPS 通信，从不调用 `/muse-api/` HTTP 接口
- Patch 只修改 `handle_connection()` 中的 HTTP API 分支（line 454+）
- WSS 握手分支（line 386-421）**未被触碰**：仍用 `check_auth(token)` + 全局 `CONFIGURED_TOKEN`

### 2.2 WSS 鉴权未变

PC 插件连接时（`muse-bridge-client.js` line 218-219）：
```js
headers: {
  'X-Bridge-Token': token,  // PC 客户端 token，走 header
}
```

VPS 侧 WSS 握手（`muse-bridge-orig.py` line 387-395）：
```python
token = (
    headers.get("x-bridge-token")
    or query.get("token", [""])[0]
    or query.get("access_token", [""])[0]
)
if not check_auth(token):  # ← 全局 token，不变
```

Patch 的 `verify_controller_token()` 只在 HTTP 分支调用，WSS 分支完全不受影响。

### 2.3 WSS 消息格式未变

VPS → PC 的消息经 `dispatch_request_to_pc()` 发送，patch 未修改此函数：

| 方向 | 格式 | 是否变化 |
|------|------|----------|
| VPS → PC (cmd) | `{"type": "cmd", "command": ..., "timeout": ..., "cwd": ..., "id": ...}` | **不变** |
| VPS → PC (batch) | `{"type": "batch", "commands": [...], "id": ...}` | **不变** |
| VPS → PC (file) | `{"type": "read_file"/"write_file"/..., ...}` | **不变** |
| PC → VPS (hello) | `{"type": "hello", "client": "muse-bridge/1.1.0", "hostname": ..., "platform": ..., "arch": ...}` | **不变** |
| PC → VPS (result) | `{"type": "result"/"file_result"/"batch_result", "id": ..., ...}` | **不变** |

`target` 字段在发送前被 `req_obj.pop("target", None)` 移除（line 315），PC 插件从来看不到 `target`。

### 2.4 HTTP 接口请求/响应格式未变

Patch 在 `/cmd`、`/batch`、`/file` handler 中只做**加法**：

```python
# 新增：8 行权限检查（在 target = ... 之后插入）
if controller_auth[0] != "legacy":
    muse_id, allowed_pcs = controller_auth
    allowed, err = check_target_permission(allowed_pcs, target)
    if not allowed:
        await send_json({"ok": False, "error": f"Forbidden: {err}"}, 403)
        return
    target = resolve_target(allowed_pcs, target)
```

- 请求字段：`command`、`timeout`、`cwd`、`wait`、`target`、`commands`、`op` — **全部不变**
- 响应字段：`ok`、`error`、`id`、`results` — **全部不变**
- 新增的唯一行为：未授权 target 时返回 **403**（新状态码，见 §三风险点）

### 2.5 向后兼容：Legacy 路径

使用旧全局 token 的调用走 Tier 2 回退：
```python
controller_auth = ("legacy", ["*"])  # 完全访问权限，与 patch 前一致
```

- 现有 `musepc`（SSH 方式）继续工作，零影响
- 过渡期新旧可并存

---

## 三、风险点

### 3.1 新 403 状态码（低风险）

**场景**: 使用新 controller token 但请求了未授权的 target

**影响**: HTTP API 调用者会收到 `{"ok": False, "error": "Forbidden: ..."}` + 403

**为什么低风险**:
- PC 插件不调用 HTTP API，收不到 403
- Legacy token（现有 musepc）走回退路径，永不触发 403
- 403 只影响新 token 的越权请求 — 这是**设计意图**，不是 bug

### 3.2 超管 token 必须显式指定 target（设计意图）

**场景**: `pcs=["*"]` 的超管 token 发起请求但未带 `target`

**行为**: 返回 403 `"Super-admin must specify target explicitly (no default)"`

**为什么这样设计**: 防止误发命令到默认机器。超管每次必须明确说要操作哪台 PC。

**PC 插件影响**: 无（PC 插件不发 HTTP 请求）

### 3.3 单 PC 控制器自动解析（行为微调）

**场景**: 控制器绑定 `pcs=["RD-928"]`，请求未带 `target`

**旧行为**: `target=None` → `pick_client(None)`：
- 1 台 PC 在线 → 返回该 PC
- 2+ 台在线 → 409 "Multiple clients online, specify target"

**新行为**: `resolve_target()` 返回 `"RD-928"` → `pick_client("RD-928")`：
- RD-928 在线 → 返回 RD-928（即使其他 PC 也在线）
- RD-928 不在线 → 404

**为什么这是正确的**: 1:1 绑定的语义就是"你只能操作你的 PC"，不应受其他 PC 在线状态影响。

**PC 插件影响**: 无（这是 VPS 侧路由逻辑）

### 3.4 全局 token 轮换（现有行为，未改变）

PC 插件的 WSS 连接仍用全局 `CONFIGURED_TOKEN`。如果未来轮换该 token，PC 插件需要更新配置 — 这是**现有行为**，patch 未改变。

---

## 四、未来如需改 PC 插件，涉及文件

按可能性排序：

| 文件 | 职责 | 何时需要改 |
|------|------|-----------|
| `src/muse/muse-bridge-client.js` (991 行) | WSS 连接、鉴权 header、消息分发 | 改连接方式、改鉴权头、加新消息类型 |
| `src/muse/lane-pipelined-shell.js` | Shell 命令执行通道 | 改命令执行语义 |
| `src/muse/lane-windowed-sftp.js` | 文件操作通道 | 改文件操作语义 |
| `src/muse/permission-manager.js` | 命令权限校验 | 改权限规则 |
| `src/muse/clash-selfheal.js` | Clash 握手拦截自愈 | 改网络环境适配 |
| `src/vscode/bridge-settings.js` | `museBridge.*` 配置项 | 加新配置项 |
| `package.json` | 扩展清单、版本号 | 发新版 |

**当前**: 以上文件**都不需要改**。

---

## 五、验证方法（部署后）

```bash
# 1. PC 插件 WSS 连接不受影响（现有行为）
# 在 RD-928 的 VS Code 输出面板确认 bridge 仍在线

# 2. 新 token 经 HTTPS 调用（走 patch 新路径）
curl -k -X POST https://38.207.167.51/muse-api/cmd \
  -H "Authorization: Bearer muse_<token>" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello", "target": "RD-928"}'
# 预期: {"ok": true, ...} 且 RD-928 执行了命令

# 3. 越权请求被拒绝
curl -k -X POST https://38.207.167.51/muse-api/cmd \
  -H "Authorization: Bearer muse_<rd928-token>" \
  -d '{"command": "echo hi", "target": "homepc"}'
# 预期: 403 Forbidden

# 4. 旧全局 token 仍可用（legacy 回退）
# 现有 musepc (SSH) 继续工作
```

---

**签字**: 兼容性验证通过。PC 侧零改动，可安全部署 VPS patch。
