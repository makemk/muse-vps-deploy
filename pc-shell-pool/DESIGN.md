# PC 插件常驻 Shell 进程池 — 设计方案

> 来源：Gemini 4 第二轮架构评审 §1（`~/workspace/user/files/code_artifact__2.md`）
> 状态：Phase 2 工作（VS Code 插件端大改），本目录只出方案 + 可测原型，**不合入插件主干**。
> 目标：把 PC 端 `/cmd` 的进程拉起开销从 `cmd.exe 15–40ms` / `powershell.exe 150–300ms`
> 压到 **1–3ms**（约 100 倍），让 `VPS→PC→VPS` 段稳定落在 40ms 预算内。

---

## 1. 为什么不用 node-pty（ConPTY）

必须使用 `child_process.spawn(..., { stdio: ['pipe','pipe','pipe'] })` 原生匿名管道。
node-pty 在 Windows 上走 ConPTY，有三个致命缺陷：

1. **输出污染，破坏结构化数据**：ConPTY / `conhost.exe` 会向输出流注入 ANSI 转义序列
   （光标定位、颜色代码、窗口标题 `\x1b]0;...`），还会按终端列宽（如 80 列）强制插入软换行。
   直接损坏命令返回的 JSON、文件路径、Base64 内容——而我们的 `/cmd` 经常回传这类数据。
2. **丢失 stderr 独立流**：PTY 底层把 stdout 与 stderr 合并为单一终端字符流，
   调用方无法区分标准输出与错误输出，`/cmd` 的 `stdout`/`stderr` 分离语义被破坏。
3. **额外渲染延迟**：ConPTY 内部维护虚拟屏幕缓冲区并按帧刷新，相比内核匿名管道
   平白增加 5–15ms 延迟——这已经吃掉了我们优化目标的一大半。

实测基线（Windows 内核 `WriteFile`/`ReadFile` 经匿名管道 < 0.2ms + 解释器解析）：
**常驻 shell 单次命令约 1–3ms**。

---

## 2. 架构：2 × powershell.exe + 2 × cmd.exe

```
┌─────────────────────────────────────────────────────────────┐
│ ShellPool (插件内单例)                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌────────┐  ┌────────┐  │
│  │ ps-worker #1 │  │ ps-worker #2 │  │cmd #1  │  │cmd #2  │  │
│  │ powershell   │  │ powershell   │  │cmd.exe │  │cmd.exe │  │
│  │ stdin/stdout │  │ stdin/stdout │  │ pipes  │  │ pipes  │  │
│  │ stderr pipe  │  │ stderr pipe  │  │        │  │        │  │
│  └──────────────┘  └──────────────┘  └────────┘  └────────┘  │
│        ▲                  ▲                ▲          ▲       │
│        └────────── acquire(busy?/dead?) ──┘          │       │
│   超时/异常 → taskkill /F /T 强杀 → 异步预热替补 → 放回池   │
└─────────────────────────────────────────────────────────────┘
```

- 每个 worker 是一个常驻子进程，三路独立匿名管道（stdin / stdout / stderr）。
- `acquire(shell)`：按需取空闲 worker；同类型无空闲时可借用另一类型（调用方指定 `shell` 时严格匹配）。
- 池大小 2+2 是起点：单台 PC 的 `/cmd` 在插件侧是串行派发为主，4 worker 足以吸收突发；
  若未来压测发现排队，再按需扩容（上限建议 8，避免 Windows 桌面堆句柄压力）。
- `isolated: true` 的请求**不走池**：单独 `spawn` 一次性进程执行（见 §4）。

---

## 3. 哨兵协议（Sentinel Framing）

常驻进程不会在单条命令结束时关闭 stdout（无 EOF），也拿不到进程退出码。
每条命令由插件自动包裹哨兵标记，**stdout 与 stderr 双流都收到 END 标记才结算**。

### 3.1 标记定义

| 标记 | 流 | 含义 |
|---|---|---|
| `__MUSE_OUT_BEGIN_<req_id>__` | stdout | 本请求输出开始 |
| `__MUSE_ERR_BEGIN_<req_id>__` | stderr | 本请求错误流开始 |
| `__MUSE_OUT_END_<req_id>:<exit_code>__` | stdout | 本请求输出结束，携带退出码 |
| `__MUSE_ERR_END_<req_id>__` | stderr | 本请求错误流结束 |

