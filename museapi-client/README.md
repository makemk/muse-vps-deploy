# museapi-client — HTTPS 直连客户端（生产级）

VM → VPS `/muse-api/` 的 HTTPS 客户端：替代 SSH，成为 `musepc` 的新传输层。
纯 Python 标准库，零第三方依赖。

## 文件清单

| 文件 | 说明 |
|------|------|
| `museapi.py` | **生产级客户端库**：连接池、自动重试、超时控制、Bearer token、typed 错误 |
| `cli.py` | **musecli 命令行**：与 `musepc` 参数兼容（--target/--cwd/--timeout/--read/--write/--archive 等） |
| `benchmark.py` | 延迟基准测试（目标：热连接 p50 <50ms） |
| `test_museapi.py` | 单元测试 17 项（全 mock，无需网络） |
| `README.md` | 本文档 |
| `MIGRATION.md` | 从 musepc 迁移指南 |
| `client.py` / `test.sh` | 早期原型（保留备查，见下） |

## 快速开始

```python
from museapi import MuseAPIClient

with MuseAPIClient() as api:
    r = api.cmd("RD-928", "echo hello", timeout=30)
    print(r["stdout"])

    r = api.read_file("RD-928", "C:/Users/J03378/notes.txt")
    print(r["content"])
```

```bash
# CLI（与 musepc 参数兼容）
python3 cli.py --target RD-928 "echo hello"
python3 cli.py --target RD-928 --cwd "C:/work" --timeout 120 "make -j7"
python3 cli.py --target RD-928 --read "C:/a.txt"
python3 cli.py --target RD-928 --write "C:/b.txt" "hello world"
python3 cli.py --status
```

## API 文档

### `MuseAPIClient(config_path=None, *, token=None, **overrides)`

支持 `with` 语句。配置默认 `~/.config/mused/config.json`，token 从
`token_file`（默认 `~/.config/mused/token`，600 权限）读取。

| 方法 | 说明 |
|------|------|
| `status()` | `GET /muse-api/status` |
| `cmd(target, command, *, cwd="", timeout=60, wait=True)` | 执行 shell 命令 |
| `batch(target, commands, *, timeout=180, wait=True)` | 批量执行 |
| `read_file(target, path)` / `write_file(target, path, content)` | 读写文件 |
| `edit_file(target, path, old, new)` | 替换文件内容 |
| `glob(target, pattern, path="")` / `grep(target, pattern, path="")` | 搜索 |
| `ls(target, path)` | 列目录 |
| `archive(target, path, *extra)` / `extract(target, path, out_dir="")` | 打包/解包 |
| `file_op(target, op, path, **kw)` | 通用文件操作 |
| `request(method, path, body=None, *, timeout=None)` | 原始请求 |

### 错误类型

| 异常 | HTTP | 含义 |
|------|------|------|
| `AuthError` | 401 | token 错误或缺失 |
| `ForbiddenError` | 403 | 无权访问该 target |
| `NotFoundError` | 404 | PC 不在线 |
| `ConflictError` | 409 | 多台 PC 在线，需指定 target |
| `ServerError` | 5xx | 桥接服务端错误 |
| `NetworkError` | — | TCP/TLS/代理故障 |
| `TimeoutError` | — | 连接或读取超时 |

### 重试策略

- 只重试幂等方法（GET/HEAD/OPTIONS），指数退避 0.2s→0.4s→0.8s，最多 3 次
- POST/PUT **永不**自动重试（避免命令重复执行）

### 性能（2026-10-19 实测）

| 场景 | 延迟 |
|------|------|
| 冷启动（TCP+TLS 握手） | ~250ms（一次性） |
| 热连接 keep-alive p50 | **32ms** ✅（目标 <50ms） |
| 热连接 p95 | 59ms |
| 旧方案 SSH | ~300ms |

## 测试

```bash
python3 -m pytest test_museapi.py -v   # 17/17 通过，无需网络
python3 benchmark.py --samples 20       # 需 VPS 网络
```

## 原型备查

`client.py` / `test.sh` 是最早的链路验证原型（MUSE_API_TOKEN 环境变量、
两次请求证明 keep-alive）。`mused` daemon 诞生前的概念验证，确认了
keep-alive 稳态 27ms（vs SSH 300ms）。保留作参考，新开发用 `museapi.py`。
