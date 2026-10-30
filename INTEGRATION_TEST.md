# 去 SSH 链路端到端集成测试方案

> 状态：**方案稿，未执行**（VPS 鉴权 patch 尚未部署）。
> 覆盖链路：`VM daemon(mused)` → HTTPS → `VPS /muse-api/` → WSS → `PC 插件` → 执行 → 返回。
> 相关代码：`../mused/`（daemon+CLI）、`../vps-auth-patch/`（鉴权 patch）、`../token-manager/`（token 生成）、`../scripts/`（部署）。
> 编写日期：2026-10-19。

---

## 1. 测试目标与范围

### 1.1 目标

1. 验证去 SSH 后的完整链路可用：VM 经 daemon 用 HTTPS 直连 VPS，所有原有命令语义（`/cmd`、`/batch`、`/file`、`/result`、`/status`）行为不变。
2. 验证鉴权模型：controller token 双层鉴权、1:1 绑定（越权 403）、超管 token（`pcs=["*"]`，target 必填）。
3. 验证性能：稳态单次往返显著优于 SSH（300ms），达到原型实测的 ~27ms 量级。
4. 验证可靠性：VPS 服务重启、PC 掉线场景下 fail-fast 且可恢复，不丢请求（`wait=false` + `/result`）。

### 1.2 不在范围

- PC 插件内部逻辑（已有 1.1.0 / 3.0.0 的回归覆盖）。
- VPS bridge 的 WSS 协议细节（已有 `test_muse_bridge.py` 17 项回归）。
- `agent-mail` 网关（`/mail-api/`，尚未实施）。

### 1.3 术语

| 缩写 | 含义 |
|---|---|
| daemon | VM 常驻进程 `mused.py`，持 HTTPS keep-alive 到 VPS |
| museapi | daemon 的 CLI 前端（unix socket 本地调用） |
| direct client | 绕过 daemon、直连 VPS HTTPS 的测试客户端（pytest 内实现，用于无 token/错 token 场景） |
| PC-A | RD-928（工作机），token 标签 `muse-rd928` |
| PC-B | homepc（家用机），token 标签 `muse-homepc` |

---

## 2. 测试环境准备

### 2.1 VM 端

- [ ] daemon 已部署并运行：`~/.config/mused/config.json`（600）、`~/.config/mused/token`（600，超管 token）、socket 文件存在、日志 `~/.local/share/mused/mused.log` 无 ERROR。
- [ ] `museapi` CLI 可用：`museapi GET /status` 返回 200。
- [ ] CA 证书：`~/workspace/muse-bridge/src/muse/vps-root-ca.pem` 存在（daemon 默认引用）。
- [ ] egress 代理环境变量：`https_proxy` 已设置（daemon 与 direct client 共用）。
- [ ] pytest：`pip list | grep -i pytest`（测试框架见 §4）。

预检命令：

```bash
# T00 级别的冒烟检查（见 T00）
ls -l ~/.config/mused/config.json ~/.config/mused/token   # 均为 -rw------- 
test -S ~/.config/mused/mused.sock && echo "socket OK"     # socket 路径以 config.json 为准
museapi GET /status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['ok'], d['clients'], d['version'])"
```

### 2.2 VPS 端

- [ ] 鉴权 patch 已部署（`scripts/deploy-auth-patch.sh`，`--check-only` 先 dry-run）。
- [ ] `/etc/muse-bridge/controllers.json`（600）包含 3 个 token：`super`（`pcs=["*"]`）、`muse-rd928`（`pcs=["RD-928"]`）、`muse-homepc`（`pcs=["homepc"]`）。
- [ ] `muse-bridge` 服务运行中：`systemctl is-active muse-bridge`（服务名以实际为准）。
- [ ] nginx `/muse-api/` → `127.0.0.1:8471` 正常（沿用现有配置，零改动）。

### 2.3 PC 端

- [ ] PC-A（RD-928）插件在线：`museapi GET /status` 的 `client_list` 包含 `RD-928`。
- [ ] PC-B（homepc）插件在线：`client_list` 包含 `homepc`。
- [ ] PC-A 工作目录 `C:\Users\J03378\Documents\vscode_env` 可写（T02 用）。
- [ ] PC-A shell 为 Git Bash（`sleep`、`echo` 可用）。

### 2.4 Token 矩阵（测试账号）

