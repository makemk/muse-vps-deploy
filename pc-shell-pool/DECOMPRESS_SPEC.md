# PC 插件解压侧实现规范（DECOMPRESS_SPEC）

> 面向插件开发者。只定规范，不含完整插件代码。
> 配套文档：`~/workspace/muse-vps-deploy/pc-shell-pool/DESIGN.md`（常驻 Shell 池哨兵协议）。
> Daemon 侧压缩实现：`~/workspace/muse-vps-deploy/mused/compress.py`（`SmartCompressor`，判定规则与本规范第 3 节完全一致，两端共用同一套阈值/黑名单/探针逻辑）。

## 1. 目标与范围

Gemini 4 架构评审确定：**应用层端到端压缩**——压缩动作下沉到数据源头（VM Daemon 与 PC 插件），VPS 8471 服务端**完全不感知、不解压、不压缩**，仅原样透传字节流。收益：

1. VPS 零 CPU 消耗（压缩/解压分布在 VM 和 PC 两端）；
2. `VM↔VPS` 与 `VPS↔PC` 两段公网同时享受压缩后的小体积。

本规范只管 **PC 插件侧**：

- **请求路径（VPS→PC）**：收到 `/cmd` / `/batch` / `/file`（含 `read`/`write` 等）请求时，按 `content_encoding` 字段解压后再执行；
- **响应路径（PC→VPS）**：执行完成后，对响应体按同一套规则决定是否压缩再回传。

WSS 通道本身**不开启** `permessage-deflate`（RFC 7692）：VPS 单进程事件循环扛不住 zlib 压缩的 CPU 开销，且无法按消息类型动态开关。

## 2. 协议：`content_encoding` 消息字段

WSS 消息是 JSON（非 HTTP），因此“`Content-Encoding: deflate` 头”映射为**消息级字段** `content_encoding`：

```jsonc
{
  "type": "cmd",
  "id": "req_8f3a9b",
  "content_encoding": "deflate",   // "deflate" | "identity"(或缺省)
  "command": "<base64(zlib.deflate(utf8(命令字符串)))>",
  "timeout": 60,
  "cwd": "C:\\work"
}
```

语义：

| 取值 | 含义 |
|------|------|
| `"deflate"` | 本消息“可压缩字段”承载的是 `base64(zlib.deflate(原始字节))`，接收方**必须先 inflate 还原**再按原字段语义解读 |
| `"identity"` 或字段缺省 | 字段保持原样（明文字符串 / 明文 base64），与旧行为完全一致 |

**可压缩字段定义（按消息类型）**：

| 方向 | 消息 `type` | 可压缩字段 | 压缩前形态 |
|------|-------------|-----------|-----------|
| VPS→PC 请求 | `cmd` / `bash` | `command` | UTF-8 字符串 |
| VPS→PC 请求 | `batch` | `commands`（整个数组序列化后） | UTF-8 JSON 字符串 |
| VPS→PC 请求 | `write_file` / `write` | `data` | base64（已是 base64，压缩的是其解码后的原始字节，再重新 base64）|
| VPS→PC 请求 | `write_file` / `write` | `content` | UTF-8 字符串 |
| PC→VPS 响应 | `cmd_result` | `stdout`、`stderr` | UTF-8 字符串 |
| PC→VPS 响应 | `file_result`（读） | `data` | base64（压缩解码后原始字节，再重新 base64）|

> 注意：`write_file` 的 `data` 字段在旧协议里本身就是 base64。压缩时：`base64 → 解码得原始字节 → zlib.deflate → base64`。解压时逆操作。不要对 base64 文本本身做 deflate（base64 熵高，压不出东西还浪费 CPU）。

**与旧 `compressed` 布尔标志的迁移**：旧代码（`muse-bridge-client.js`）对文件读写使用 `msg.compressed: true` + base64 载荷。新规范统一为 `content_encoding: "deflate"`。插件**必须同时兼容**两者至少一个版本周期：

```js
// 判定某字段是否压缩过的兼容逻辑
function isFieldCompressed(msg) {
  return msg.content_encoding === 'deflate' || msg.compressed === true;
}
```

