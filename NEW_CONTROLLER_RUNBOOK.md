# 新 Muse + 新 PC 接入 Runbook（独立 controller token 隔离）

日期：2026-10-21。用户决策：走 B（独立 token 隔离再接）。
状态：**VPS 侧已执行完成（2026-10-21 09:42–09:47 CST，用户已批准 SSH）**，见文末执行记录；
**2026-10-21 10:53 接入全部闭环**（PC 上线 + echo 端到端通过 + 新 Muse 活跃），见 §4 末。
新 PC 主机名：**DESKTOP-HC5R77C**；controller id：**muse-newpc**，digest `873c0bc350077103…`。
剩余：无（隧道基线纳管由巡检按指纹自动完成）。

## 0. 前提事实（已核对，2026-10-21）

- 线上 VPS `muse-bridge.py` **已含** 4-controller 鉴权（2026-10-19 部署，OPT-04 事故 diff 实证），
  本次**不改服务端代码**，只往 `/etc/muse-bridge/controllers.json` 追加一条。
  ⚠️ 铁律（OPT-04 教训）：严禁拿本地旧快照整体覆盖线上文件；本次根本不碰 .py。
- 线上 schema（`vps-auth-patch/auth_patch.py` v2 为准，勿用 muse-bridge-auth/ 的简化重建版）：
  `{"<sha256hex>": {"muse": "<id>", "pcs": ["<PC主机名>"], "created": "...", "label": "...", "enabled": true}}`
  可选 `allowed_ips`（CIDR 列表）。VPS 只存 SHA-256，明文永不落盘。
- 授权语义（`check_target_permission`）：普通 controller 的 target 必须 ∈ pcs（大小写不敏感），
  越权 403（通用文案，不泄露清单）；单 PC controller 不带 target 时自动解析到自己那台；
  super（pcs=["*"]）必须显式带 target，无 target 403。
- 鉴权双层、两分支一致（2026-10-21 查线上代码实证）：HTTP API 与 WSS 握手均为
  `verify_controller_token` 优先、legacy `check_auth` 兜底。WSS 曾只认 legacy（controller token 握手 401），
  2026-10-19 已修复部署（握手实测 101）。**新机对的插件与控制器共用 muse-newpc token**，legacy 无需下发。

## 1. 变量（执行前由用户确认）

- `<NEWPC>`：新 PC 的 Windows 主机名（`hostname` 的实际值，pcs 填它）
- `<ALIAS>`：新 PC 短别名（controller id 用 `muse-<ALIAS>`，如 muse-rd928 式命名）

## 2. 执行步骤

### Step 1 — VPS 生成 token 并追加条目（SSH 管理通道，用户已批准后执行）
```bash
# 在 VPS 上（材料已在 /root/muse-vps-deploy/，本机副本 ~/workspace/muse-vps-deploy/token-manager/）
cd /root/muse-vps-deploy/token-manager   # 若不存在，先同步本机副本过去
python3 gen_token.py --muse muse-<ALIAS> --pcs <NEWPC> --label "<ALIAS> 1:1 绑定" \
    --output /etc/muse-bridge/controllers.json
# 明文 token 只在 stderr 出现一次 → 当场进入 Step 2 的安全分发，禁止记入聊天/记忆/日志
cp /etc/muse-bridge/controllers.json /etc/muse-bridge/controllers.json.bak-$(date +%Y%m%d-%H%M%S)  # 先备份再改（顺序按脚本实际行为核对）
chmod 600 /etc/muse-bridge/controllers.json
python3 -c "import json; d=json.load(open('/etc/muse-bridge/controllers.json')); print(len(d))"  # 条数应 = 旧数+1
```
注：load_controllers 按 mtime 热加载（_controllers_cache），预期免重启；
若 Step 4 冒烟出现新 token 401，再 `systemctl restart muse-bridge` 后重测。

### Step 2 — token 分发（明文只走文件投递，绝不进聊天）
- 新 Muse 侧：明文写入其 VM 的 `~/.config/mused/token`（600）。
  交付方式按新 Muse 的实际位置定（用户提供通道后在此补记具体路径）。
- 新 PC 插件侧：与控制器共用同一个 muse-newpc token（见 §0），在 PC 上用
  VS Code 命令 `Bridge: 设置 Token` 录入（存 SecretStorage，不写 settings 明文）。

