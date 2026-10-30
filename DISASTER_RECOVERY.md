# 备份与灾难恢复 — 去 SSH 架构

**范围**：VPS（muse-bridge + nginx + PKI）和 VM（mused daemon）两端。
PC 插件端（VS Code 扩展）不在本方案内——插件从 vsix 重装即可，无状态。

**核心认知**（先读这三条，后面全是它们的推论）：

1. **token 明文只在生成瞬间存在**。VPS 的 `controllers.json` 只存 SHA-256，
   丢了明文 = 所有 token 作废，必须重新生成并逐个分发到各端。
   → token 明文备份是整个 DR 方案里**唯一不可再生的资产**。
2. **VPS 是无状态中转**（除了 controllers.json）。muse-bridge.py 本身无状态，
   重启不丢数据；WSS 连接断了客户端会自动重连。
   → VPS 恢复 = 装文件 + 起服务，**不需要**数据迁移。
3. **VM daemon 也是无状态的**。配置 + token 文件就是它的全部状态。
   → VM 恢复 = 从本仓库重装 + 恢复两个文件。

---

## 1. 备份清单

### 1.1 VPS 端

| # | 路径 | 内容 | 备份方式 | 频率 |
|---|------|------|----------|------|
| V1 | `/usr/local/bin/muse-bridge.py` | 命令转发服务（含鉴权 patch） | 文件拷贝 + md5 | 每次变更后 |
| V2 | `/usr/local/bin/muse-bridge.py.bak-*` | 部署时的自动备份 | 随 V1 一起 | 每次变更后 |
| V3 | `/etc/muse-bridge/controllers.json` | **token 摘要库**（600） | 文件拷贝 | **每次 token 变更后立即** |
| V4 | `/etc/nginx/sites-enabled/`（或 `conf.d/`）中含 `muse-api` 的 server 块 | nginx 反代配置 | 文件拷贝 | 每次变更后 |
| V5 | `/etc/ssl/certs/vps38-server.pem` | 服务器证书（公钥，可公开） | 文件拷贝 | 每次换证后 |
| V6 | `/etc/ssl/private/vps38-server.key` | 服务器私钥（**绝密**） | 加密备份 | 每次换证后 |
| V7 | `/etc/ssl/certs/vps38-root-ca.pem` | 自建 Root CA 公钥 | 文件拷贝 | 一次（10 年有效期） |
| V8 | `/etc/ssl/private/vps38-root-ca.key` | Root CA 私钥（**绝密中的绝密**） | 加密备份，离线 | 一次 |

> V6/V8 是私钥：备份必须加密（age 或 openssl enc），明文私钥**永不**进 git、
> 永不进聊天、永不放云盘。建议：加密后存 VM 本地 + 用户 U 盘各一份。

### 1.2 VM 端

| # | 路径 | 内容 | 备份方式 | 频率 |
|---|------|------|----------|------|
| M1 | `~/.config/mused/config.json` | daemon 配置（600） | 文件拷贝 | 每次变更后 |
| M2 | `~/.config/mused/token` | **控制器 token 明文**（600，**不可再生**） | 加密备份 | 生成/轮换后立即 |
| M3 | `~/.config/systemd/user/mused.service` | systemd unit | 文件拷贝 | 每次变更后 |
| M4 | `~/.local/bin/mused` | daemon 可执行文件 | 从本仓库重装即可（`mused/mused.py`） | 不需单独备份 |
| M5 | `~/workspace/muse-vps-deploy/` | 本仓库（所有源码+脚本+文档） | git（已有） | 持续 |

### 1.3 token 明文备份（单独强调）

`gen-tokens.sh` 生成 token 时，明文只在终端显示一次。
**生成当场**必须做两件事：

1. 把明文写入加密文件：`~/.config/mused/tokens.vault.age`
   （或 openssl 加密，见 `backup.sh --encrypt-tokens`）
2. 把各 token 分发到对应端（super → 本 VM 的 `~/.config/mused/token`；
   各 PC 的 token → 通过安全通道给用户，由用户填进插件配置）

**没有明文备份 = 丢 controllers.json 后全网 token 作废重发。**
有明文备份 = 直接恢复 controllers.json（重新算 SHA-256 写入即可）。

### 1.4 保留期与存储

