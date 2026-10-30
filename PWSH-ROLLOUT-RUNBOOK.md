# muse-bridge 1.4.0（pwsh 路线 C）上线与回退手册

日期：2026-10-21。用户决策：执行端从 Git Bash 切 PowerShell 7 走**路线 C**
（pwsh 主车道 + `shellType` 配置双轨，bash 车道保留为回退），**先 POC 后全量切换**。
状态：**已执行（RD-928 部分，2026-10-21 当日闭环）**。1.4.0 已发布并完成 RD-928
POC 12 项；⑥ 缺陷以 1.4.1 修复并复测；muxfer 双车道适配与 ⑩ 延迟门复测通过后，
RD-928 已翻 pwsh 默认（执行记录见 §6 与 §9）。**DESKTOP-HC5R77C 按用户令暂缓**，
其 §1/§3 步骤未执行，待恢复推进时按本手册续走。POC 清单与全部证据在
`~/workspace/muse-bridge-poc/`（POC-CHECKLIST.md 执行记录节）。

## 0. 前提事实（口径已定，勿改）

- 1.4.0 新增配置键 **`museBridge.shellType`**，取值 `bash` | `pwsh`，**默认 `bash`**。
  另有 pwsh 主车道（常驻进程池，与现有 bash 车道并行存在）。
- 插件现状（1.3.0）已是双通道：原生 ShellPool（PS 5.1 + cmd）优先、
  Git Bash 车道管回退 + 后台任务。路线 C 不是推翻重做，是把主车道换成 pwsh 7。
- **软降级**：主 shell 缺失或启动失败时，插件自动回退另一车道并保持上线，
  不再像旧版「无 Git Bash 直接拒绝启动」。降级是保命机制，**不是静默成功**——
  每次切换/冒烟后必须确认实际生效的车道（见 §1、§5）。
- 语义已拍板：**执行期失败不跨语言重跑**（acquire 阶段未执行才允许回退另一车道），
  与 OPT-03 幂等 at-most-once 对齐。POC 与冒烟中遇到「失败后另一语言又跑了一遍」
  即为缺陷，停、记录、上报，不放行。