### Step 3 — 新 Muse 侧配置（其 VM 上）
- 安装 mused/museapi/musepc（架构仓 `vm/` 现成材料 + systemd unit 或 nohup）。
- `~/.config/mused/config.json`：`vps_host=38.207.167.51, vps_port=443, sni=ai.vps38.local`，
  `ca_cert_path` 指向插件仓 `src/muse/vps-root-ca.pem`，`socket_path=/tmp/mused.sock`，
  `compress_enabled=false, microbatch_enabled=false`（与现网一致）。
- 默认 target 可不设（单 PC controller 自动解析）；设 `MUSEPC_TARGET=<NEWPC>` 更直观，二选一。

### Step 4 — 新 PC 侧
- VS Code + Git Bash → 装 `muse-bridge-1.3.0.vsix`（/home/hatch/local_download/）。
- settings：`museBridge.enabled=true`（其余默认：vpsHost 38.207.167.51:443、SNI、内置 CA）。
- `Bridge: 设置 Token` 录入 muse-newpc token → Reload Window → 状态栏在线。

### Step 5 — 冒烟（HTTPS only，照 2026-10-19 Step 2 的 7 场景裁剪）
1. 新 controller token → 自己 PC `/cmd echo` → **200**
2. 新 controller token → target=RD-928 → **403**（隔离证明，本次核心验收）
3. 新 controller token 不带 target → 自动落到自己 PC → **200**
4. 错 token → **401**
5. super（本机 mused）→ target=<NEWPC> → **200**（超管协调通道保留）
6. `GET /status`：client_list 同时含 RD-928 与 <NEWPC>，hello 均为 muse-bridge/1.3.0

### Step 6 — 收尾
- 隧道监控基线 `~/workspace/logs/tunnel-baseline.json` 加入新 PC 源 IP 指纹（上线后观察其 :443 常驻连接数）。
- 记忆登记：<ALIAS> = 新 PC、controller id、绑定关系（不记任何 token 值）。

## 3. 回滚
删除 controllers.json 中新条目（按 sha256 键）或恢复 Step 1 的 .bak 备份；
热加载/重启后新 token 立即失效，其余 controller 不受影响。PC 插件与 legacy 通道全程不动。

## 4. 执行记录（2026-10-21，超管 Muse 经 SSH 执行，用户已批准）

- 预检：VPS `muse-bridge` service active；controllers.json 原 4 条（super/muse-rd928/muse-homepc/ssh_fallback_admin）。
- 生成：`gen_token.py --muse muse-newpc --pcs NEWPC-TBD --output /etc/muse-bridge/controllers.json`，
  备份 `controllers.json.bak-20261021-014241`；明文只落 VPS `/root/newpc-controller.token`(600) 中转，
  随后经 scp 直取进交接包（内容全程未打印），取毕已 shred VPS 上的明文与临时脚本。
- 绑定：用户提供主机名后 pcs 改为 `["DESKTOP-HC5R77C"]`（用 token-manager 的 load_db/save_db_atomic，
  备份 `controllers.json.bak-20261021-014324-bind`），digest 不变 `873c0bc350077103…`，共 5 条，其余未动。
- 冒烟（VPS 本机 curl + SNI 解析，两轮：占位名与真实名各一轮，结论一致）：
  ① 新 token 打 RD-928 → **403 Forbidden**（隔离核心证据）；
  ② 不带 target → 自动解析到 DESKTOP-HC5R77C → "not online"（PC 未上线，符合预期）；
  ③ 显式 target=DESKTOP-HC5R77C → 同上（授权通过、路由未达）；
  ④ 错 token → **401**；⑤ 新 token GET /status → 200。热加载生效，全程未重启服务。
- 交接包：`/home/hatch/local_download/new-muse-onboarding.7z`（手册+现行工具链+token+模板+vsix），
  由用户转交新 Muse。**未放入 legacy token**（新机对不需要它，见 §0 双层鉴权）。
  【2026-10-21 修正】用户追问后查线上 WSS 握手代码实证插件可直接用 controller token，
  交接包 README 已按此改正并重打（新 SHA-256 见记忆条目）。
- 待续：新 Muse 回执（mused 起来 + /status 自检）、PC 装插件上线后端到端 echo 冒烟、
  隧道监控基线加入 DESKTOP-HC5R77C 的源 IP 指纹。
