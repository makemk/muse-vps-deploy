# 部署脚本（scripts）

VPS 端部署鉴权补丁 + 生成 token 的自动化脚本。

## 文件清单

| 文件 | 说明 |
|------|------|
| `deploy-auth-patch.sh` | 部署 controller-token 鉴权补丁到 `/usr/local/bin/muse-bridge.py` |
| `gen-tokens.sh` | 批量生成 token（超管 + 各 PC），写入 VPS 的 `controllers.json` |
| `DEPLOY.md` | 7 步部署清单：dry-run → 部署 → token → 验证 → 切流 → 回滚 |

## deploy-auth-patch.sh

```bash
# 先 dry-run（不改 VPS）
./deploy-auth-patch.sh --check-only

# 正式部署（SSH 到 VPS 执行）
./deploy-auth-patch.sh

# 回滚到最近备份
./deploy-auth-patch.sh --rollback
```

工作流程：备份（时间戳 `.bak`）→ 精确锚点合并补丁 → `py_compile` 语法检查 → 空 `controllers.json` 兜底（fail-closed，600）→ `systemctl restart muse-bridge`。

**幂等**：二次运行自动跳过已插入的代码。**回滚验证过**：恢复后与原文件逐字节一致。

所有 VPS 路径在脚本顶部变量可覆盖：`BRIDGE_PY`、`PATCH_DIR`、`CONTROLLERS_JSON`、`SERVICE_NAME`。

## gen-tokens.sh

```bash
# 默认生成 3 个：super(pcs=*)、muse-rd928(RD-928)、muse-homepc(homepc)
./gen-tokens.sh

# 追加一个
./gen-tokens.sh --add --muse muse-newpc --pcs NEW-PC --label "新机器"

# 列出（不暴露 digest）
./gen-tokens.sh --list
```

- VPS 只存 SHA-256，明文 token 只在终端显示一次
- `--add` 防重名

## DEPLOY.md

7 步清单，含 VPS 端验证（4a 错 token 401、4b 错 target 403、4c 正确 200、4d 超管无 target 403、4e legacy 兼容、4f WSS 不受影响）→ VM 端验证（无 SSH）→ 切流 → 回滚。

部署前必读。