| 标签 | pcs | 用途 | 环境变量 |
|---|---|---|---|
| super | `["*"]` | 超管直控，target 必填 | `MUSEAPI_TOKEN_SUPER` |
| muse-rd928 | `["RD-928"]` | 绑定 PC-A | `MUSEAPI_TOKEN_A` |
| muse-homepc | `["homepc"]` | 绑定 PC-B | `MUSEAPI_TOKEN_B` |
| legacy | （旧全局 token） | 向后兼容验证 | `MUSEAPI_LEGACY_TOKEN` |
| invalid | — | 负例 | 硬编码 `muse_THIS_IS_INVALID_000` |

> **安全要求**：token 只从环境变量读取，**永不**写入测试脚本、日志或本文档。CI/手动跑之前 `export`。

### 2.5 预检清单（全部通过才开测）

```bash
# 1. daemon 活着
museapi GET /status
# 2. 两台 PC 在线（clients >= 2）
# 3. 各 token 有效（direct client，见 §4.2）
# 4. 旧 SSH 通路仍可用（性能对比基线，T18 用）
~/workspace/bin/musepc --target RD-928 "echo baseline-ok"
```

---

## 3. 测试用例

> 命令中 `museapi` 默认走 daemon（unix socket），token 取自 `~/.config/mused/token`（super）。
> 需要指定身份时用 `--token "$MUSEAPI_TOKEN_X"` 覆盖（CLI 支持 `--token`）。
> `direct` 指 §4.2 的 direct client（绕过 daemon，直连 VPS，用于无 token/错 token 场景）。

### 3.1 基础功能

#### T00 daemon 冒烟检查

- **前置**：§2.5 预检通过。
- **步骤**：
  1. `ls -l ~/.config/mused/config.json ~/.config/mused/token` 确认均为 `600`。
  2. `museapi GET /status`。
- **预期**：HTTP 200，`ok=true`，`clients>=2`，`version` 与部署版本一致。
- **判定**：PASS = 200 且 `ok=true`；否则 FAIL（先查 daemon 日志，不继续）。

#### T01 echo 全链路往返（核心 happy path）

- **前置**：T00 通过。
- **步骤**：
  ```bash
  time museapi --target RD-928 POST /cmd '{"command":"echo hello-it01","timeout":30}'
  ```
- **预期**：HTTP 200，`ok=true`，`stdout` 包含 `hello-it01`，耗时显著 < 300ms（稳态约 27–70ms）。
- **判定**：PASS = 200 + `ok=true` + stdout 精确包含标记串；耗时记入 §5 基准表。

#### T02 文件写 + 读往返（/file）

- **前置**：T00 通过；PC-A 工作目录可写。
- **步骤**：
  ```bash
  museapi --target RD-928 POST /file '{"op":"write","path":"C:/Users/J03378/Documents/vscode_env/itest_t02.txt","content":"itest-20261019"}'
  museapi --target RD-928 POST /file '{"op":"read","path":"C:/Users/J03378/Documents/vscode_env/itest_t02.txt"}'
  museapi --target RD-928 POST /file '{"op":"info","path":"C:/Users/J03378/Documents/vscode_env/itest_t02.txt"}'
  ```
- **预期**：三次均为 200；读回 `content` 精确等于写入串；`info` 返回 size>0。
- **判定**：PASS = 读回内容逐字节一致；随后清理：`POST /cmd '{"command":"rm C:/Users/J03378/Documents/vscode_env/itest_t02.txt"}'`。
- **备注**：路径语义以插件 `resolvePath` 实际行为为准，预跑时确认（正斜杠在 1.0.1+ 已修复）。

#### T03 命令超时

- **前置**：T00 通过。
- **步骤**：
  ```bash
  time museapi --target RD-928 POST /cmd '{"command":"sleep 10","timeout":3}'
  ```
- **预期**：约 3–8 秒内返回（非 10 秒），`ok=false`，`error` 含 timeout 语义（以实际文案为准，预跑确认）。
- **判定**：PASS = 返回时间 < 8s 且 `ok=false`；FAIL =  hang 住 ≥10s 或返回 `ok=true`。

#### T04 错误命令（PC 侧执行失败）

- **前置**：T00 通过。
- **步骤**：
  ```bash
  museapi --target RD-928 POST /cmd '{"command":"nonexistent-cmd-xyz-123","timeout":15}'
  ```
- **预期**：200（传输层正常），`ok=false`，有可读错误信息（exit code 非零 / command not found），**不是** 500 或连接崩溃。
- **判定**：PASS = 应用层错误正确回传；FAIL = 500/超时/daemon 崩溃。