- 10:53 闭环：DESKTOP-HC5R77C 于 10:53:44 hello 上线（muse-bridge/1.3.0），超管 musepc echo
  端到端通过（exit 0，回主机名）；出口 IP 113.116.74.10（广东电信，与 09:38 起观察名单的
  2 条 :443 未归属 IP 吻合，隧道巡检按指纹自动纳管）；新 PC/新 Muse 上线巡检 cron 已删。
  新 Muse 侧 10:55:41 确认活跃（VPS 日志 `muse-newpc -> DESKTOP-HC5R77C` 的 cmd 已成功路由）。
  401 错值的确切来源无最终实证（对面称一直用的包内 token 文件；重录 + 重新启用 Bridge 后即上线——
  "401 后重连循环停止、必须手动重启用"这点是实锤）。

## 5. 插件侧 token 录入铁律（2026-10-21 401 事故教训）

2026-10-21 10:46–10:53，DESKTOP-HC5R77C 插件握手 HTTP 401
（`握手被拒绝:HTTP401` → "鉴权失败，已停止重连循环"）。三路取证闭环后定位：
包内 token 哈希与 VPS digest 全 64 位一致、VPS 条目健康、服务端鉴权代码复核——
包没错、VPS 没错，错的是插件实际发出的值。修法：对面从 token 文件经剪贴板重录
`Bridge: 设置 Token` 并手动重新启用 Bridge，10:53:44 上线。铁律如下（下次接入直接照抄）：

1. 插件 1.3.0 **没有 settings 里的 token 键**：token 唯一去处是 VS Code SecretStorage
   （键 `muse_bridge.token`，与旧版沿用），唯一录入路径是命令面板 `Bridge: 设置 Token` 的密码框。
   往 settings.json 写任何 token 键名都会被彻底忽略；凭据文件只认 `muse_sec_` 前缀，
   controller token 放文件里也会被无视。（1.4.2 起例外：服务端可在超管请求下经
   `set_token` 控制帧远程写入新 token，见 §7——那是机器对机器的第二条写入路径；
   人工录入仍只有命令面板这一条，本节铁律不变。）
2. 录入框按 **Esc 是静默取消、无任何提示**——"以为重设了、其实没录进去"完全可能。
3. **空 token 时插件直接停摆、绝不连接、绝不报 401**；报 401 即证明插件手里握着一个错值在发。
4. **绝不从 `info/controller-entry.json` 抄任何字符串进 token 框**：那是服务端的 sha256 摘要，
   不是 token，录进去必 401。token 只认包内 `token/mused-token.txt`。
5. 校验标准：48 字符、以 `muse_` 开头；本地对文件首尾几位即可，**明文绝不进聊天/日志/记忆/截图**。
6. 401 语义只有一种："递上来的 token 服务端不认识"（错一字、漏一字、混入空格/BOM 皆 401）。
   403 才是无权限；握手阶段**不校验主机名绑定**，别往"PC 名对不上"方向查。
7. 401 后插件"已停止重连循环"——**必须手动重新启用 Bridge**（状态栏菜单）或 Reload Window，
   它不会自己再试。
8. 401 路径按设计**不写服务端日志**（nginx access_log 关）——"日志里没拒绝记录"不能证明连接没到 VPS；
   能收到 HTTP 401 本身就说明请求已到达应用层。

标准录入动作（可直接转交对面）：
```powershell
(Get-Content <包解压目录>\token\mused-token.txt -Raw).Trim() | Set-Clipboard
```
→ VS Code 命令 `Bridge: 设置 Token` → 粘贴、回车（别按 Esc）→ 重新启用 Bridge → 等上线。

另：交接包 README-FIRST 的"401 = token 文件不对/没读到"是 mused 侧的写法，对插件侧有误，
下次打包时按上文重写"出问题时"一节。

## 6. 已知风险：交接包外流 = 该对控制权外流（2026-10-21 记录，用户决定暂缓处理）

交接包内含 controller token 明文 + 现成工具（mused/musepc）+ 说明书，是一套开箱即用的控制权：