新发送一律用 `content_encoding`；旧 `compressed` 只做接收兼容，不再主动发送。

## 3. 压缩判定规则（与 Daemon 侧完全一致）

响应路径（PC→VPS）决定是否压缩时，按以下**三步判定**，任一步命中即不压缩（`content_encoding: "identity"`）：

### 3.1 体积下限：`< 2KB` 不压缩

```js
if (rawBytes.length < 2048) return { encoding: 'identity', payload: raw };
```

理由：`< 2KB` 数据装入 1~2 个 TCP MSS（1460B），压缩省不出包数，纯浪费 CPU。

### 3.2 文件后缀黑名单：已压缩/二进制格式直接跳过

```js
const NO_COMPRESS_EXT = new Set([
  '.zip', '.gz', '.tgz', '.7z', '.rar', '.bz2', '.xz', '.zst',
  '.png', '.jpg', '.jpeg', '.gif', '.webp',
  '.mp4', '.mkv', '.mp3',
  '.pdf', '.woff2',
  '.exe', '.dll', '.so', '.pyc', '.whl',
]);

function extBlocked(filename) {
  if (!filename) return false;
  const dot = filename.lastIndexOf('.');
  return dot >= 0 && NO_COMPRESS_EXT.has(filename.slice(dot).toLowerCase());
}
```

> 该列表与 Daemon 侧 `SmartCompressor` 共用同一份，任一端增删后缀时必须同步另一端（建议抽成共享常量文件，当前先靠本文档约束）。

`cmd_result` 的 `stdout`/`stderr` 没有文件名，走 3.3 探针判定（文本输出通常可压）。

### 3.3 前 1KB 采样探针 + 强制 `level=1`

对未知类型且 `> 64KB` 的载荷，先试压前 1KB（仅 ~3μs）：

```js
const probe = zlib.deflateSync(rawBytes.subarray(0, 1024), { level: 1 });
if (probe.length > 870) {
  // 节省不足 15%，判定为高熵二进制，全块跳过压缩
  return { encoding: 'identity', payload: raw };
}
```

压缩级别**强制 `level: 1`（`Z_BEST_SPEED`）**：文本数据可达 `level=6` 约 85% 的压缩率，但 CPU 快 4~5 倍（单核 >220MB/s）；64KB 分片压缩仅 ~0.28ms，在 10Mbps 链路上可省约 40ms 传输，收益比 ~140:1。

## 4. 请求路径（VPS→PC）：先解压，再执行

```
WSS 收到消息
  → 读 content_encoding（兼容旧 compressed 标志）
  → 若 "deflate"：对可压缩字段逐个 inflate（见 4.1）
      → 任一字段解压失败 → 按第 8 节返回 400，**不执行**
  → 得到明文命令/数据
  → 走原有流程：权限校验 → 只读模式校验 → 哨兵包裹 → 常驻 Shell 执行
```

### 4.1 解压位置

解压必须发生在**一切业务逻辑之前**，包括权限校验——因为权限校验要看明文命令字符串：

```js
async function decodeInbound(msg) {
  // 返回解压后的消息浅拷贝（不修改原 msg）
  if (!isFieldCompressed(msg)) return msg;
  const out = { ...msg, content_encoding: 'identity' };
  try {
    const t = String(msg.type || '').toLowerCase();
    if (t === 'cmd' || t === 'bash') {
      out.command = (await inflateToString(msg.command)).toString();
    } else if (t === 'batch') {
      // batch 的 commands 数组整体被压成一个字符串
      const raw = typeof msg.commands === 'string'
        ? msg.commands
        : JSON.stringify(msg.commands);
      out.commands = JSON.parse((await inflateToString(raw)).toString());
    } else if (t === 'write_file' || t === 'write') {
      if (msg.data !== undefined) {
        out.data = (await inflateToBytes(msg.data)).toBase64();
      }
      if (msg.content !== undefined) {
        out.content = (await inflateToString(msg.content)).toString();
      }
    }
    // file_result 回包方向由发送方处理，这里只处理入站请求类型
    return out;
  } catch (e) {
    throw new DecompressError(`inbound inflate failed: ${e.message}`);
  }
}
```