#### T05 /batch 批量命令

- **前置**：T00 通过。
- **步骤**：
  ```bash
  museapi --target RD-928 POST /batch '{"commands":[{"command":"echo bat-a"},{"command":"echo bat-b"},{"command":"echo bat-c"}],"timeout":30}'
  ```
- **预期**：200，三条结果 `ok=true`，stdout 分别为 `bat-a`/`bat-b`/`bat-c`。
- **判定**：PASS = 3/3 成功且顺序/内容对应；FAIL = 任一失败或错位。

#### T06 /status 内容完整性

- **前置**：T00 通过。
- **步骤**：`museapi GET /status`
- **预期**：200，含 `version`、`clients`、`client_list`、`pending`、`uptime`；`client_list` 中有 `RD-928` 与 `homepc`。
- **判定**：PASS = 字段齐全且两台 PC 均在列。

### 3.2 鉴权与权限

#### T07 无 token → 401

- **前置**：VPS patch 已部署。
- **步骤**：`direct GET /muse-api/status`（不带 `Authorization` 头，也不带 body token）。
- **预期**：HTTP 401，`ok=false`，`error` 含 "Unauthorized"。
- **判定**：PASS = 401；FAIL = 200（鉴权未生效）或连接失败（先确认网络）。

#### T08 错 token → 401

- **前置**：同 T07。
- **步骤**：`direct GET /muse-api/status`，`Authorization: Bearer muse_THIS_IS_INVALID_000`。
- **预期**：401。
- **判定**：PASS = 401 且响应体为 JSON 错误（不是 nginx 默认页）。

#### T09 越权访问 → 403（token A 访问 PC-B）

- **前置**：T07/T08 通过（鉴权层工作正常）。
- **步骤**：
  ```bash
  museapi --token "$MUSEAPI_TOKEN_A" --target homepc POST /cmd '{"command":"echo should-not-run","timeout":15}'
  ```
- **预期**：HTTP 403，`ok=false`，命令**未**在 PC-B 执行（事后在 PC-B 检查无副作用：本例 echo 无副作用，关键看状态码）。
- **判定**：PASS = 403；FAIL = 200（越权成功，严重）或 401（token 本身无效，先重做 T08 区分）。

#### T10 超管无 target → 403

- **前置**：同 T09。
- **步骤**：
  ```bash
  museapi --token "$MUSEAPI_TOKEN_SUPER" POST /cmd '{"command":"echo no-target","timeout":15}'
  ```
- **预期**：403（超管必须显式指定 target，防误发）。
- **判定**：PASS = 403；FAIL = 200（约束未生效）。

#### T11 超管显式 target → 200（sanity）

- **前置**：T10 通过。
- **步骤**：
  ```bash
  museapi --token "$MUSEAPI_TOKEN_SUPER" --target RD-928 POST /cmd '{"command":"echo super-ok","timeout":15}'
  museapi --token "$MUSEAPI_TOKEN_SUPER" --target homepc POST /cmd '{"command":"echo super-ok-b","timeout":15}'
  ```
- **预期**：两次均为 200，`ok=true`，各 PC 回显正确。
- **判定**：PASS = 2/2 成功；证明超管直控两台 PC 能力正常。

#### T12 legacy token 向后兼容 → 200

- **前置**：VPS 保留旧全局 token（`CONFIGURED_TOKEN`）。
- **步骤**：
  ```bash
  museapi --token "$MUSEAPI_TOKEN_LEGACY" --target RD-928 POST /cmd '{"command":"echo legacy-ok","timeout":15}'
  ```
- **预期**：200（降级路径工作）。
- **判定**：PASS = 200；FAIL = 401（降级路径断了，影响旧客户端）。
- **备注**：已知行为——legacy 路径**不做** target 权限检查（设计如此，见 vps-auth-patch/IMPLEMENTATION.md §3），本用例只验证可用性。

#### T13 未知 file op → 400

- **前置**：T00 通过。
- **步骤**：
  ```bash
  museapi --target RD-928 POST /file '{"op":"frobnicate","path":"x"}'
  ```
- **预期**：400，`error` 列出支持的 op。
- **判定**：PASS = 400 且错误信息可用。

### 3.3 并发

#### T14 并发 N=10 / 50 / 100 延迟分布