- **服务端只认 token 不认人**：条目未设 `allowed_ips`、不查来源 IP、不验设备。任何人拿到包，在目标 PC 在线时即可用该 token 经 HTTPS API 下发任意命令；日志一律记为该 controller 名，与正牌使用者不可区分。
- **影响面隔离在一台**：token 的 `pcs` 绑定决定其只能打自己那台 PC，打其他 PC 一律 403；碰不到 VPS 本身（无 SSH）、碰不到其他 controller 与 legacy 全局权限。即最坏情况 = 客户自己的机器被接管（通道经本 VPS）。
- **现有兜底**：吊销是秒级的——删/禁 controllers.json 中该 digest 即全网失效，再单独换发新 token 给正牌 Muse（插件重录 + mused token 文件替换后删旧条目）。

缓解选项（提出于 2026-10-21，用户 11:14 决定先不管、只记录，后续如需处理再启用）：

1. 维持现状靠吊销兜底（当前采用）。
2. 给条目加 `allowed_ips`（Muse 侧出口 + PC 侧出口）——两端 IP 均不固定，换 IP 先误伤正牌，不推荐。
3. 双凭证改造（治本，需改服务端鉴权）：插件用只能上线、不能下发的 client 凭证；下发 token 只交付 Muse 本人、不进交接包。此后包外流最多冒充上线，下不了命令。

## 8. 张德阳新 PC 双凭证拆分（2026-10-23 执行，用户当次指令）

背景：DESKTOP-HC5R77C 原为「1:1 绑定」——插件与张德阳的 Muse 共用 muse-newpc 一条 token。用户令给两边都下发新 token，借机拆成双凭证（与 RD-928 同构）：

1. 插件侧：`muse-rotate-pc-token DESKTOP-HC5R77C` 远程轮换 confirmed（2.9s 级）：插件换专属绑定 digest `e38f5b946fa6…`（label `PC binding credential (remote-rotated)`），旧共享 digest `873c0bc35007…` 在确认后退役。明文只存在于该 PC 的 SecretStorage。
2. 控制器侧：本地生成新 controller token，digest `f4737a6b7a21…` 注册为第二条 muse-newpc 条目（pcs 仍仅 DESKTOP-HC5R77C，label `controller credential for Zhang Deyang Muse (split 2026-10-23)`）。controllers 总数 6→7。
3. 交接：明文只进交接包 `/home/hatch/local_download/zhangdeyang-new-controller-token.7z`（1,157B，BLAKE3 `26c77b5c…`，含 README-FIRST.md + token/mused-token.txt）；待用户转交张德阳的 Muse，对方替换其 mused token 文件并重启后生效；换上之前其 controller 通道为 401（预期窗口）。
4. 验证（直连 HTTPS，绕开本机 mused）：新 controller 打 DESKTOP-HC5R77C → 200；打 RD-928 → 403；打 homepc → 403；旧共享 token → 401。**旧交接包 new-muse-onboarding.7z 内的 token 已死，§6 的包外流风险就此关闭。**

⚠️ 运维注意：rotate 端点的退役步删除「同 muse id 的全部旧 digest」。该 PC 的插件绑定与 controller 同属 muse-newpc——**今后对该 PC 再执行远程轮换，会连 controller digest 一并退役，轮换后必须按本节同法重发 controller token**。

⚠️ 测试方法注意：经本机 mused 用 museapi --token 测 controller 权限**不作数**——本机 mused 的 WSS mux 用守护进程自身 token（super）上行、服务端按连接身份鉴权；权限冒烟一律直连 HTTPS（Authorization: Bearer），或在对方自己的 mused 上测。

## 9. 桥 1.5.0 写代码四件套（2026-10-23 上线）

- 插件 1.5.0（RD-928 已装并 hello 实测；GitHub push 待用户批准，张德阳 PC 在 push 后会自升）：批量执行帧 batch v2（serial/parallel/dag + dependsOn + summary + timing）、build 帧（PC 侧解析 gcc/MSVC 诊断只回结构化结果，原始日志落 PC 的 `.muse-logs/`）、vs_state 帧（未保存缓冲/活动编辑器/Problems 诊断）、edit 通道扩展（backup 到 `.muse-backups/`、expect_hash 乐观锁、want_diff 回 unified diff；expect_hash 带参时引擎绕 MVCC 缓存现读磁盘）。
- 服务端：新增 POST /muse-api/dispatch {target, frame, timeout}（帧白名单 batch/build/vs_state，set_token 等保留帧 400 拒绝；鉴权与 /cmd 同构；超时钳制 [5,3600]、batch 缺省按步数推导）；dispatch_request_to_pc 成功路径注入 `_srv.pc_roundtrip_ms`。线上备份 /root/muse-bridge.py.bak-20261023-150；补丁存档 ~/workspace/bridge-150/。
- VM 侧工具：`~/workspace/bin/musedev {batch|build|vsstate|edit} <target> <spec.json>`（经 mused 走 /dispatch 或 /file）。
- 延时口径（2026-10-23 真机）：单条 echo 的服务端↔PC 往返 ~229ms = 插件侧总处理 ~63ms（pre 1 + exec 62）+ WSS 传输/服务端 ~166ms；6 命令批量一次往返中位 0.53s vs 串行 6 次 2.54s（≈4.8×）。

