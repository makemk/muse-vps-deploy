# 测试（tests）

mused daemon 的 pytest 集成测试。

## 文件清单

| 文件 | 说明 |
|------|------|
| `test_daemon.py` | 6 个集成测试：启动、401 转发、--target 覆盖、SIGTERM 优雅退出、keep-alive、socket 清理 |

## 运行

```bash
cd ~/workspace/muse-vps-deploy/tests
python3 -m pytest test_daemon.py -v
# 预期：6/6 通过
```

## 测试设计

- 测试引用 `../mused/mused.py` 和 `../mused/museapi`（不复制、不修改 daemon 代码）
- 每个测试用隔离的临时 HOME，**永不碰**真实 `~/.config/mused/`
- 用**错误 token**：daemon 上游 warm-up 收到 VPS 的 HTTP 401 后继续运行——401 恰好证明链路通，不需要有效 token
- daemon 以子进程启动，测试结束 SIGTERM 清理

## 覆盖范围

| 测试 | 验证点 |
|------|--------|
| 启动 | daemon 拉起、socket 创建、上游连接建立 |
| 401 转发 | 错误 token 的请求返回 401 给客户端 |
| --target 覆盖 | CLI `--target` 优先于请求 body 的 target |
| SIGTERM | 优雅退出：socket 删除、上游关闭、日志记录 |
| keep-alive | 第二次请求延迟显著降低 |
| socket 清理 | 退出后无残留 socket 文件 |