| 备份 | 位置 | 保留期 |
|------|------|--------|
| VPS 每日全量（V1–V5） | VPS 本地 `/var/backups/muse-bridge/` + 同步到 VM `~/backups/vps/` | 30 天滚动 |
| controllers.json（V3） | 每次变更即时备份，VM `~/backups/vps/controllers.json.<时间戳>` | 90 天 |
| token 明文加密包（M2） | VM `~/.config/mused/tokens.vault.age` + 用户 U 盘 | 永久（直到轮换） |
| 私钥加密包（V6/V8） | VM 加密 + 用户 U 盘离线 | 永久 |

---

## 2. 灾难场景与恢复步骤

### 场景 A：VPS 挂了（系统盘损坏 / 重装系统）

**RTO：2 小时。RPO：24 小时**（V1–V5 每日备份；V3 即时备份故为 0）。

```
1. 新 VPS 装好 Debian，配好 SSH（vps38 别名）
2. 装依赖：python3、nginx（apt）
3. 恢复文件（从 VM ~/backups/vps/<最新日期>/）：
     /usr/local/bin/muse-bridge.py
     /etc/muse-bridge/controllers.json   (600, root:root)
     nginx muse-api 站点配置
     /etc/ssl/certs/vps38-server.pem + /etc/ssl/private/vps38-server.key
     /etc/ssl/certs/vps38-root-ca.pem
4. systemctl enable/start muse-bridge（服务名按 RUNBOOK §0.2 确认）
5. 验证（RUNBOOK §4 的 6 个场景：401/403/200/超管无target/legacy/WSS）
6. VM 端 mused 自动重连（heartbeat 指数退避，上限 30s，无需人工干预）
   PC 插件自动重连 WSS（插件自带重连）
```

> 如果备份里的 muse-bridge.py 版本太旧：用本仓库 `vps-auth-patch/` 重新打 patch
>（`scripts/deploy-auth-patch.sh`），再恢复 controllers.json 即可。
> patch 与数据是解耦的——这是双层设计的好处。

### 场景 B：controllers.json 丢了 / 损坏

**分两种情况**，天壤之别：

**B1：有 token 明文备份**（正常情况）→ **RTO：15 分钟。RPO：0。**

```bash
# 在 VPS 上：
python3 - <<'EOF'
import hashlib, json
# 从加密备份解密出明文 token 列表后：
tokens = {"super": "<明文>", "muse-rd928": "<明文>", "muse-homepc": "<明文>"}
pcs_map = {"super": ["*"], "muse-rd928": ["RD-928"], "muse-homepc": ["homepc"]}
db = {}
for name, plain in tokens.items():
    digest = hashlib.sha256(plain.encode()).hexdigest()
    db[digest] = {"muse": name, "pcs": pcs_map[name],
                  "created": "<原created或now>", "label": name, "enabled": True}
json.dump(db, open("/etc/muse-bridge/controllers.json", "w"), indent=2)
EOF
chmod 600 /etc/muse-bridge/controllers.json
systemctl restart muse-bridge
```

**B2：无明文备份**（最坏情况）→ **RTO：1 小时。RPO：全部 token。**

1. `scripts/gen-tokens.sh` 重新生成 3 个 token（super/RD-928/homepc）
2. 当场做加密备份（§1.3）
3. super token → 写入本 VM `~/.config/mused/token`，重启 mused
4. RD-928/homepc 的 token → 通过安全通道发给用户，用户填进插件配置并 Reload
5. 验证 6 场景

> **教训**：B2 的 1 小时里有一半是等人（用户填 token）。所以 §1.3 不是建议，是强制。

### 场景 C：VM 挂了（本机重装 / 换机器）

**RTO：30 分钟。RPO：0**（配置和 token 都有备份）。

```
1. 装好系统，恢复 ~/workspace（git clone 或从备份拷）
2. 恢复 ~/.config/mused/config.json 和 ~/.config/mused/token（600）
3. 运行本仓库 mused/install.sh（装 daemon + systemd unit）
4. systemctl --user start mused
5. museapi --target RD-928 "echo ok" 验证
```

> token 明文备份在，VM 恢复不需要动 VPS 任何东西。

### 场景 D：VPS 被入侵（最严重）

**RTO：4 小时。RPO：0**（假设备份未被污染——用入侵**前**的备份）。