> `inflateToString` / `inflateToBytes` 见第 7 节。注意 `batch` 的特殊处理：发送方把 `commands` 数组 `JSON.stringify` 后整体压缩，字段在压缩态是一个 base64 字符串；解压后 `JSON.parse` 还原数组。

### 4.2 与 `X-Muse-Batchable` 的关系

微批聚合（Daemon 侧 `MicroBatcher`）可能把多个小请求打包成一个 `/batch`。PC 侧**不需要**感知聚合逻辑：收到的就是一个普通 `batch` 消息，按 4.1 解压后走现有 `onBatch` 即可。子请求错误隔离（某条失败不影响其他）由现有 `batch_result.results[]` 数组语义保证。

## 5. 响应路径（PC→VPS）：执行完，再决定压不压

```
执行完成，得 stdout/stderr 或文件字节
  → 按第 3 节三步判定（2KB 下限 / 后缀黑名单 / 1KB 探针）
  → 需压缩：zlib.deflate(level=1) → base64 → 置 content_encoding: "deflate"
  → 不压缩：原样 → content_encoding: "identity"（或省略）
  → sendJson 发回
```

### 5.1 `cmd_result`

```js
async function encodeCmdResult(id, exitCode, stdout, stderr) {
  const fields = {};
  for (const [k, v] of [['stdout', stdout], ['stderr', stderr]]) {
    const buf = Buffer.from(v || '', 'utf8');
    const enc = await maybeCompress(buf, null); // stdout 无文件名，走探针
    fields[k] = enc.encoding === 'deflate' ? enc.payload.toString('base64') : v;
  }
  const anyDeflate = /* 任一字段被压缩 */;
  return {
    type: 'cmd_result', id, ok: true, exitCode,
    ...fields,
    ...(anyDeflate ? { content_encoding: 'deflate' } : {}),
  };
}
```

> 细节：`content_encoding` 是消息级的。若 `stdout` 压了但 `stderr` 没压（<2KB），消息仍标 `deflate`，接收方按“字段是否 base64+zlib”逐个尝试？**不**——规范要求：消息标 `deflate` 时，**所有**可压缩字段都必须按压缩态编码（没达到压缩阈值的字段也走“压缩流程”，只是压缩器内部可能判定不划算……这就矛盾了）。
>
> **最终裁定（避免歧义）**：`content_encoding: "deflate"` 表示**本消息中至少一个**可压缩字段是压缩态；每个字段是否压缩由该字段自身的形态区分**是不允许的**——改为：**字段级标记**。即每个可压缩字段独立决定，消息级 `content_encoding` 仅为提示。接收方对**每个**可压缩字段独立执行 `isFieldCompressed` 判定：
>
> ```js
> // 字段级：字符串字段若以 "deflate:" 前缀开头则为压缩态，其余为 base64(zlib) 体
> ```
>
> 太复杂了。**简化最终方案**：可压缩字段在压缩态时统一编码为 `{"enc":"deflate","data":"<base64>"}` 对象形态，明文态保持原形态（字符串/数组）。接收方 `typeof field === 'object' && field.enc === 'deflate'` 即判定压缩。这样字段级自描述，无需消息级字段猜测，`content_encoding` 消息字段保留作**快速短路**（若缺省且无字段是对象形态，直接走旧逻辑零开销）。

修正后的字段编码规则（覆盖第 2 节的表格）：

| 字段形态 | 含义 |
|---------|------|
| `"plain string..."` | 明文（identity） |
| `{"enc":"deflate","data":"<base64(zlib(level=1) 字节)>"}` | 压缩态 |

`content_encoding: "deflate"` 仍保留在消息上，作为“本消息含压缩字段”的提示，接收方见到后逐字段按对象形态解压；未见该字段时可跳过字段扫描（性能短路）。

### 5.2 `file_result`（读文件）

复用现有模式（`muse-bridge-client.js` 已有 `compressed: true` + base64 实现），迁移到新字段形态：