## 10. 1.5.1 加固与增量回传（2026-10-23，服务端已部署）

- 服务端：非对象 JSON body 统一 400；batch_progress 收集（按 id+连接身份，2000 步/4MB 上限）；/dispatch 超时响应带 partial.completed；迟到结果暂存 600s，GET /muse-api/result?id=<mb_id> 取回（在途回 running+partial，未知 404）。备份 /root/muse-bridge.py.bak-20261023-151。
- 插件 1.5.1（未打包，用户令停放）：ld 三段名解析、batch 步输出 encodeOutputField+256KB 上限、batch_progress 逐步直发。打包安装后增量回传在真机生效；此前线上 batch 超时仍全丢（服务端 partial 为空）。

## 11. 桥 1.6.0 七域 + 事件 / 1.6.1 修复（2026-10-23 上线，RD-928 已装 1.6.1）

- 插件（全帧经 /dispatch、帧内 op 分发、写类 op 在 readOnly 桥被拒）：debug（state/start/stop/breakpoints/add_bp/remove_bp/threads/stack/scopes/variables/evaluate/继续/暂停/单步，DAP 经 session.customRequest，行列 1-based↔0-based 在胶水层换算）、lang（definition/references/hover/doc_symbols/ws_symbols/rename{code_actions/apply_code_action 带 expect_title 防漂移}）、tasks（list/run 等 onDidEndTaskProcess）、term（list/exec 走 shell integration，输出过 encodeOutputField+256KB）、present（reveal/notify/progress 为读、confirm 为写类——模态不进只读桥）、git（status/diff/log/stage；status 含 merge 桶）、config（get/update）。
- 事件：插件 vs_event 单向帧（EventRing+合并窗，设置 museBridge.eventsEnabled/eventCoalesceMs）；服务端按连接身份归属存环（1000 条/2MB、seq 回退清环），GET /muse-api/events?target=&since=&limit= 拉取；musedev 加 debug/lang/tasks/term/present/git/config/events 子命令（tasks run/term exec 缺省超时 615/315 自动注入）。
- 语义注意：config update 改 museBridge.* 自家键会触发桥自重启，回包显示断开、约 8s 自愈、值已写入（与设置 UI 手改同效）。
- 重载口径（2026-10-23 实测钉死）：**杀 extension-host 进程不会换新版**（窗口按启动时的扩展清单重拉宿主）；换版必须整窗 Reload 或重启 Code 进程。自动重载可用法：`code --status` 取 extension-host PID 仅供定位；整机重启 Code 用分离助手（杀全部 Code → 轮询进程排空 ≤40s → 再拉起 Code.exe 带工作区），实测桥约 24s 回来。
- 1.6.1 真机修复：① remove_bp 与 git 仓库匹配的路径比较改大小写不敏感（fsPath 盘符恒小写 vs 入参大写盘符，`===` 永假——remove_bp 恒 0 的根因）；② lang 提供者调用前预打开文档（未打开文档 rename 抛 Unexpected type）。
- 真机验收（2026-10-23）：七域全通、事件拉回 29 条 seq 连续 dropped 0；基线 echo 中位 0.334s / batch6 0.438s（不劣于 1.5.1）；term 热 exec 往返 219ms（pc_exec 29ms）、冷首跑 1424ms（建终端成本）。未验：真调试会话深读（ADuCM430_ThreadX 的 ELF 未在 PC 构建，start 被干净拒绝）、present confirm 点选（需用户在键盘前）。服务端备份 /root/muse-bridge.py.bak-20261023-160；设计与测试存档 ~/workspace/bridge-160/；本地套件 183/0/1。
- GitHub push 仍待批准（插件仓将含 1.5.0→1.6.1 全部；张德阳 PC 在 push 后自升）。
- 1.6.2（2026-10-23，真机复验）：输出编码信封绕过上限修复——`_encodeOutputFieldCapped` 加 capEnvelope 口径，仅 batch 步输出与 term exec 两车道启用：信封 {enc,data} 的 data 超 256KB 时回落原文截断明文 + droppedBytes 计量；/cmd 主车道不经本函数、信封行为不变。同 400KB 随机 base64 探针复验：明文恰 262,144B、truncated/outputTruncated true、droppedBytes 137,866。套件 184/0/1（新增 F14 中等可压缩用例）。上限口径经用户确认维持 256KB（保险丝+契约，非配额；可压缩内容经信封仍可回 MB 级原文）。