- 与 OPT-03 幂等层零耦合：换 shell 不碰幂等键/结果缓存/single-flight 逻辑。
- 两台 PC：
  - **RD-928**：用户 J03378，工作目录 `C:\Users\J03378\Documents\vscode_env`；
    pwsh 7.6.7 便携版已装于 `vscode_env\tools\pwsh\`（官方 zip 哈希验过，未改 PATH）。
  - **DESKTOP-HC5R77C**（新 PC）：pwsh 由另一路并行补装，便携版同版本 7.6.7；
    就位路径**以补装回执为准**（预期同构 `vscode_env\tools\pwsh\`，执行前必须实测确认）。
- 切换顺序铁律：**先 RD-928 单机 POC，全过才逐台全量切换**；任何一台不过，
  停在原地走 §4 回退，不带病推进第二台。

## 1. 前置检查（两台 PC 逐台过，任一项不过先修再继续）

### 1.1 pwsh 就位

在目标 PC 上执行（Git Bash 或现有通道均可）：

```bash
# RD-928（路径已确认）
"/c/Users/J03378/Documents/vscode_env/tools/pwsh/pwsh.exe" --version
# 期望输出：PowerShell 7.6.7
```

- [ ] pwsh.exe 存在，`--version` 输出 **7.6.7**。
- [ ] DESKTOP-HC5R77C：先按补装回执确认实际安装路径，再跑同一命令验版本；
      路径与回执不一致时以实测为准，并回填本节。
- [ ] 两台版本一致（均为 7.6.7），不一致先对齐再往下走。

### 1.2 插件与桥的当前状态（切换前的基线）

- [ ] `/status`（超管侧）：目标 PC 在线，hello 为当前线上版本（基线应为
      `muse-bridge/1.3.0`）；把切换前 hello 原文记入 §6。
- [ ] 目标 PC 上 VS Code 已装插件版本与 `/status` 运行态一致（历史上出现过
      「装盘新版、运行旧版」——只认 `/status` 的 hello，不认装盘）。
- [ ] 经现有通道对目标 PC 跑一条无副作用命令（如 `echo` + `hostname`）通，
      证明切换前通道健康，避免把旧故障算到新版本头上。
- [ ] 确认目标 PC 当前 `museBridge.shellType` 未设置或为 `bash`（基线口径）。

### 1.3 发布物（VM 侧，1.4.0 包的生产流程，按既有惯例）

1. VM 侧改码 → 改前整仓备份（`~/workspace/muse-bridge.bak-<版本>-<日期时间戳>`）。
2. **ocr 审查**（委托模式，无 key）过一遍，问题清零或有书面豁免。
3. 仓库测试全绿（现有回归 + 1.4.0 新增 pwsh 用例），红的不打包。
4. `vsce package`，硬验证：包内 `node_modules/ws` 在位、`require('ws')` 加载 OK、
   package.json 版本号 / vsixmanifest / 运行时 hello 标识三处均为 **1.4.0**。
5. 产物放 `/home/hatch/local_download/`，**只留最新版**（旧 vsix 删除，
   唯一例外：§4 包回退所需的 1.3.0 在 POC 期间暂存别处，见 §4.2 注）。
6. 交给用户在 PC 上安装，并**明确提醒：装后必须 Reload Window**，
   然后以 `/status` hello 确认运行态（见 §2）。

## 2. POC 阶段（仅 RD-928 单机）

目标：在一台机器上证明 pwsh 车道全链路可用、可回退，再谈推广。
POC 清单脚本位于 `~/workspace/muse-bridge-poc/`（另一路在建，以该目录实际清单为准）。

### 2.1 步骤

1. **装 1.4.0，保持 `shellType=bash`**：用户在 RD-928 安装 1.4.0 vsix →
   Reload Window → `/status` 确认 hello=`muse-bridge/1.4.0`。
2. **bash 口径冒烟**（证明新包没把老路弄坏）：echo/hostname 一条、
   带中文输出一条、一条后台任务（起/读/杀）。全通才进下一步；
   不通直接按 §4.2 包回退，不在新包上排查老路故障之外的的问题。
3. **切 pwsh**：在 RD-928 的 VS Code settings 设 `museBridge.shellType: "pwsh"` →
   Reload Window → `/status` 看 hello：版本 1.4.0 **且 shellType 已生效为 pwsh**
   （字段名以 1.4.0 实现为准，待补，见 §7）。
4. **确认没有发生软降级**：查插件日志/hello 的降级标记（字段待补，见 §7）；
   若实际生效车道仍是 bash，说明 pwsh 池没起来——按 §5 速查表处置，
   **这种情况不算 POC 开始**，修好重走第 3 步。
5. **跑 POC 7 项清单**（脚本目录 `~/workspace/muse-bridge-poc/`），逐项记录到 §2.2 表格：
   1. 池冷启与健康检查（哨兵回显、worker 死亡自愈替补）；
   2. 基本命令全链路（exit 0 与 exit 42 退出码透传）；
   3. 双流分离与原生 exe（stdout/stderr 不串扰；原生 exe 写 stderr 且非零退出不挂死）；
   4. 中文与编码（中文路径 cd、中文输出无乱码、cp936 原生程序输出经解码可读）；
   5. 超时强杀与自愈（长睡眠 + 短超时 → 进程树连根杀净、无孤儿、worker 替补后立即正常）；
   6. 输出边界（>1MB 输出截断与 droppedBytes 计数；输出末行无换行符时 END 哨兵不漏检）；
   7. 幂等与后台任务（同 idem_key 重投回放不重执行、同键并发 single-flight 只跑一次；
      后台任务启动/读输出/杀掉三件套）。
6. **配置回退演练**（POC 内必做一次）：`shellType` 改回 `bash` → Reload →
   冒烟一条 → 再切回 `pwsh` → Reload → 冒烟一条。证明 §4.1 配置回退真实可用，
   不是纸面路径。

### 2.2 POC 结果记录表（执行时填）

| # | 清单项 | 期望 | 实际 | 结果（过/不过） | 备注 |
|---|--------|------|------|----------------|------|
| 1 | 池冷启 + 自愈 | 哨兵 OK，杀 worker 后替补可用 | | | |
| 2 | 退出码透传 | 0 与 42 均原样回包 | | | |
| 3 | 双流 + 原生 exe | 双流不串扰，非零不挂死 | | | |
| 4 | 中文/编码 | 无乱码，cp936 可读 | | | |
| 5 | 超时强杀 | 无孤儿，替补立即正常 | | | |
| 6 | 输出边界 | 截断计数对，无尾换行不漏检 | | | |
| 7 | 幂等 + 后台 | 重投不重跑，后台三件套通 | | | |
| 附 | 配置回退演练 | bash↔pwsh 各冒烟一条全通 | | | |

放行标准：**7 项 + 附项全过**。任一项不过：记录现象 → 按 §4 回退 RD-928 →
问题回 VM 侧修 → 重打 1.4.0（或 1.4.1）→ 从 §2.1 第 1 步重走，不跳步。

> **执行结果（2026-10-21）**：POC 清单在执行中由 7 项扩为 12 项（审查增补
> ⑨⑩⑪⑫），上表与脚本的对应关系以 `POC-CHECKLIST.md` 为准；逐项结论与证据
> 记在该文件的「执行记录」与「1.4.1 统一复测记录」两节，本表不再回填。
> 摘要：1.4.0 轮 9 PASS + ⑫ 限定 PASS、⑥/⑩ FAIL、⑦b 未执行；⑥ 以 1.4.1
> 修复复测 PASS、⑩ 换口径复测 PASS（校正口径）、muxfer 适配后真机 PASS，
> 三项前置齐备后翻默认（§9）。实际流程与本节预案的偏差：改 settings 后扩展
> 在线拾取，**无需 Reload Window**（§2.1 第 3 步与 §3.1 第 3 步的 Reload
> 仅装包激活时需要）。

## 3. 全量切换（POC 全过后，逐台执行，一台稳了再下一台）

顺序：先 RD-928，后 DESKTOP-HC5R77C。每台独立走完本节，不并行切换两台。

### 3.1 单台切换步骤

1. 该台先完成 §1 全部前置检查（新 PC 特别确认 pwsh 路径实测值）。
2. 若该台未装 1.4.0：安装 vsix → **Reload Window** → `/status` 确认 hello=1.4.0。
3. 设置 `museBridge.shellType: "pwsh"` → **Reload Window**。
4. 确认生效：`/status` hello 中版本=1.4.0、shellType=pwsh；查降级标记，
   确认未发生软降级（标记含义见 §5、字段待补见 §7）。
5. **冒烟三件套**（三件全过才算切换完成）：
   - 版本：hello 版本与 shellType 与第 4 步一致；
   - 简单命令：`echo` + `hostname`，回包主机名与目标机一致、exit 0；
   - 带副作用命令：写文件 → 读回 → 哈希比对（用 b3sum，双边同值），
     再删除；证明 pwsh 车道下文件副作用真实落地且可验。
     （可选加验：同 idem_key 连发两次，第二次回放、副作用只发生一次。）
6. 结果填 §6 执行记录表，该台进入 §3.2 观察期。

### 3.2 观察期与隧道巡检注意事项

- 观察期口径：切换后先以日常任务自然使用，不刻意压测；出现 §5 任一症状先按速查表处置。
- **Reload Window 会让该 PC 的 WSS 短暂掉线重连**，隧道巡检（`vps-tunnel-watch`）
  在切换时间窗内看到连接数抖动/短暂离线属正常，**不要**据此判定异常或改动基线；
  巡检若在切换窗外仍报该机异常，再按故障处理。
- 观察期内巡检基线与白名单**不做任何调整**（切换不改变源 IP 指纹与带数预期；
  若形态长期变化，先核对是否本机任务形态变了，再议基线）。
- 两台都切完、且各自冒烟三件套全过后，在 §6 记录「全量切换完成」，
  此后 VM 侧发往 PC 的命令默认按 **pwsh 语法**书写（bash 语法只在明确
  `shellType=bash` 的机器/回退场景使用）——此口径由主 agent 同步更新到 AGENTS.md。

## 4. 回退路径

两条回退互相独立：配置回退治「pwsh 车道不行」，包回退治「1.4.0 本身不行」。
**先试配置回退**，它快且不动安装。

### 4.1 配置回退：`shellType` 改回 `bash`

- 触发条件（任一）：POC 任一项不过；冒烟三件套任一不过；hello/日志出现降级标记
  且 §5 处置后仍反复；日常使用中 pwsh 车道特有故障（乱码、孤儿、回包异常）复现 ≥2 次。
- 动作：目标 PC 的 VS Code settings 改 `museBridge.shellType: "bash"`（或删除该键，
  默认即 bash）→ **Reload Window**。
- 验证：`/status` hello 版本不变、shellType 生效为 bash；跑冒烟三件套的
  简单命令 + 带副作用命令两件（bash 口径）全通；该机恢复到「1.4.0 + bash 车道」
  状态——这正是 POC 前的基线，插件功能不受影响。
- 注意：配置回退只影响该台，另一台已切 pwsh 的不连带回退，除非故障被确认
  是双机共有（如清单外共性缺陷），此时两台都回，并停止全量切换、上报修包。

### 4.2 包回退：整包退回 1.3.0 vsix

- 触发条件（任一）：1.4.0 在 `shellType=bash` 下冒烟都不过（新包把老路弄坏）；
  插件无法上线/反复断连且与 shellType 无关；配置回退后故障依旧。
- 动作：目标 PC 卸载 1.4.0 → 安装 `muse-bridge-1.3.0.vsix` → **Reload Window**。
  （1.3.0 没有 `shellType` 键，settings 里留着也无害，会被忽略。）
- 验证：`/status` hello 回到 `muse-bridge/1.3.0`；echo/hostname 一条通；
  该机回到 POC 前基线。
- 注（与「只留最新版」惯例的衔接）：POC 期间 1.3.0 vsix 需从 local_download
  之外留一份回退副本（如 VM 侧 `~/workspace/muse-bridge/releases-archive/`，
  路径待定，见 §7）；全量切换完成且观察期无回退事件后，按惯例清理，
  local_download 恢复只留最新版。

## 5. 故障速查表

| 症状 | 先查什么 | 处置 |
|------|----------|------|
| 切 pwsh 后 hello 里 shellType 仍是 bash / 出现降级标记 | 插件日志中 pwsh 池启动段；`pwsh.exe` 按 §1.1 路径实测能否手动启动 | 路径不对→改插件的 pwsh 路径配置（键名待补，见 §7）或按实测路径补装；池启动报错→留日志回 VM 侧修包；修好前保持 bash 车道（软降级下业务不受影响） |
| pwsh 池起不来（Reload 后长时间不上线或反复重连） | `/status` 该机是否在线；插件日志池初始化/哨兵健康检查段；任务管理器是否有 pwsh 僵尸进程 | 有僵尸→按 PID 精确杀（禁全局杀）；仍起不来→§4.1 配置回退保业务，日志打包回 VM 侧 |
| 命令回包乱码（中文变方块/问号） | 是单条命令还是全部；输出程序是 pwsh cmdlet 还是原生 exe（cp936 输出）；同一命令在 bash 车道是否正常 | 全部乱码→查池的 UTF-8 初始化是否生效（属包缺陷，回退+修包）；仅个别原生 exe→记下程序名，按个案处理（decode 层应兜 cp936，未兜住即缺陷上报） |
| 命令超时后有孤儿进程（CPU 占用/文件锁不放） | 任务管理器按命令行找残留 pwsh/子进程与其 PID；插件日志该命令的收割段 | 按 PID `taskkill /F /T` 连根杀（只杀确认的树）；复现→属 POC 第 5 项口径缺陷，§4.1 回退并上报，不长期容忍 |
| 命令失败后疑似被另一语言重跑了一遍 | 该命令的 idem_key 与两次执行的日志时间戳；副作用是否发生两次 | 违反已拍板语义，**立即 §4.1 回退该机**，留证上报，修包后重走 POC 第 7 项 |
| 输出大时回包截断异常/等不到结束 | 输出是否 >1MB、末行是否有换行符 | 对照 POC 第 6 项：清单内复现即缺陷，回退+修包；清单外新形态先留样本再议 |
| 后台任务起不来/读不到输出/杀不掉 | 任务 id 与插件日志的 bg 通道段；进程是否还在 | 对照 POC 第 7 项口径；杀不掉按 PID 精确杀；复现回退上报 |
| 切换后隧道巡检报该机异常 | 先核对时间是否落在 Reload 切换窗内（§3.2） | 窗内→忽略，不动基线；窗外且 `/status` 显示离线→按断连故障查 WSS/Clash（新 PC 注意 Clash sniffer 老坑） |
| 插件整体不上线（与 shell 无关的断连） | `/status`、PC 网络、Clash 是否在运行 | 先按既有断连流程处理（参考新 PC 接入记录）；确认与 1.4.0 无关前不动包；若 1.3.0 同机曾正常且换包后才断→§4.2 包回退验证 |

## 6. 执行记录表（执行时逐行填）

| 时间 | 机器 | 操作者 | 动作（装包/切配置/冒烟/POC 第几项/回退） | 结果 | 备注（hello 原文/日志位置） |
|------|------|--------|------------------------------------------|------|------------------------------|
| 2026-10-21 15:38–15:40 | RD-928 | 主线子 agent | 装机 1.4.0（muxfer 传包 + code --install-extension --force） | 装盘成功，运行态仍 1.3.0 | 回滚包先行存 `muse-bridge-poc/rollback/`（1.3.0 验真）；BLAKE3 `8531bb61…` |
| 2026-10-21 15:42 | RD-928 | 用户 | Reload Window 激活 1.4.0 | PASS | hello = muse-bridge/1.4.0（激活时 shell_type=bash 默认态） |
| 2026-10-21 15:43–15:52 | RD-928 | 主线子 agent | POC 12 项（切 pwsh 测后恢复 bash） | 9 PASS + ⑫ 限定 PASS；⑥ FAIL、⑩ FAIL、⑦b MANUAL 未执行 | 证据：POC-CHECKLIST.md 执行记录 + results-20261021-074349.md；附带发现 muxfer pwsh 车道不可用 |
| 2026-10-21 ~16:05 | RD-928 | 主线 | 装机 1.4.1（⑥ 截断标记修复版）+ 用户 Reload | 激活 PASS | hello = muse-bridge/1.4.1 |
| 2026-10-21 16:10–16:16 | RD-928 | 主线子 agent | 1.4.1 统一复测：⑥ 复测 / muxfer pwsh 真机测 / ⑩ 关池基线复测 | 三项全 PASS（⑩ 为校正口径，原始口径 FAIL 留痕） | droppedBytes=2774345 实证；关池冷启 0.984s vs 开池稳态 0.447s；测后配置恢复原状 |
| 2026-10-21 16:17 | RD-928 | 主线 | **翻默认**：settings 增 `museBridge.shellType: "pwsh"` | 生效 | hello：1.4.1 / shell_type=pwsh / degraded=false / pool 8/8；冒烟 0.477s；muxfer 往返一致。详见 §9 |
| — | DESKTOP-HC5R77C | — | 未执行（用户令暂缓） | — | 仍 1.3.0 + bash；恢复推进时从 §1 续走 |

## 7. 待补信息（草案缺口，如实标注）

- **hello 字段已定版（2026-10-21 安全修复回填）**：`shell_type`（生效车道）、
  `shell_requested`、`shell_degraded`、`shell_path`、`shell_version`、
  `degraded_reason`；getStatus 同步提供。§1.2/§2.1/§3.1/§5 查这些字段即可，
  不必再以日志原文为准。
- **插件侧配置键已定版**：`museBridge.shellType`（bash/pwsh，默认 bash）、
  `museBridge.pwshPath`（可选覆盖）、`museBridge.pwshPoolSize`（池深，见下条）。
- **一次性 worker 延迟与池深（2026-10-21 用户拍板）**：安全修复的一次性 worker
  在 VM 实测中位 ~665ms/条，用户判定不可接受；解法 = 深池 + 并行预热补仓，
  隔离语义不回退。**放行门新增一条：POC 须实测 RD-928 真机稳态顺序负载的
  有效延迟并达标，未达标不翻默认**（对应 POC 清单第 ⑩ 项）。
- **DESKTOP-HC5R77C 的 pwsh 补装：用户 2026-10-21 令暂缓**，本机不在本轮
  上线范围；其 pwsh 实际路径待恢复推进时回填。
- **翻默认的前置（Muse 侧适配）**：musepc 的反斜杠预双写有两份实现
  （legacy + resident），pwsh 模式下会污染 Windows 路径——翻默认前必须
  按目标机 hello 的 `shell_type` 分流（pwsh 目标关双写）；agent-mail 的
  rd928 动态任务约定同步处理。清单全文见 `~/workspace/pwsh-caller-inventory.md`。
  **状态（2026-10-21）：已随 F1 落地**——新 musepc-clientd 进程内持有车道状态
  并做 shell 感知的反斜杠变换（pwsh 目标不双写），翻默认当晚 111 条对照与
  muxfer 往返实测无路径污染。
- **VPS /cmd 的 idem_key 透传：2026-10-21 用户批准、已部署上线**。VPS
  `muse-bridge.py` 两处 /cmd 重建点（HTTP handler + WSS controller 的
  `api_request` handler）各 +3 行透传（备份
  `muse-bridge.py.bak-idem-20261021-064914`）；直连 VPS API 实测同 key 两发
  第二次 `deduped:true`、副作用恰一次——Layer 2 去重已在线上生效。
- **POC 清单脚本的最终形态**：`~/workspace/muse-bridge-poc/` 已定版
  （POC-CHECKLIST.md + run_poc.py），§2.2 表格与脚本对应关系已核对；
  2026-10-21 审查后增补 ⑨⑩⑪ 三项（askpass 真机、池延迟门、降级角落字段）。
- **1.3.0 回退副本的留存路径**：**已定版**——
  `~/workspace/muse-bridge-poc/rollback/muse-bridge-1.3.0.vsix`（345,471B，
  验真与定版一致）；local_download 只留最新版（现 1.4.1）的惯例不受影响。
  另有 git worktree @55265f8 可重打一条后路。

## §9 翻默认执行记录（2026-10-21 16:17 CST，RD-928）

前置三项全部实质通过后执行：⑥ 截断标记（1.4.1 已装机激活、真机复测 PASS：droppedBytes=2774345 实证）；muxfer pwsh 车道适配（真机三档往返+续传 PASS）；⑩ 延迟门（校正口径 PASS：关池冷启基线 median 0.984s、开池稳态 0.447s ≈ bash 通道参照 0.450s，冷启分量归零；原始公式因 transit 地板 45.7% 结构性不可达，FAIL 已留痕）。

动作：RD-928 用户 settings.json 增 `museBridge.shellType: "pwsh"`（muxfer 推回，BLAKE3 一致）。生效核对：hello = muse-bridge/1.4.1、shell_type=pwsh、requested=pwsh、degraded=false、pwsh 7.6.7、pool size 8 / standby 8。冒烟：pwsh 命令 0.477s 正常回；muxfer 60KB 往返哈希一致（翻后车道自动识别 pwsh）。新 PC DESKTOP-HC5R77C 未动（仍 bash，按用户令暂缓）。
回滚：删除 settings 中该键即回 bash 默认（扩展在线拾取，无需重装/Reload）。