- **前置**：T01 通过；VPS 负载基线记录（`uptime`）。
- **步骤**（pytest，见 §4.4）：N 个线程同时经 daemon 发 `POST /cmd {"command":"echo c-$i","target":"RD-928"}`（A/B PC 各半可轮换，避免单 PC 串行成为瓶颈——预跑确认 PC 侧是否串行）；
  记录每次耗时，计算 p50/p95/p99、错误数。
- **预期**：
  | N | p95 阈值 | 错误率 |
  |---|---|---|
  | 10 | ≤ 500ms | 0 |
  | 50 | ≤ 1500ms | 0 |
  | 100 | ≤ 3000ms | 0 |
- **判定**：PASS = 零错误且 p95 在阈值内；数据记入 §5。
- **备注**：阈值为初值（VPS 1 vCPU，asyncio 预期轻松承载）；预跑后按实测校准，未达标先查是 VPS 瓶颈还是 PC 侧串行。

### 3.4 可靠性

#### T15 VPS bridge 重启后 daemon 自动重连

- **前置**：T00 通过；**这是最后一次允许用 SSH**（部署后即关闭）。
- **步骤**：
  1. `museapi GET /status`（确认正常，记耗时 t0）。
  2. `ssh -F /home/hatch/.ssh/config vps38 "sudo systemctl restart muse-bridge"`。
  3. 等 5 秒，连续 3 次 `museapi GET /status`，记录每次耗时与状态码。
  4. 检查 daemon 日志 `~/.local/share/mused/mused.log` 有无重连记录、无未恢复异常。
- **预期**：3 次均为 200；第 1 次可能较慢（≤10s，重建 TLS+keep-alive），第 2/3 次回到稳态（~27–70ms）。
- **判定**：PASS = 3/3 成功且第 2/3 次耗时回到稳态区间；FAIL = 任一失败或持续缓慢（连接泄漏）。

#### T16 离线 target 快速失败（fail-fast）

- **前置**：T00 通过。
- **步骤**：
  ```bash
  time museapi --target NO-SUCH-PC-XYZ POST /cmd '{"command":"echo x","timeout":15}'
  ```
- **预期**：**快速**返回错误（< 5s），`ok=false`，`error` 含 "Unknown target"（以实际文案为准）。
- **判定**：PASS = < 5s 内明确失败；FAIL = hang 到 timeout（路由层没有 fail-fast）。
- **备注**：不杀用户 VS Code 进程；真实 PC 掉线场景为 T16b（手动，可选）：用户在 homepc 重启 VS Code，60s 内 `GET /status` 的 `clients` 回到 2。

#### T17 wait=false + /result 异步取结果

- **前置**：T00 通过。
- **步骤**：
  ```bash
  RID=$(museapi --target RD-928 POST /cmd '{"command":"sleep 2; echo async-done","wait":false,"timeout":15}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
  echo "req_id=$RID"
  for i in 1 2 3 4 5; do sleep 2; museapi GET /result/$RID && break; done
  ```
- **预期**：首次 POST 立即返回 200（含 `id`）；轮询 `/result/<id>` 在 10s 内拿到 `ok=true` 且 stdout 含 `async-done`。
- **判定**：PASS = 10s 内取到完整结果；FAIL = 永久 pending 或 id 丢失（结果缓存 TTL 300s，见 bridge 实现）。

### 3.5 性能基准

#### T18 SSH vs HTTPS 实测对比

- **前置**：T01 通过；SSH 通路仍可用（对比基线）。
- **步骤**（各 20 次，取均值/p50/p95）：
  ```bash
  # HTTPS（新）
  for i in $(seq 1 20); do
    /usr/bin/time -f "%e" museapi --target RD-928 POST /cmd '{"command":"echo bench","timeout":15}' >/dev/null
  done
  # SSH（旧基线）
  for i in $(seq 1 20); do
    /usr/bin/time -f "%e" ~/workspace/bin/musepc --target RD-928 "echo bench" >/dev/null
  done
  ```
- **预期**：HTTPS 稳态均值 ~27–70ms；SSH 均值 ~300ms；提升约 5–11 倍。
- **判定**：本用例为**记录型**（无 pass/fail），数据填入 §5 基准表；若 HTTPS 均值 > 150ms 视为异常，需排查（代理/TLS/keep-alive）。

---

## 4. 自动化脚本框架（pytest 骨架）

> 目标目录：`tests/integration/`（新建，与现有 `tests/test_daemon.py` 并列）。
> 只用标准库 + pytest（`http.client`/`ssl`/`socket`/`struct`/`json`），无第三方依赖。
> Token 全部从环境变量读；缺失则对应用例 `pytest.skip`。