`<req_id>`：每次 `exec` 生成的唯一 id（建议 `crypto.randomUUID()` 去横杠或自增 + 随机后缀，
避免与用户输出碰撞；概率可忽略，另见 §7 的二次校验）。

### 3.2 PowerShell 包装模板

```powershell
[Console]::Out.WriteLine("__MUSE_OUT_BEGIN_<req_id>__");
[Console]::Error.WriteLine("__MUSE_ERR_BEGIN_<req_id>__");
try {
  Set-Location -LiteralPath '<cwd>';
  <用户命令>;
  $ec = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }
} catch {
  [Console]::Error.WriteLine($_.Exception.Message);
  $ec = 1
}
[Console]::Out.WriteLine("__MUSE_OUT_END_<req_id>:$ec");
[Console]::Error.WriteLine("__MUSE_ERR_END_<req_id>__");
```

要点：
- 用 `[Console]::Out/::Error.WriteLine` 而非 `Write-Output`：绕开 PowerShell 的输出流包装，
  保证哨兵行是**纯文本单行**，不被对象格式化污染。
- `$LASTEXITCODE` 覆盖原生命令；`$?` 覆盖 cmdlet；`try/catch` 兜住终止性错误。
- `Set-Location -LiteralPath`：`-LiteralPath` 不解析通配符，路径含 `[` `]` 也安全；
  cwd 中的单引号需转义为双单引号 `''`。

### 3.3 CMD 包装模板

```bat
echo __MUSE_OUT_BEGIN_<req_id>__
echo __MUSE_ERR_BEGIN_<req_id>__ 1>&2
cd /d "<cwd>"
<用户命令>
call echo __MUSE_OUT_END_<req_id>:%ERRORLEVEL%
echo __MUSE_ERR_END_<req_id>__ 1>&2
```

要点：
- `call echo ... %ERRORLEVEL%`：`call` 强制重解析该行，使 `%ERRORLEVEL%` 取到**命令执行后**
  的值（否则按行预解析会取到旧值）。
- `1>&2` 把哨兵行送到 stderr 流。
- `cd /d` 支持跨盘符切换；cwd 中的双引号需转义（`\"` → prototype 中做简单替换，复杂路径建议调用方先 normalize）。
- `@echo off` **不加**：保持默认即可，哨兵行本身用 `echo` 发出，不受影响；
  若用户命令含 `@echo off` 也无妨（只影响后续命令回显，不影响我们的 `echo` 哨兵行输出）。

### 3.4 结算状态机（per-worker，单 pending）

```
          ┌───────── stdout行 ─────────┐
          │ 匹配 OUT_BEGIN_<id> → collecting=true, 丢弃之前所有行(游离输出) │
          ▼                            │
collecting:  累积 stdout 行 / stderr 行（各自独立）                     │
          │  匹配 OUT_END_<id>:<ec> → outDone=true, exitCode=<ec>      │
          │  匹配 ERR_END_<id>     → errDone=true                       │
          └───────── outDone && errDone → resolve({stdout, stderr, exitCode})
```

- stdout/stderr 各自累积，**互不等待对方的 BEGIN**（stderr 可能先到）。
- 结算内容 = BEGIN 之后、END 之前的行（不含哨兵行本身），按收到顺序拼接。
- 超时或管道异常 → 走 §5 自愈，**本次请求 reject，绝不 resolve 半截数据**。

---

## 4. cwd 与环境隔离

- 每次命令前强制 `cd /d "<cwd>"`（cmd）/ `Set-Location -LiteralPath '<cwd>'`（powershell），
  不依赖上条命令遗留的工作目录。
- 环境变量污染：默认信任调用方；若请求带 `"isolated": true`
  （如大面积 `setx`/`$env:` 修改、安装类操作），**退回一次性子进程**执行，
  不占用池 worker，结果语义与池化路径一致。
- 插件侧建议：`cwd` 统一用绝对路径，避免相对路径在池复用间漂移。

---

## 5. 自愈：超时即销毁，绝不放回坏 worker

交互式命令（未加 `-y` 的确认、`pause`、弹窗）会阻塞 stdin 等待输入，
甚至把**下一条命令的字符串当成交互输入吃掉**——这是常驻池最大的正确性风险。

**铁律**：任何 worker 上的命令触发超时（或检测到管道 `error`/`close` 异常），
**严禁把该 worker 放回池中**，执行：

1. `reject` 当前 pending（错误码 `SHELL_TIMEOUT` / `SHELL_BROKEN`，附已收集的 partial 输出供排查，
   但标记 `partial: true`，调用方不得当作正常结果）。