```js
// 读文件：rawBuf 是磁盘原始字节
const enc = await maybeCompress(rawBuf, filename); // 3 步判定
return this.sendJson({
  type: 'file_result', id: msg.id, ok: true,
  data: enc.encoding === 'deflate'
    ? { enc: 'deflate', data: enc.payload.toString('base64') }
    : rawBuf.toString('base64'),
  bytes: rawBuf.byteLength,
  hash: hash,
  ...(enc.encoding === 'deflate' ? { content_encoding: 'deflate' } : {}),
});
```

> 旧 `compressed: true` 标记不再发送（接收侧保留兼容，见第 2 节）。

### 5.3 大文件流式压缩

文件 `> 8MB` 时避免一次性读入内存 + 一次性 deflate（内存峰值 3 倍文件大小：原文件 + 压缩后 + base64）。用流式（见 7.2）：`fs.createReadStream → zlib.createDeflate({level:1}) → 收集 chunks`。仍要先过 3.2 黑名单与 3.3 探针（探针只需读前 1KB）。

## 6. 与哨兵协议的衔接

**结论：压缩是传输层行为，对哨兵协议完全透明。哨兵协议只在明文上工作。**

```
请求路径（VPS→PC）：
  WSS 消息（可能压缩）
    → 传输层解压 → 明文 command
      → 插件用哨兵包裹：Set-Location …; <command>; … __MUSE_OUT_END_<id>__:$ec
        → 写入常驻 Shell stdin

响应路径（PC→VPS）：
  Shell stdout（含哨兵标记行）
    → 插件按哨兵解析：提 exitCode、去标记、得干净 stdout/stderr
      → 传输层压缩判定（第 3 节）→ 压缩 → WSS 发回
```

关键点：

1. **请求侧**：Daemon 发出的 `command` 是**原始命令**（不含哨兵）。哨兵包裹是插件职责（`DESIGN.md`），发生在解压**之后**。Daemon 不需要知道哨兵的存在。
2. **响应侧**：压缩对象是**哨兵解析后的干净结果**（`stdout`/`stderr` 字符串、`file_result` 的文件字节），不是含哨兵标记的原始流。哨兵标记行（`__MUSE_OUT_BEGIN__` / `__MUSE_OUT_END__`）在解析阶段已被剥离，不进入压缩输入——避免把协议元数据计入压缩判定（标记行虽小，但原则上属于传输开销，不应污染阈值判断）。
3. **禁止**在压缩输入中混入未解析的哨兵流：如果某实现选择压缩原始 stdout（含标记），必须在解压侧能还原出完全一致的字节流——能做到，但无收益且增加耦合，**规范不允许**。

## 7. Node.js 实现片段

> 约定：沿用插件现有风格——`util.promisify(zlib.deflate)` / `promisify(zlib.inflate)`，**勿用 `zlib.promises`**（某些精简 Node 构建缺失，见 `muse-bridge-client.js` 第 6 行注释）。

### 7.1 一发式（< 8MB，默认路径）

```js
const zlib = require('zlib');
const { promisify } = require('util');
const deflateAsync = promisify(zlib.deflate);
const inflateAsync = promisify(zlib.inflate);

// 解压：base64(zlib) → 原始字节；带解压炸弹防护（见第 9 节）
async function inflateToBytes(b64, maxOutput = 256 * 1024 * 1024) {
  const compressed = Buffer.from(String(b64), 'base64');
  if (compressed.length === 0) throw new DecompressError('empty payload');
  // 先用 maxOutput 做上限保护：zlib.inflate 本身无流式上限，这里用输出长度熔断
  const out = await inflateAsync(compressed, { maxOutputLength: maxOutput });
  return out;
}

async function inflateToString(b64, maxOutput) {
  return inflateToBytes(b64, maxOutput); // Buffer，调用方 .toString('utf8')
}

// 压缩判定 + 压缩（第 3 节三步规则）
async function maybeCompress(rawBuf, filename) {
  if (!Buffer.isBuffer(rawBuf)) rawBuf = Buffer.from(rawBuf);
  if (rawBuf.length < 2048) return { encoding: 'identity', payload: rawBuf };
  if (extBlocked(filename)) return { encoding: 'identity', payload: rawBuf };
  if (rawBuf.length > 64 * 1024) {
    const probe = zlib.deflateSync(rawBuf.subarray(0, 1024), { level: 1 });
    if (probe.length > 870) return { encoding: 'identity', payload: rawBuf };
  }
  const compressed = await deflateAsync(rawBuf, { level: 1 });
  // 防御性：压缩后反而变大（极小概率）则回退明文
  if (compressed.length >= rawBuf.length) return { encoding: 'identity', payload: rawBuf };
  return { encoding: 'deflate', payload: compressed };
}
```