### 4.1 依赖与运行

```bash
pip install pytest            # 仅此一个
cd ~/workspace/muse-vps-deploy

# 手动跑全量（需先 export 5 个 token，见 §2.4）
pytest tests/integration/ -v

# 只跑冒烟+基础
pytest tests/integration/test_basic.py -v

# 并发（慢）
pytest tests/integration/test_concurrency.py -v -s

# 性能基准（记录型，输出表格）
pytest tests/integration/test_benchmark.py -v -s
```

### 4.2 `conftest.py`（共享 helpers）

```python
"""Integration test fixtures: daemon socket client + direct HTTPS client."""
import json, os, socket, ssl, struct, time
import http.client
import pytest

HDR = struct.Struct("!I")

# ---- env ----
SUPER = os.environ.get("MUSEAPI_TOKEN_SUPER")
TOKEN_A = os.environ.get("MUSEAPI_TOKEN_A")      # RD-928
TOKEN_B = os.environ.get("MUSEAPI_TOKEN_B")      # homepc
LEGACY = os.environ.get("MUSEAPI_LEGACY_TOKEN")
INVALID = "muse_THIS_IS_INVALID_000"

VPS_HOST = os.environ.get("MUSEAPI_VPS_HOST", "38.207.167.51")
VPS_PORT = int(os.environ.get("MUSEAPI_VPS_PORT", "443"))
SNI = os.environ.get("MUSEAPI_SNI", "ai.vps38.local")
CA = os.path.expanduser(os.environ.get(
    "MUSEAPI_CA", "~/workspace/muse-bridge/src/muse/vps-root-ca.pem"))
SOCK_PATH = os.path.expanduser(os.environ.get(
    "MUSEAPI_SOCK", "~/.config/mused/mused.sock"))  # 以 config.json 为准

pytestmark = pytest.mark.integration


def need(env_val, name):
    if not env_val:
        pytest.skip(f"env {name} not set")


# ---- daemon client (unix socket, length-prefixed JSON) ----
def daemon_call(method, path, body=None, token=None, timeout=60):
    """POST/GET via mused daemon. Returns (status_hint, dict).

    daemon 协议：4 字节大端长度 + JSON {method, path, body, token?}。
    具体帧格式以 mused.py 实现为准，预跑时对齐。
    """
    req = {"method": method, "path": path}
    if body is not None:
        req["body"] = body
    if token is not None:
        req["token"] = token
    payload = json.dumps(req).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK_PATH)
    s.sendall(HDR.pack(len(payload)) + payload)
    raw_len = s.recv(4)
    (n,) = HDR.unpack(raw_len)
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data.decode())


# ---- direct HTTPS client (bypass daemon; for no/bad token cases) ----
def _proxy_from_env():
    raw = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    assert raw, "https_proxy not set"
    raw = raw.replace("http://", "").replace("https://", "")
    host, _, port = raw.partition(":")
    return host, int(port or 8080)


def direct_request(method, path, body=None, token=None, timeout=30):
    """Direct HTTPS to VPS /muse-api/* via CONNECT proxy. Returns (status, dict)."""
    ph, pp = _proxy_from_env()
    sock = socket.create_connection((ph, pp), timeout=timeout)
    connect_req = (f"CONNECT {VPS_HOST}:{VPS_PORT} HTTP/1.1\r\n"
                   f"Host: {VPS_HOST}:{VPS_PORT}\r\n\r\n")
    sock.sendall(connect_req.encode())
    resp = sock.recv(4096).decode(errors="replace")
    assert " 200 " in resp.split("\r\n")[0], f"proxy CONNECT failed: {resp[:80]}"
    ctx = ssl.create_default_context(cafile=CA)
    # SNI 必须为 ai.vps38.local（代理会 MITM 掐 SNI，见调研结论）
    tls = ctx.wrap_socket(sock, server_hostname=SNI)
    conn = http.client.HTTPSConnection(VPS_HOST, VPS_PORT, timeout=timeout)
    conn.sock = tls  # 复用已建好的隧道
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body or {}).encode() if body is not None else None
    conn.request(method, path, body=payload, headers=headers)
    r = conn.get_response()
    data = r.read()
    try:
        return r.status, json.loads(data.decode() or "{}")
    except json.JSONDecodeError:
        return r.status, {"_raw": data[:200].decode(errors="replace")}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))]
```

