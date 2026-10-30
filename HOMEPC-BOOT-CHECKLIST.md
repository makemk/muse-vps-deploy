# homepc 开机后的插件升级 + token 轮换清单

触发条件：homepc 上线（隧道巡检出现其指纹，或 VPS 日志出现它的 hello）。
**触发前不要打扰用户**——homepc 事项他说过不急。

## 现状（2026-10-23 记，15:53 修订）

- homepc：插件 **1.4.1**（pwsh 车道），凭证仍是**全局 legacy**，2026-10-21 起关机。
- 02:12 曾承诺"下次开机装 1.4.2 并同法轮换"——**该承诺已过时**：
  开机时直接装 `/home/hatch/local_download/` 里**唯一的** `muse-bridge-*.vsix`
  （当前 **1.7.3**；local_download 只留最新版，文件名即真相），不要经中间版本中转。

## 顺序（不可调换）

1. 确认 homepc 在线（hello / 隧道指纹）。
2. 远程安装最新 vsix（同 RD-928 的远程安装路径）。
3. 用户在 homepc 的 VS Code 里 **Reload Window**（他自己点）。
4. 查 VPS 日志确认 hello 报的是新版本（版本串已动态化，须与装包的版本一致）。
5. `~/workspace/bin/muse-rotate-pc-token <homepc 上报的主机名>`——主机名以 VPS
   hello/在线列表为准，不要凭别名猜。轮换把插件从 legacy 迁到专属绑定。
   - 前提：插件 ≥1.4.2（set_token 支持），所以第 2–4 步必须在轮换之前。
6. 复验：homepc echo 通、新 digest 上线、旧 legacy digest 已退役、其他机器不受影响。

## 备注

- 服务端 /dispatch 补丁是纯增行/已在线上，旧插件在升级前照常工作，不用抢跑。
- homepc 无人值守自升级（不像张德阳那台有对面 Muse 跟版），全程手动。