> `inflateAsync(buf, { maxOutputLength: maxOutput })`：Node 的 `zlib.inflate` 支持 `maxOutputLength` 选项（字节上限），超限抛 `ERR_BUFFER_TOO_LARGE`——这是解压炸弹的第一道防线（第 9 节）。

### 7.2 流式（> 8MB 大文件）

```js
const fs = require('fs');

function deflateStreamToBuffer(readable) {
  return new Promise((resolve, reject) => {
    const deflater = zlib.createDeflate({ level: 1 });
    const chunks = [];
    let total = 0;
    deflater.on('data', (c) => { chunks.push(c); total += c.length; });
    deflater.on('end', () => resolve(Buffer.concat(chunks, total)));
    deflater.on('error', reject);
    readable.on('error', reject);
    readable.pipe(deflater);
  });
}

function inflateStreamToBuffer(readable, maxOutput = 256 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const inflater = zlib.createInflate({ maxOutputLength: maxOutput });
    const chunks = [];
    let total = 0;
    inflater.on('data', (c) => {
      chunks.push(c); total += c.length;
      if (total > maxOutput) {
        inflater.destroy();
        reject(new DecompressError('inflate output exceeded cap'));
      }
    });
    inflater.on('end', () => resolve(Buffer.concat(chunks, total)));
    inflater.on('error', (e) => reject(new DecompressError(e.message)));
    readable.on('error', reject);
    readable.pipe(inflater);
  });
}

// 大文件读取示例：先探针（只读前 1KB），再决定流式压缩或直接 base64
async function readLargeFile(filePath) {
  const stat = await fs.promises.stat(filePath);
  if (extBlocked(filePath)) return { encoding: 'identity', stream: fs.createReadStream(filePath) };
  if (stat.size < 2048) { /* 小文件走一发式 */ }
  // 探针：读前 1KB
  const head = await readFirstN(filePath, 1024);
  const probe = zlib.deflateSync(head, { level: 1 });
  if (probe.length > 870) return { encoding: 'identity', stream: fs.createReadStream(filePath) };
  const compressed = await deflateStreamToBuffer(fs.createReadStream(filePath));
  return { encoding: 'deflate', payload: compressed };
}
```

### 7.3 字段编解码辅助

```js
class DecompressError extends Error {
  constructor(msg) { super(msg); this.name = 'DecompressError'; }
}

// 压缩态字段形态：{ enc: 'deflate', data: '<base64>' }
function wrapCompressed(buf) {
  return { enc: 'deflate', data: buf.toString('base64') };
}
function isCompressedField(f) {
  return f && typeof f === 'object' && f.enc === 'deflate' && typeof f.data === 'string';
}
async function unwrapField(f, maxOutput) {
  if (!isCompressedField(f)) return f; // 明文原样
  return inflateToBytes(f.data, maxOutput);
}
```

## 8. 错误处理

| 场景 | 处理 | 是否执行命令 |
|------|------|-------------|
| `content_encoding` 非法值（如 `"gzip"`） | 视为 `"identity"`，记 warning 日志 | 执行（按明文） |
| inflate 抛错（数据损坏/非 zlib 流） | 记 error 日志（含 msg.id，不记 payload 明文） | **不执行**，回错误结果 |
| inflate 输出超 `maxOutput` | 同上 | **不执行**，回错误结果 |
| base64 解码失败 | 同上 | **不执行**，回错误结果 |
| 解压后 `batch.commands` JSON.parse 失败 | 同上 | **不执行**，回错误结果 |

错误结果形态（对标现有 `sendResult` / `sendJson`，**不用** HTTP 状态码——WSS 是 JSON 消息层，无 HTTP 语义；任务书中的“返回 400”映射为以下错误结果）：