> **注意**：`daemon_call` 的帧格式为示意——以 `mused.py` 实际 socket 协议为准，预跑时对齐（`museapi` CLI 是参考实现）。

### 4.3 用例文件骨架

```python
# tests/integration/test_basic.py  (T00/T01/T02/T03/T04/T05/T06)
import time
from conftest import daemon_call, SUPER, need

def test_t00_smoke():
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    d = daemon_call("GET", "/status", token=SUPER)
    assert d["ok"] is True and d["clients"] >= 2

def test_t01_echo():
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    t = time.monotonic()
    d = daemon_call("POST", "/cmd",
                    {"target": "RD-928", "command": "echo hello-it01", "timeout": 30},
                    token=SUPER)
    dt = (time.monotonic() - t) * 1000
    assert d["ok"] is True and "hello-it01" in d.get("stdout", "")
    print(f"\nT01 latency: {dt:.1f}ms")

def test_t02_file_roundtrip():
    # write -> read -> info -> cleanup，见 §3 T02
    ...

def test_t03_timeout():
    t = time.monotonic()
    d = daemon_call("POST", "/cmd",
                    {"target": "RD-928", "command": "sleep 10", "timeout": 3},
                    token=SUPER)
    dt = time.monotonic() - t
    assert d["ok"] is False and dt < 8

def test_t04_bad_command():
    d = daemon_call("POST", "/cmd",
                    {"target": "RD-928", "command": "nonexistent-cmd-xyz-123",
                     "timeout": 15}, token=SUPER)
    assert d["ok"] is False  # 应用层错误，非 500/崩溃

def test_t05_batch():
    d = daemon_call("POST", "/batch",
                    {"target": "RD-928",
                     "commands": [{"command": "echo bat-a"},
                                  {"command": "echo bat-b"},
                                  {"command": "echo bat-c"}],
                     "timeout": 30}, token=SUPER)
    assert d["ok"] is True and len(d["results"]) == 3

def test_t06_status_fields():
    d = daemon_call("GET", "/status", token=SUPER)
    names = [c.get("hostname") for c in d["client_list"]]
    assert "RD-928" in names and "homepc" in names
```

```python
# tests/integration/test_auth.py  (T07/T08/T09/T10/T11/T12/T13)
from conftest import daemon_call, direct_request, SUPER, TOKEN_A, LEGACY, INVALID, need

def test_t07_no_token_401():
    status, d = direct_request("GET", "/muse-api/status", token=None)
    assert status == 401

def test_t08_bad_token_401():
    status, d = direct_request("GET", "/muse-api/status", token=INVALID)
    assert status == 401

def test_t09_cross_pc_403():
    need(TOKEN_A, "MUSEAPI_TOKEN_A")
    d = daemon_call("POST", "/cmd",
                    {"target": "homepc", "command": "echo should-not-run",
                     "timeout": 15}, token=TOKEN_A)
    assert d.get("_http_status") == 403 or d["ok"] is False
    # 精确断言以 daemon 错误透传格式为准，预跑对齐

def test_t10_super_no_target_403():
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    d = daemon_call("POST", "/cmd",
                    {"command": "echo no-target", "timeout": 15}, token=SUPER)
    assert d.get("_http_status") == 403 or d["ok"] is False

def test_t11_super_explicit_target():
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    for pc, mark in [("RD-928", "super-ok"), ("homepc", "super-ok-b")]:
        d = daemon_call("POST", "/cmd",
                        {"target": pc, "command": f"echo {mark}", "timeout": 15},
                        token=SUPER)
        assert d["ok"] is True and mark in d.get("stdout", "")

def test_t12_legacy_compat():
    need(LEGACY, "MUSEAPI_LEGACY_TOKEN")
    d = daemon_call("POST", "/cmd",
                    {"target": "RD-928", "command": "echo legacy-ok", "timeout": 15},
                    token=LEGACY)
    assert d["ok"] is True

def test_t13_unknown_file_op_400():
    d = daemon_call("POST", "/file",
                    {"target": "RD-928", "op": "frobnicate", "path": "x"},
                    token=SUPER)
    assert d.get("_http_status") == 400 or d["ok"] is False
```