## 12. 桥 1.7.0 五新帧 + 多窗口 / 1.7.1 label 修复（2026-10-23 上线，RD-928 双窗口实证）

- **1.7.0**：command{exec,list}/editor{edit,save,close}/verify/window{list,open,close}/decor{set,clear,clear_all} 五帧 + lang call_hierarchy（depth≤3、flatten≤500）/format + git commit/checkout/branches/blame（porcelain 解析、未提交行零 sha）+ debug memory_read/write。多窗口：hello 带 instanceId（客户端发 snake_case `instance_id`，服务端摄入点别名归一）；服务端实例注册表 host→[连接]，hello 只顶替同 instanceId；target `HOST#前缀`（大小写不敏感、多命中 multiple、无命中 not_found）；事件环按实例分键，裸 host 落主实例环，注销维护主环指针（B2）。全帧经 /dispatch；classify：五帧全写类（只读桥拒），call_hierarchy/branches/blame/memory_read 读类，format/commit/checkout/memory_write 写类。
- **打包事故（定版教训）**：1.7.0 首包手工 `vsce package --no-dependencies` 把 ws 依赖漏在包外，插件 require('ws') 失败、桥永不连接，用户手卸。此后打包只走 `~/workspace/muse-bridge/package-vsix.sh`（不传该 flag + 验包 ws 在内 + version 一致 + 只留最新 + BLAKE3）。
- **审查必修已修**：B1 command 事件留痕改胶水 noteVsEvent 发（带 seq 与 ok；客户端直发无 seq 会被服务端丢弃）；B3 verify 的 command 构建异步拒绝不再吞（ran:false + error，真机实证）；S1 musedev verify 默认 timeout 注入 675。
- **真机实证（RD-928，服务端已部署 1.7.0，备份 /root/muse-bridge.py.bak-20261023-170）**：五帧逐 op 全通；command 事件入环（seq+ok）；git 四项在临时仓全通（blame 含 Not Committed Yet 行）；call_hierarchy 真树（main→System_Hardware_Init→WdtGo 跨文件）；多窗口全周期（开 ADuCM430_ThreadX 窗→注册→#前缀路由/大写前缀/坏前缀报错→close 注销）；**真调试会话**：cortex-debug 起会话（JLink SWD）→ memory_read 读到向量表、memory_write 4B→读回逐字一致 → stop 干净。性能门全过且优于 1.6.2：echo 中位 252ms（基线 334）、batch6 266ms（438）、term 热 ~207ms（219）。
- **1.7.1**：修 tasks label——VS Code 运行时 Task 以 `.name` 暴露 tasks.json 的 label，此前只读 label/definition.label，工作区任务 list 全空、run 按 label 找不到（1.6 时代潜伏，靠提供者任务的 definition.label 侥幸未爆）。四处提取点（taskLabelOf/onTasks/events 两处）已补 `.name`。套件 197/0/1；vsix BLAKE3 `567690c0…` 在 local_download。
- **语义备忘**：editor close 不落盘，未存改动走 VS Code hot-exit（重开恢复）；format 落缓冲区、需显式 save；window.open 对不存在的路径回 {opened:true} 但 VS Code 静默不开窗（命令已发、无法感知）；verify 无 build 时回存盘+诊断。（原记「tasks run 的 EIDE build 首开会卡、非桥缺陷」已于当日下午更正，见 §12 补记——实为开窗方式 + 桥等待匹配双重问题。）
- **PC 现场**：ADuCM430_ThreadX 工程已同步至 RD-928 `vscode_env\ADuCM430\ADuCM430_ThreadX`（原 PC 上没有此工程）并保持开窗；测试残留 probe170.json/两 vsix/screen_test.png 已进回收站；probe-repo 与 ADuCM430 内的 aducm430_threadx.7z 因句柄占用没删掉（无害小文件，待用户顺手删或下次清理）。