```
1. 立即：VPS 断网（服务商控制台关机/防火墙全 drop）
2. 在新机器（或重装后的原机器）上：
   a. 用入侵前的备份恢复 muse-bridge.py + nginx 配置
   b. 吊销所有旧 token：gen-tokens.sh 重新生成 3 个（旧摘要自然失效，
      因为 controllers.json 里只会有新摘要）
   c. 换证书：用离线 Root CA 私钥签发新服务器证书（旧证书作废）
   d. 审计：查 nginx access log 找异常 IP/异常 target，
      查 controllers.json 的 enabled 字段有无被篡改
3. 分发新 token（同 B2 步骤 3–4）
4. 换掉 VPS 的 SSH key（入侵可能偷了 authorized_keys）
5. 恢复服务，验证 6 场景
6. 事后：写事件报告（入侵点、影响范围、修复）
```

> **为什么换证书也要做**：攻击者可能拿到了服务器私钥（V6），
> 就可以中间人冒充 VPS。Root CA 私钥（V8）离线保存就是防这一手——
> 只要 V8 没泄露，签新证书只需要 5 分钟。

### 场景 E：误删 / 改错配置（最常见）

```
- muse-bridge.py 改坏了：deploy-auth-patch.sh --rollback（自动恢复最近 .bak）
- controllers.json 改坏了：~/backups/vps/controllers.json.<时间戳> 拷回去，restart
- nginx 改坏了：nginx -t 先测，坏了就恢复备份，reload
- mused config 改坏了：恢复 ~/.config/mused/config.json，restart
```

> 所有"改文件"操作的前置动作都是**先备份**（脚本里已内置）。

---

## 3. 一键备份脚本

`backup.sh`（在 `~/workspace/muse-vps-deploy/` 下）：

```bash
./backup.sh            # 每日全量（VPS V1–V5 + VM M1/M3，不含私钥明文）
./backup.sh --full     # 含私钥加密包（V6/V8，需输加密口令，每次换证后跑一次）
./backup.sh --tokens   # 只备份 controllers.json（token 变更后立即跑）
```

- VPS 部分经 `ssh -F /home/hatch/.ssh/config vps38` 拉取。
- 私钥/口令**永不**出现在命令行历史（用 `read -s` 交互输入）。
- 备份落盘：`~/backups/vps/<YYYYMMDD-HHMMSS>/` + `latest` 软链接。
- 30 天滚动清理（`--full` 的私钥包不清理）。

## 4. 一键恢复脚本

`restore.sh`：

```bash
./restore.sh --list                    # 列出可用备份
./restore.sh --date 20261019-030000    # 从指定备份恢复（dry-run 先打清单）
./restore.sh --date 20261019-030000 --apply   # 真正执行
./restore.sh --controllers-only       # 只恢复 controllers.json（场景 B1）
```

- 默认 dry-run：先打印"要恢复哪些文件到哪里"，`--apply` 才动手。
- 恢复 controllers.json 后自动 `chmod 600` + 提示 restart。
- **不恢复私钥**（`--full` 备份里的私钥包需要手动解密，这是故意的——
  防止脚本误操作把私钥铺到错误位置）。

---

## 5. RTO / RPO 汇总

| 场景 | RTO | RPO | 备注 |
|------|-----|-----|------|
| A VPS 重装 | 2h | 24h（V3 为 0） | 瓶颈是装系统+配网络 |
| B1 controllers.json 丢（有明文备份） | 15min | 0 | 纯 VPS 操作 |
| B2 controllers.json 丢（无明文备份） | 1h | 全部 token | 一半时间在等人填 token |
| C VM 重装 | 30min | 0 | 不碰 VPS |
| D VPS 被入侵 | 4h | 0 | 含换证+换 SSH key+审计 |
| E 误删改错 | 5min | 0 | 脚本自带回滚 |

**当前短板**：B2。缓解措施只有一条——§1.3 的 token 明文备份做成铁律，
`gen-tokens.sh` 生成后不做加密备份就不算"生成完成"。

---

## 6. 定期演练

| 演练 | 频率 | 内容 |
|------|------|------|
| 备份有效性 | 每月 | 从 `latest` 备份解压，校验 md5 + controllers.json 可解析 |
| B1 恢复 | 每季度 | 在测试环境（mock）跑一遍 restore.sh --controllers-only |
| 私钥解密 | 每半年 | 确认 V6/V8 加密包能解密（口令还记得） |

> 没演练过的备份 = 没备份。第一次演练就定在**部署完成后第一周**。