```python
# tests/integration/test_concurrency.py  (T14)
import time, threading
import pytest
from conftest import daemon_call, pct, SUPER, need

@pytest.mark.parametrize("n,thr", [(10, 500), (50, 1500), (100, 3000)])
def test_t14_concurrent(n, thr):
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    lat, errs = [], []
    def one(i):
        pc = "RD-928" if i % 2 == 0 else "homepc"
        t = time.monotonic()
        try:
            d = daemon_call("POST", "/cmd",
                            {"target": pc, "command": f"echo c-{i}", "timeout": 30},
                            token=SUPER, timeout=60)
            assert d["ok"] is True
            lat.append((time.monotonic() - t) * 1000)
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    ts = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    print(f"\nN={n}: p50={pct(lat,50):.0f}ms p95={pct(lat,95):.0f}ms "
          f"p99={pct(lat,99):.0f}ms errors={len(errs)}")
    assert not errs, f"{len(errs)} errors: {errs[:3]}"
    assert pct(lat, 95) <= thr, f"p95 {pct(lat,95):.0f}ms > {thr}ms"
```

```python
# tests/integration/test_reliability.py  (T15/T16/T17)
import time, subprocess
import pytest
from conftest import daemon_call, SUPER, need

@pytest.mark.manual(reason="needs one last SSH to VPS; run explicitly")
def test_t15_bridge_restart_reconnect():
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    d0 = daemon_call("GET", "/status", token=SUPER)
    assert d0["ok"] is True
    subprocess.run(["ssh", "-F", "/home/hatch/.ssh/config", "vps38",
                    "sudo systemctl restart muse-bridge"], check=True)
    time.sleep(5)
    lats = []
    for _ in range(3):
        t = time.monotonic()
        d = daemon_call("GET", "/status", token=SUPER, timeout=30)
        lats.append((time.monotonic() - t) * 1000)
        assert d["ok"] is True
    print(f"\npost-restart latencies: {[f'{x:.0f}ms' for x in lats]}")
    assert lats[0] <= 10000 and lats[2] <= 150  # 第一次重建连接，后续回稳态

def test_t16_offline_target_failfast():
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    t = time.monotonic()
    d = daemon_call("POST", "/cmd",
                    {"target": "NO-SUCH-PC-XYZ", "command": "echo x", "timeout": 15},
                    token=SUPER)
    dt = time.monotonic() - t
    assert d["ok"] is False and dt < 5, f"not fail-fast: {dt:.1f}s"

def test_t17_wait_false_result():
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    d = daemon_call("POST", "/cmd",
                    {"target": "RD-928", "command": "sleep 2; echo async-done",
                     "wait": False, "timeout": 15}, token=SUPER)
    rid = d["id"]
    got = None
    for _ in range(5):
        time.sleep(2)
        r = daemon_call("GET", f"/result/{rid}", token=SUPER)
        if r.get("ok") is True or "stdout" in r:
            got = r
            break
    assert got and "async-done" in got.get("stdout", "")
```

```python
# tests/integration/test_benchmark.py  (T18, 记录型)
import time, subprocess, statistics
from conftest import daemon_call, pct, SUPER, need

def _time_https(n=20):
    need(SUPER, "MUSEAPI_TOKEN_SUPER")
    xs = []
    for _ in range(n):
        t = time.monotonic()
        d = daemon_call("POST", "/cmd",
                        {"target": "RD-928", "command": "echo bench", "timeout": 15},
                        token=SUPER)
        assert d["ok"] is True
        xs.append((time.monotonic() - t) * 1000)
    return xs

def _time_ssh(n=20):
    xs = []
    for _ in range(n):
        t = time.monotonic()
        subprocess.run(["/home/hatch/workspace/bin/musepc", "--target", "RD-928",
                        "echo bench"], check=True, capture_output=True, timeout=60)
        xs.append((time.monotonic() - t) * 1000)
    return xs

def test_t18_benchmark(capsys):
    h, s = _time_https(), _time_ssh()
    with capsys.disabled():
        print("\n=== T18 benchmark (ms) ===")
        print(f"HTTPS: mean={statistics.mean(h):.1f} p50={pct(h,50):.1f} p95={pct(h,95):.1f}")
        print(f"SSH  : mean={statistics.mean(s):.1f} p50={pct(s,50):.1f} p95={pct(s,95):.1f}")
        print(f"speedup: {statistics.mean(s)/statistics.mean(h):.1f}x")
    # 记录型：不断言，只输出；异常阈值告警
    assert statistics.mean(h) < 150, "HTTPS mean > 150ms, investigate"
```

### 4.4 预跑对齐清单（骨架→可跑之前必须确认）