```js
// cmd / batch
this.sendResult(msg.id, -1, '', `[DecompressError] ${e.message}`);
// file op
this.sendJson({ type: 'file_result', id: msg.id, ok: false, error: `[DecompressError] ${e.message}` });
```

> 为什么解压失败必须不执行：压缩态的 `command` 字段是 base64(zlib) 二进制串，若误当明文执行，等于把乱码喂给 Shell——轻则报错，重则乱码中恰好含有危险字符序列。**先解压成功，再谈执行**，是铁律。

## 9. 安全：解压炸弹防护

攻击者（或 bug）可发送极小压缩包解压出 GB 级数据，撑爆插件内存。两道防线：

1. **`maxOutputLength` 硬上限**：所有 inflate 调用（包括流式）必须传 `maxOutputLength`，默认 `256MB`。Node `zlib.inflate` 的 `maxOutputLength` 选项超限抛 `ERR_BUFFER_TOO_LARGE`；流式路径手动累计 `total` 熔断（7.2）。

   > ⚠️ 曾用名 `maxOutput` 是**错误**的——Node.js 会静默忽略未知选项，等于无防护。详见 `SECURITY-1.2.0.md` C1。
2. **压缩比熔断**：若 `压缩包大小 < 1KB` 但解压输出 `> 10MB`（压缩比 > 10000:1），视为炸弹，直接拒绝。实现：在 `inflateToBytes` 后检查 `out.length / compressed.length`。

```js
async function inflateToBytesGuarded(b64, maxOutput = 256 * 1024 * 1024) {
  const compressed = Buffer.from(String(b64), 'base64');
  if (compressed.length === 0) throw new DecompressError('empty payload');
  const out = await inflateAsync(compressed, { maxOutputLength: maxOutput });
  if (compressed.length < 1024 && out.length > 10 * 1024 * 1024) {
    throw new DecompressError('suspicious compression ratio, rejected');
  }
  return out;
}
```

> 正常业务中 64KB 分片的压缩比 rarely 超过 100:1，此阈值无误伤。

## 10. 测试清单（插件开发者自测）

- [ ] 明文请求（无 `content_encoding`）：行为与旧版完全一致（回归）
- [ ] 旧 `compressed: true` 文件写入：能正确解压（兼容）
- [ ] `deflate` 的 `cmd` 请求：长命令（>2KB，如大段 PowerShell）往返正确，stdout 一致
- [ ] `deflate` 的 `write_file`：二进制文件（随机字节）往返 hash 一致
- [ ] 黑名单：`.zip` / `.png` / `.exe` 的 `file_result` 不带 `content_encoding`
- [ ] `<2KB` 的 `cmd_result`：`stdout` 保持明文字符串形态
- [ ] 解压炸弹：10KB 压缩包声称解压出 1GB → 被拒绝且命令不执行
- [ ] 损坏载荷：随机篡改 base64 后 → `[DecompressError]` 错误结果，命令不执行
- [ ] 哨兵衔接：常驻 Shell 池开启时，压缩 `cmd` 的 exitCode 与明文一致
- [ ] 大文件（>8MB）流式路径：内存峰值 < 1.5 倍文件大小（用 `--inspect` 抽查）

## 附：与 Daemon 侧的对照表

| 项目 | Daemon 侧（`compress.py`） | PC 插件侧（本规范） |
|------|--------------------------|-------------------|
| 压缩库 | `zlib.compress(level=1)` | `zlib.deflate({level:1})`（同算法，Node 命名差异） |
| 判定阈值 | 2KB / 黑名单 / 1KB 探针(870B) | 完全一致（第 3 节） |
| 协议标记 | HTTP `Content-Encoding: deflate` | WSS `content_encoding: "deflate"` + 字段对象形态 |
| 解压失败 | 抛异常，上游返回 502 | `[DecompressError]` 错误结果，不执行 |
| 流式阈值 | —（Daemon 侧按需） | >8MB 走流式（7.2） |
| 哨兵 | 不感知 | 解压后包裹 / 解析后压缩（第 6 节） |
