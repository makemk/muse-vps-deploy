# controllers.json — JSON Schema 文档

`controllers.json` 是 VPS 端 controller token 的唯一真实来源。
key 为 token 明文的 **SHA-256 hex**（64 个小写十六进制字符），value 为条目对象。
明文 token 永不落盘、永不可从库中恢复。

## 顶层结构

```json
{
  "<sha256-hex-digest>": { "<entry>" },
  "<sha256-hex-digest>": { "<entry>" }
}
```

- 顶层：object，key = digest（`^[0-9a-f]{64}$`），value = 条目对象
- 空库合法：`{}`（fail-closed：无 token 可用，全部 401）

## 条目字段

| 字段 | 类型 | 必填 | 说明 | 示例 |
|------|------|------|------|------|
| `muse` | string | ✅ | muse 唯一标识 | `"muse-rd928"` / `"super"` |
| `pcs` | string[] | ✅ | 允许操作的目标 PC；超管固定 `["*"]`，且不可与其他混用 | `["RD-928"]` / `["*"]` |
| `created` | string | ✅ | ISO-8601 UTC 创建时间 | `"2026-10-19T03:05:26.888344+00:00"` |
| `label` | string | ❌ | 人类可读标签（默认 `""`） | `"RD-928 绑定"` |
| `enabled` | boolean | ✅ | 是否有效；`false` → 鉴权直接拒绝 | `true` |
| `allowed_ips` | string[] | ❌ | 源 IP 白名单（CIDR 格式）；缺省或空列表 `[]` = 不限制 | `["203.0.113.10/32", "198.51.100.0/24"]` |
| `grace_until` | string | ❌ | 轮换宽限期（ISO-8601 UTC）；超时后条目自动失效，即使 `enabled` 仍为 true | `"2026-10-20T03:05:26+00:00"` |
| `superseded_by` | string | ❌ | 被哪个 digest 的新 token 取代（digest 前 16 字符 + `…` 仅为可读性，存全量） | `"<64-hex>"` |
| `rotated_from` | string | ❌ | 本条目由哪个旧 digest 轮换而来 | `"<64-hex>"` |
| `revoked_at` | string | ❌ | 吊销时间（ISO-8601 UTC） | `"2026-10-19T04:00:00+00:00"` |
| `revoke_reason` | string | ❌ | 吊销原因（审计） | `"PC 报废"` |

### 字段约束

- `muse`：建议格式 `muse-<slug>`；超管固定为 `"super"`。同一 muse 可有多条（轮换宽限期双活），但**有效**（`is_entry_valid`）的通常只有一条。
- `pcs`：非空数组；`"*"` 出现时数组长度必须为 1（超管独占）。VPS 鉴权：请求 target 必须在 `pcs` 中，或 `pcs == ["*"]`（此时 target 参数必填）。
- `enabled`：缺失时按 `false` 处理（fail-closed）。
- `allowed_ips`：可选字符串数组，元素为 IPv4/IPv6 单 IP 或 CIDR 网段；单 IP 自动识别为 /32（/128），`ip_network("203.0.113.5")` 即 `203.0.113.5/32`（Gemini 4 架构评审 §7.1）。缺省或空列表 `[]` = 不限制源 IP。格式损坏（非列表、含非字符串、非法 CIDR）→ 条目失效（fail-closed）。VPS 鉴权校验时传入请求源 IP，不在范围内返回 403。
- `grace_until`：格式损坏按失效处理（fail-closed）。VPS 鉴权必须实现该语义（见 `common.is_entry_valid`）。

## 完整示例

```json
{
  "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef": {
    "muse": "muse-rd928",
    "pcs": ["RD-928"],
    "created": "2026-10-19T03:05:26.888344+00:00",
    "label": "RD-928 绑定",
    "enabled": true
  },
  "cafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00d": {
    "muse": "super",
    "pcs": ["*"],
    "created": "2026-10-19T03:05:26.938982+00:00",
    "label": "超管 Muse",
    "enabled": true,
    "allowed_ips": ["203.0.113.10/32", "198.51.100.0/24"]
  },
  "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef": {
    "muse": "muse-rd928",
    "pcs": ["RD-928"],
    "created": "2026-10-18T03:05:26.888344+00:00",
    "label": "RD-928 绑定（旧）",
    "enabled": true,
    "grace_until": "2026-10-19T03:05:26.888344+00:00",
    "superseded_by": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
  },
  "cafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00d": {
    "muse": "ssh_fallback_admin",
    "pcs": ["*"],
    "created": "2026-10-19T03:05:26.888344+00:00",
    "label": "SSH 回退通道专用（VPS 本机 musepc）",
    "enabled": true,
    "allowed_ips": ["127.0.0.1/32"]
  }
}
```

## 文件级要求

- 路径（VPS）：`/etc/muse-bridge/controllers.json`
- 权限：`600`，属主 `root`（本工具写入时强制）
- 写入：必须原子（临时文件 + fsync + rename）+ `flock` 互斥，本工具已实现
- 备份：每次变更前建议 `cp controllers.json controllers.json.bak-$(date +%F_%T)`