- [ ] `daemon_call` 帧格式与 `mused.py` socket 协议一致（参考 `museapi` CLI 实现）。
- [ ] daemon 对上游非 200 的透传格式（`_http_status` 字段是否存在；T09/T10/T13 的断言据此调整）。
- [ ] `/cmd` 超时 / 错误命令的实际 `error` 文案（T03/T04 断言用 `in` 匹配关键字）。
- [ ] `/batch` 返回体字段名（`results`？T05）。
- [ ] `client_list` 元素字段名（`hostname`？T06）。
- [ ] `wait=false` 返回的 id 字段名（T17）。
- [ ] PC-A/PC-B 并发是否串行（T14 若 PC 侧串行，阈值需放宽或只测单 PC）。

---

## 5. 性能基准记录表

> T01（单次）、T14（并发）、T18（对比）跑完后填写。每次记录：日期、VPS 负载（`uptime` 1min）、样本数。

### 5.1 单次命令延迟（T01，`echo`，RD-928）

| 日期 | 通路 | 样本数 | 均值 | p50 | p95 | VPS 1min 负载 | 备注 |
|---|---|---|---|---|---|---|---|
| | HTTPS(daemon) | 20 | | | | | |
| | SSH(musepc) | 20 | | | | | 基线 |

### 5.2 并发延迟分布（T14）

| 日期 | N | 成功/总数 | p50 | p95 | p99 | 错误数 | 备注 |
|---|---|---|---|---|---|---|---|
| | 10 | / | | | | | |
| | 50 | / | | | | | |
| | 100 | / | | | | | |

### 5.3 场景对比（T18 扩展，可选）

| 场景 | HTTPS 均值 | SSH 均值 | 提升倍数 | 备注 |
|---|---|---|---|---|
| echo（小输出） | | | | T18 |
| /file read 1KB | | | | T02 变体，20 次 |
| /batch 5 条 | | | | T05 变体，20 次 |

### 5.4 判定参考线

- 单次 HTTPS 稳态均值应 < 150ms（原型实测 27ms，留足余量）；> 150ms 视为异常。
- 并发零错误为硬要求；p95 阈值见 T14 表格，预跑后校准。

---

## 6. 执行顺序与通过标准

### 6.1 推荐顺序

```
T00 → T01 → T06 → T07 → T08 → T12 → T09 → T10 → T11 → T13
  → T02 → T05 → T03 → T04 → T17 → T16 → T14(10→50→100) → T18 → T15
```

理由：先冒烟（T00/T01），再鉴权（T07–T13，鉴权坏了后面全是噪音），再功能（T02–T05），再边界（T03/T04/T17/T16），再压力（T14/T18），最后做破坏性操作（T15 重启 VPS 服务）。

### 6.2 通过标准

- **全量通过**：18 个用例 PASS（含 T18 记录完整）。
- **发布门禁**：T00–T13 必须全 PASS（鉴权零容忍：T09/T10 任一 FAIL 即阻塞上线）；T14 零错误；T15 3/3 成功。
- **回归**：VPS patch 或 daemon 任何变更后，重跑 T00–T13（约 5 分钟）。

### 6.3 失败处理

| 现象 | 先查 |
|---|---|
| T00 FAIL | daemon 进程/日志 `~/.local/share/mused/mused.log`、socket 权限 |
| T07/T08 非 401 | patch 是否部署成功（`deploy-auth-patch.sh --check-only`）、nginx 是否绕过 8471 |
| T09/T10 非 403 | `controllers.json` 的 `pcs` 字段、patch 的 target 检查是否插入三处 handler |
| T14 错误率高 | VPS `uptime`/内存、PC 侧是否串行（见 §4.4） |
| T15 恢复慢 | daemon 重连退避逻辑、nginx keepalive 超时 |

---

## 附录 A：手动跑 checklist（无 pytest 时）

```bash
export MUSEAPI_TOKEN_SUPER=... MUSEAPI_TOKEN_A=... MUSEAPI_TOKEN_B=... MUSEAPI_LEGACY_TOKEN=...
museapi GET /status                                        # T00
time museapi --target RD-928 POST /cmd '{"command":"echo hi","timeout":30}'   # T01
# …其余见 §3 各用例命令
```

## 附录 B：回滚

任一门禁用例 FAIL 且 30 分钟内无法定位 → 按 `scripts/DEPLOY.md` 回滚 VPS patch（`deploy-auth-patch.sh --rollback`），流量切回 SSH（`~/workspace/bin/musepc` 不动），本测试方案归档为"未通过"版本。
