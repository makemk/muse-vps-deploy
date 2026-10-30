# Token 安全规范

## 1. 明文 Token 生命周期

| 阶段 | 要求 |
|------|------|
| 生成 | `gen_token.py` 明文**只在 stderr 显示一次**；操作者立即复制分发 |
| 分发 | 走加密通道：SSH 会话内复制、或已加密的即时通讯；**禁止**贴到聊天记录、邮件正文、日志、工单系统 |
| 存储（客户端） | VM 端 `~/.config/mused/token`（600）；PC 侧由各绑定 Muse 自行保管。**VPS 端永不存明文** |
| 使用 | 仅走 `Authorization: Bearer` HTTP 头；禁止拼进 URL / query string（会进 nginx access log） |
| 轮换 | 周期见 §4；轮换用 `rotate.py`（宽限期双活，零停机） |
| 吊销 | 疑似泄露、PC 报废、人员变更 → `revoke.py` 立即失效 + 审计 |

**铁律**：明文一旦离开生成终端，就视为"已分发"；本地副本（terminal scrollback、剪贴板、临时文件）立即清理。

## 2. controllers.json 保护

- 路径：`/etc/muse-bridge/controllers.json`，权限 **600**，属主 root
- 写入必须经本工具（原子 + flock）；**禁止手工编辑**（JSON 手滑 = 全站 401）
- 变更前备份：`cp controllers.json controllers.json.bak-$(date +%F_%T)`
- 备份文件同样 600，定期清理（只留最近 7 天）
- 该文件**不进 git、不进备份镜像、不出现在任何聊天/文档**（只存 digest，泄露 digest ≠ 泄露 token，但仍按敏感处理）

## 3. 传输安全

- VM→VPS：HTTPS（TLS 1.2+），校验服务端证书（VPS38 Root CA 或系统 CA）
- Token 只在 `Authorization` 头中传输；nginx 配置 `access_log` 不得记录该头
- 代理环境（CONNECT）下确认 SNI 正确，避免中间人掐 SNI 导致降级

## 4. 轮换周期建议

| Token 类型 | 建议周期 | 说明 |
|------------|----------|------|
| 超管 `super` | 90 天 | 权限最大，泄露 blast radius 最大；轮换需同步更新 VM daemon 配置并重启 |
| 绑定 muse（`muse-*`） | 180 天 | 1:1 绑定，blast radius 限于一台 PC |
| 临时/调试 | 7 天或用后即焚 | 调试完立即 `revoke.py` |

- 轮换默认宽限期 24h（`rotate.py --grace-hours` 可调）；超管建议 48h（给足切换窗口）
- 宽限期结束**无需人工操作**，旧 token 自动失效（`grace_until` 语义）
- 到期前 7 天建议在运维日历提醒，避免"自然过期"造成中断

## 5. 应急：疑似泄露

1. 立即 `revoke.py --digest <前缀> --reason "疑似泄露" --yes`（秒级全网失效）
2. `rotate.py` 生成新 token，走加密通道重新分发
3. 查审计日志（`<db>.audit.log`）确认吊销/轮换记录
4. 查 VPS 鉴权日志：泄露 token 在吊销前有无异常调用（非常见 target、非常见时间）
5. 复盘：泄露路径是什么（聊天记录？日志？），堵住

## 6. 审计

- 每次 `rotate` / `revoke` 自动追加 JSONL 审计记录到 `<db>.audit.log`
- 记录字段：`at`、`action`、`muse`、`digest`（前 16 字符）、`reason`、`by`
- 审计日志 append-only，定期归档到只读存储；至少保留 1 年