2. `taskkill /F /T /PID <pid>`（Windows）连同子进程树彻底强杀；
   非 Windows（测试）用 `proc.kill('SIGKILL')`。
3. 从池中摘除该 worker，**后台异步** `spawn` 一个新 worker 预热（编码设置 + 空跑一次
   `echo` 哨兵验证管道健康）后加入空闲池。预热期间池容量暂时 -1，不阻塞其他请求。

默认超时：30s（与现有 `/cmd` 的 `timeout` 语义对齐，调用方可按请求覆盖）。

---

## 6. 编码：启动时一次性设为 UTF-8

在每个 worker `spawn` 成功后、投入使用前，一次性写入：

- cmd.exe：`chcp 65001 >nul`
- powershell.exe：
  ```powershell
  [Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  ```

之后所有命令不再重复切换代码页。插件侧 stdout/stderr 按 UTF-8 解码；
解码失败的字节用 `�` 替换（`StringDecoder`），不抛错。

---

## 7. 游离输出过滤

后台残留子进程可能继承管道句柄，在命令结束后继续打印（如上一条命令拉起的后台任务）。
规则：

- BEGIN 标记**之前**收到的所有行 → 直接丢弃（上一条命令的残留或 shell 横幅）。
- END 标记**之后**、下一个 BEGIN 之前收到的行 → 丢弃（本条命令的后台残留）。
- 只有 `[OUT_BEGIN, OUT_END)` 区间内的 stdout 行、`[ERR_BEGIN, ERR_END)` 区间内的 stderr 行
  才计入本次结果。
- 双重保险：哨兵 id 含 8 位随机十六进制（如 `8f3a9b2c`），用户输出偶然撞上完整
  `__MUSE_OUT_END_<8hex>:<digits>__` 格式的概率可忽略；若发生，表现为提前结算，
  调用方可通过 `partial` 标记 + 行数异常在测试中捕获。

---

## 8. 与现有插件的集成点（Phase 2 实施时）

- 位置：`muse-bridge` / `http-proxy-ai` 插件中执行 shell 命令的模块
  （现为每次 `spawn`/`exec` 一次性进程）。
- 改动面：新增 `src/shell/shell-pool.js`（本原型即其前身）；原 `execCommand()` 改为
  `pool.exec(cmd, { shell, cwd, timeoutMs, isolated })`；`isolated` 请求走老路径。
- WSS 消息格式**不变**：`/cmd` 的 `{command, cwd, timeout}` → `{stdout, stderr, exitCode}`，
  池化对 VPS/VM 完全透明。
- 插件启动时预热 4 worker（约 4 × 200ms powershell 冷启动，并行拉起，总 < 500ms），
  VS Code 激活时用户无感。
- 降级开关：`museBridge.shellPool: true/false`（默认 true），设 false 回退到一次性进程，
  用于排查疑难杂症。

## 9. 性能预期与测试计划（Windows 真机）

| 场景 | 现状（一次性进程） | 池化目标 |
|---|---|---|
| `echo hello` (cmd) | ~40ms（含 CreateProcessW 15–40ms） | 2–4ms |
| `echo hello` (powershell) | ~200ms（含冷启动 150–300ms） | 2–5ms |
| 100 次串行 `dir` | ~4s (cmd) / ~20s (ps) | < 1s |
| 中文输出 | 偶发 GBK 乱码 | UTF-8 稳定 |

测试（RD-928 / homepc 真机，VS Code 外）：
```powershell
node prototype.js   # 自检：echo/中文/cwd切换/超时自愈/游离输出过滤
```
见 `prototype.js` 底部 `selftest()`（Windows 下运行；Linux 下仅做语法检查 `node --check`）。

## 10. 风险与未决

1. **杀毒软件误报**：常驻 powershell + 动态写入 stdin 可能被 Defender 标记；
   缓解：进程由 VS Code 插件（已签名宿主）拉起，行为与现有一次性执行一致，只是复用。
2. **长会话状态漂移**：某条命令改了 `$ErrorActionPreference` 等 shell 级状态会影响后续；
   缓解：包装模板每次重置关键偏好（`$ErrorActionPreference='Continue'`），或设 worker
   最大复用次数（如 500 次后主动 retire + 预热替补）。
3. **PowerShell 5.1 vs 7+**：模板只用两者共有语法（`[Console]::`、 `$LASTEXITCODE`、`$?`），
   实测以 Win11 自带 5.1 为准。