### §12 补记：1.7.2/1.7.3 tasks 等待匹配修复 + EIDE 开窗口径（2026-10-23 下午，RD-928 真机复验全过）

- **EIDE 开窗唯一正道（用户纠正）**：EIDE 工程必须打开工程根目录的 `.code-workspace` 文件进 VS Code，不要直接开文件夹——开文件夹时 EIDE 不认工程上下文，build 任务终端能开但 unify_builder 根本不启动（无 compiler.log/objs.db）。用户工作流：点 EIDE 插件 → 选工程的 .code-workspace → 桥接上开工。桥侧等价操作 = `code <工程>\<名>.code-workspace`。**窗口纪律（2026-10-23 用户令）：不开新窗口**——一律在主窗口内复用打开（`code -r <工程>.code-workspace`，窗口原地切换到工程），干完同法切回 vscode_env；多窗口路由仅在用户明确要求时用。
- **1.7.2（根因一）**：tasks run/verify 等结束事件原用对象身份（`e.execution.task === task`、事后 `earlyEnd.execution === exec`），VS Code 不保证同一对象→事件永不命中、干等到超时（ISG build 3.4s 已 exitCode 0 结束、事件在环里，调用方却挂满 270s 被中间层掐断）。改按 label 匹配：模块级 `sameTaskOf`（label 非空相等 + definition.type 两边有值须相等），waitTaskEnd 与 onTasks 内联两处同构共用；事件先到的 earlyEnd 缓冲不再要求身份一致。mused 同波修：长等待后 conn.sock 被 http.client 置 None，finally 里 settimeout 触碰抛 AttributeError——不仅盖住真错误，还绕过坏连接回收把脏连接留池；三处触碰加 None 守卫（备份 mused.bak-20261023-172，已重启 mused 生效）。
- **1.7.3（根因二）**：EIDE 执行实例名带 `:<目标名>` 后缀——fetchTasks 的 `build` vs 结束事件的 `build:D8P1OPDR8-X1`，label 全等仍匹配不到。sameTaskOf 命中条件放宽为全等 || 一方为另一方+':'+后缀；type 约束不变；`buildx` 无冒号不误配（T7 实测）。审查收尾：onTasks 订阅异常出口与 waitTaskEnd 对齐、删 earlyEnd 死字段、补 T8（反方向）/T9（后缀命中但 type 不同不误配）；已知口径（注释明示）：execution 已定后 label 通道仍兜底、同族任务并发结束可能串话，earlyEnd 无新鲜度过滤是根治挂死的接受代价。双路独立审查无必修。套件 206/0/1。
- **真机复验（1.7.3，RD-928）**：ISG 窗（D8P1OPDR8-X1.code-workspace）tasks run build → 2.9s 回 `{exitCode:0}`；ThreadX 窗 tasks run build → 4.2s 回 `{exitCode:1}`（真失败：`.bss` 溢出 DRAM 8192B，40KB/32KB，工程链接脚本账、非桥问题）；verify{build:task build}（ISG）→ 1.7s 回 `{saved:[],build:{ran:true,exitCode:0},diagnostics:{}}`。decor 缺口同日闭环（见 §12 主文验收外补测：两组 set + clear_all 回执正常；截图视觉确认因用户前台 Chrome 抢不到焦点未做，以回执为准）。vsix 1.7.3（457,243B，BLAKE3 `6f89f654…`）在 local_download；存档 extension.173.js / mused.173.py 在 ~/workspace/bridge-170/。复验后测试窗口已全关、PC 仅剩 vscode_env 主窗。
- **editor edit 坐标形状备忘（2026-10-23 复测发现）**：DESIGN 正本是扁平 `{line,col,endLine,endCol}`，胶水照此实现且字节级验对（替换 2..5 → 结果精确）。1.7.0 验收存档的 ed_edit.json 误用了嵌套 `{start:{...},end:{...}}` 形状——字段读不到被 `|0` 静默当 0,0 插入文件头，当年未做字节校验被 format 步骤掩盖；spec 已更正。隐患：缺字段的 edit 不报错、静默在 0,0 插入，建议后续给 edit 加坐标必填校验（缺失回 error）。
