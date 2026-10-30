'use strict';
/**
 * ShellPool — PC 插件常驻 Shell 进程池（原型）
 *
 * 设计见 DESIGN.md。Windows 真机运行：node prototype.js
 * Linux 下仅做语法检查：node --check prototype.js
 *
 * 核心语义：
 *  - 2 × powershell.exe + 2 × cmd.exe 常驻，stdio 三路独立管道
 *  - 每条命令用哨兵帧包裹，stdout/stderr 双流都收到 END 才结算
 *  - 超时/管道异常 → taskkill /F /T 强杀 → 异步预热替补；坏 worker 永不放回池
 *  - BEGIN 之前 / END 之后的游离输出一律丢弃
 */

const { spawn, execFile } = require('child_process');
const crypto = require('crypto');
const os = require('os');

const IS_WIN = os.platform() === 'win32';

/* ------------------------------------------------------------------ */
/* 哨兵标记                                                            */
/* ------------------------------------------------------------------ */
function newReqId() {
  return crypto.randomBytes(4).toString('hex'); // 8 位十六进制
}
const OUT_BEGIN = (id) => `__MUSE_OUT_BEGIN_${id}__`;
const ERR_BEGIN = (id) => `__MUSE_ERR_BEGIN_${id}__`;
const OUT_END_RE = /^__MUSE_OUT_END_([0-9a-f]{8}):(-?\d+)__\s*$/;
const ERR_END_RE = /^__MUSE_ERR_END_([0-9a-f]{8})__\s*$/;
const BEGIN_RE = /^__MUSE_(OUT|ERR)_BEGIN_([0-9a-f]{8})__\s*$/;

/* ------------------------------------------------------------------ */
/* 命令包装模板                                                         */
/* ------------------------------------------------------------------ */
function escPsSingleQuote(s) {
  return String(s).replace(/'/g, "''");
}

function wrapPowerShell(reqId, cwd, command) {
  return [
    `[Console]::Out.WriteLine("${OUT_BEGIN(reqId)}");`,
    `[Console]::Error.WriteLine("${ERR_BEGIN(reqId)}");`,
    `try {`,
    `  $ErrorActionPreference='Continue';`,
    `  Set-Location -LiteralPath '${escPsSingleQuote(cwd)}';`,
    `  ${command};`,
    `  $ec = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }`,
    `} catch {`,
    `  [Console]::Error.WriteLine($_.Exception.Message);`,
    `  $ec = 1`,
    `}`,
    `[Console]::Out.WriteLine("__MUSE_OUT_END_${reqId}:$ec");`,
    `[Console]::Error.WriteLine("__MUSE_ERR_END_${reqId}__");`,
    ``,
  ].join('\n');
}

function wrapCmd(reqId, cwd, command) {
  const safeCwd = String(cwd).replace(/"/g, '""');
  return [
    `echo ${OUT_BEGIN(reqId)}`,
    `echo ${ERR_BEGIN(reqId)} 1>&2`,
    `cd /d "${safeCwd}"`,
    command,
    `call echo __MUSE_OUT_END_${reqId}:%ERRORLEVEL%`,
    `echo __MUSE_ERR_END_${reqId}__ 1>&2`,
    ``,
  ].join('\r\n');
}

/* ------------------------------------------------------------------ */
/* Worker：单个常驻 shell 进程                                           */
/* ------------------------------------------------------------------ */
const WORKER_START_TIMEOUT_MS = 15000;

class ShellWorker {
  constructor(shell) {
    this.shell = shell;               // 'powershell' | 'cmd'
    this.proc = null;
    this.busy = false;
    this.dead = false;
    this.uses = 0;
    this.pending = null;              // 当前请求上下文
    this.outBuf = '';
    this.errBuf = '';
    this.onUnexpectedExit = null;     // 由 pool 注入
  }

  spawn() {
    return new Promise((resolve, reject) => {
      const exe = this.shell === 'powershell' ? 'powershell.exe' : 'cmd.exe';
      const args = this.shell === 'powershell'
        ? ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', '-']
        : [];
      // NOTE: powershell 以 -Command - 从 stdin 读脚本；cmd 默认即读 stdin
      const proc = spawn(exe, args, {
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
      });
      this.proc = proc;
      let settled = false;
      const timer = setTimeout(() => {
        if (!settled) { settled = true; this._kill(); reject(new Error('worker spawn timeout')); }
      }, WORKER_START_TIMEOUT_MS);

      proc.on('error', (e) => {
        if (!settled) { settled = true; clearTimeout(timer); reject(e); }
        else this._handleExit('error', e);
      });
      proc.on('exit', (code, signal) => this._handleExit('exit', { code, signal }));
      proc.stdout.on('data', (d) => this._onData('out', d));
      proc.stderr.on('data', (d) => this._onData('err', d));

      // 编码初始化（DESIGN.md §6）
      const initCmd = this.shell === 'powershell'
        ? `[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n$OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n`
        : `chcp 65001 >nul\r\n`;
      const finishInit = () => {
        if (settled) return;
        settled = true; clearTimeout(timer);
        // 小睡一口让编码命令生效，再 resolve
        setTimeout(resolve, 120);
      };
      try {
        proc.stdin.write(initCmd, 'utf8', (err) => {
          if (err) { if (!settled) { settled = true; clearTimeout(timer); reject(err); } }
          else finishInit();
        });
      } catch (e) {
        if (!settled) { settled = true; clearTimeout(timer); reject(e); }
      }
    });
  }

  _handleExit(kind, info) {
    if (this.dead) return;
    this.dead = true;
    const pend = this.pending;
    this.pending = null;
    if (pend) {
      clearTimeout(pend.timer);
      pend.reject(Object.assign(
        new Error(`shell worker ${kind}: ${JSON.stringify(info)} (partial output discarded)`),
        { code: 'SHELL_BROKEN', partial: true, stdout: pend.outLines.join('\n'), stderr: pend.errLines.join('\n') }
      ));
    }
    if (typeof this.onUnexpectedExit === 'function') {
      try { this.onUnexpectedExit(this); } catch (_) { /* ignore */ }
    }
  }

  _onData(which, chunk) {
    const pend = this.pending;
    if (!pend) return; // 空闲时的数据（编码初始化回显等）直接丢弃
    const text = chunk.toString('utf8');
    if (which === 'out') { this.outBuf += text; this._drain('out'); }
    else { this.errBuf += text; this._drain('err'); }
  }

  _drain(which) {
    const pend = this.pending;
    if (!pend) return;
    let buf = which === 'out' ? this.outBuf : this.errBuf;
    let idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).replace(/\r$/, '');
      buf = buf.slice(idx + 1);
      this._onLine(which, line);
      if (!this.pending) break; // 已结算
    }
    if (which === 'out') this.outBuf = buf; else this.errBuf = buf;
  }

  _onLine(which, line) {
    const pend = this.pending;
    if (!pend || pend.settled) return;
    const id = pend.reqId;

    // BEGIN 标记
    const bm = line.match(BEGIN_RE);
    if (bm && bm[2] === id) {
      if (bm[1] === 'OUT') { pend.collectingOut = true; pend.outLines = []; }
      else { pend.collectingErr = true; pend.errLines = []; }
      return;
    }
    // END 标记
    let m = which === 'out' ? line.match(OUT_END_RE) : null;
    if (m && m[1] === id) {
      pend.outDone = true;
      pend.exitCode = parseInt(m[2], 10);
      this._trySettle();
      return;
    }
    m = which === 'err' ? line.match(ERR_END_RE) : null;
    if (m && m[1] === id) {
      pend.errDone = true;
      this._trySettle();
      return;
    }
    // 普通行：只有收集态才累积，否则当游离输出丢弃（DESIGN.md §7）
    if (which === 'out' && pend.collectingOut) pend.outLines.push(line);
    else if (which === 'err' && pend.collectingErr) pend.errLines.push(line);
  }

  _trySettle() {
    const pend = this.pending;
    if (!pend || pend.settled || !(pend.outDone && pend.errDone)) return;
    pend.settled = true;
    clearTimeout(pend.timer);
    const durationMs = Date.now() - pend.startedAt;
    this.pending = null;
    this.busy = false;
    this.uses += 1;
    pend.resolve({
      stdout: pend.outLines.join('\n'),
      stderr: pend.errLines.join('\n'),
      exitCode: pend.exitCode,
      durationMs,
      reqId: pend.reqId,
      shell: this.shell,
      workerUses: this.uses,
    });
  }

  /**
   * 在该 worker 上执行一条命令。调用前确保 !busy && !dead。
   */
  exec(command, { cwd, timeoutMs }) {
    if (this.busy || this.dead) {
      return Promise.reject(new Error('worker not available'));
    }
    this.busy = true;
    const reqId = newReqId();
    const wrapped = this.shell === 'powershell'
      ? wrapPowerShell(reqId, cwd, command)
      : wrapCmd(reqId, cwd, command);
    return new Promise((resolve, reject) => {
      const pend = {
        reqId, resolve, reject,
        startedAt: Date.now(),
        settled: false,
        collectingOut: false, collectingErr: false,
        outLines: [], errLines: [],
        outDone: false, errDone: false,
        exitCode: null,
        timer: null,
      };
      pend.timer = setTimeout(() => this._onTimeout(pend), timeoutMs);
      this.pending = pend;
      this.outBuf = '';
      this.errBuf = '';
      try {
        this.proc.stdin.write(wrapped, 'utf8', (err) => {
          if (err && this.pending === pend && !pend.settled) {
            this._failWorker(pend, 'SHELL_WRITE_FAIL', err);
          }
        });
      } catch (e) {
        this._failWorker(pend, 'SHELL_WRITE_FAIL', e);
      }
    });
  }

  _onTimeout(pend) {
    if (pend.settled || this.pending !== pend) return;
    pend.settled = true;
    const err = Object.assign(
      new Error(`shell command timed out after ${Date.now() - pend.startedAt}ms (worker destroyed, never returned to pool)`),
      {
        code: 'SHELL_TIMEOUT', partial: true,
        stdout: pend.outLines.join('\n'), stderr: pend.errLines.join('\n'),
      }
    );
    this.pending = null;
    pend.reject(err);
    this.destroy(); // 超时即销毁（DESIGN.md §5 铁律）
  }

  _failWorker(pend, code, cause) {
    if (pend.settled) return;
    pend.settled = true;
    clearTimeout(pend.timer);
    this.pending = null;
    pend.reject(Object.assign(
      new Error(`${code}: ${cause && cause.message ? cause.message : cause} (worker destroyed)`),
      { code, partial: true }
    ));
    this.destroy();
  }

  /** 强杀进程（含子进程树）。Windows 用 taskkill /F /T。 */
  destroy() {
    if (this.dead) return;
    this.dead = true;
    const proc = this.proc;
    this.proc = null;
    if (!proc) return;
    try {
      if (IS_WIN) {
        execFile('taskkill', ['/F', '/T', '/PID', String(proc.pid)], () => {});
        // 兜底：2s 后若还在，直接 kill
        setTimeout(() => { try { proc.kill('SIGKILL'); } catch (_) {} }, 2000);
      } else {
        try { proc.kill('SIGKILL'); } catch (_) {}
      }
    } catch (_) { /* ignore */ }
  }
}

/* ------------------------------------------------------------------ */
/* ShellPool                                                            */
/* ------------------------------------------------------------------ */
class ShellPool {
  constructor(opts = {}) {
    this.counts = {
      powershell: opts.powershell ?? 2,
      cmd: opts.cmd ?? 2,
    };
    this.defaultTimeoutMs = opts.timeoutMs ?? 30000;
    this.maxUses = opts.maxUses ?? 500;   // worker 最大复用次数，防状态漂移（DESIGN.md §10.2）
    this.workers = [];                    // 全部 worker（含 busy）
    this.warming = 0;                     // 正在预热替补的数量
  }

  async init() {
    const jobs = [];
    for (const [shell, n] of Object.entries(this.counts)) {
      for (let i = 0; i < n; i++) jobs.push(this._spawnAndAdd(shell));
    }
    await Promise.all(jobs);
  }

  async _spawnAndAdd(shell) {
    const w = new ShellWorker(shell);
    w.onUnexpectedExit = (worker) => this._replaceWorker(worker, 'unexpected-exit');
    await w.spawn();
    // 预热验证：空跑一次哨兵，确认管道健康
    this.workers.push(w);
    try {
      await this._healthCheck(w);
    } catch (e) {
      this._replaceWorker(w, 'health-check-failed');
      throw new Error(`worker health check failed: ${e.message}`);
    }
  }

  async _healthCheck(w) {
    const r = await w.exec('echo __MUSE_WARMUP_OK__', {
      cwd: IS_WIN ? process.env.SystemRoot || 'C:\\Windows' : '/tmp',
      timeoutMs: 8000,
    }).catch(() => null);
    if (!r || !r.stdout.includes('__MUSE_WARMUP_OK__')) {
      throw new Error('warmup sentinel not echoed');
    }
  }

  /** worker 死亡/被毁后的替换：摘除 + 异步预热替补 */
  _replaceWorker(worker, reason) {
    const i = this.workers.indexOf(worker);
    if (i >= 0) this.workers.splice(i, 1);
    worker.destroy();
    // 异步补充，不阻塞调用方
    this.warming += 1;
    this._spawnAndAdd(worker.shell)
      .catch((e) => console.error(`[shell-pool] warmup replacement failed (${reason}): ${e.message}`))
      .finally(() => { this.warming -= 1; });
  }

  _acquire(shell) {
    // 1) 同类型空闲
    let w = this.workers.find((x) => !x.busy && !x.dead && x.shell === shell && x.uses < this.maxUses);
    // 2) 任意空闲（调用方未指定 shell 时）
    if (!w && !shell) w = this.workers.find((x) => !x.busy && !x.dead && x.uses < this.maxUses);
    return w || null;
  }

  /**
   * 执行命令。
   * @param {string} command 用户命令
   * @param {object} opts { shell:'powershell'|'cmd', cwd, timeoutMs, isolated }
   */
  async exec(command, opts = {}) {
    const shell = opts.shell || 'powershell';
    const cwd = opts.cwd || (IS_WIN ? (process.env.USERPROFILE || 'C:\\') : os.homedir());
    const timeoutMs = opts.timeoutMs ?? this.defaultTimeoutMs;

    if (opts.isolated) return this.execIsolated(command, { shell, cwd, timeoutMs });

    const w = this._acquire(opts.shell /* 显式指定才严格匹配 */ ? shell : undefined)
      || this._acquire(shell);
    if (!w) {
      const e = new Error('no idle shell worker available');
      e.code = 'SHELL_POOL_EXHAUSTED';
      throw e;
    }
    // 绑定一次性 onUnexpectedExit：若执行期间 worker 意外死亡，触发替换
    const prevHook = w.onUnexpectedExit;
    w.onUnexpectedExit = (worker) => this._replaceWorker(worker, 'died-during-exec');
    try {
      const res = await w.exec(command, { cwd, timeoutMs });
      // 复用次数到上限 → 主动 retire
      if (w.uses >= this.maxUses) this._replaceWorker(w, 'max-uses-retire');
      else w.onUnexpectedExit = prevHook;
      return res;
    } catch (e) {
      // 超时/写失败：worker 已在内部 destroy，这里只做替换
      if (w.dead) this._replaceWorker(w, e.code || 'exec-failed');
      else w.onUnexpectedExit = prevHook;
      throw e;
    }
  }

  /** isolated: true → 一次性子进程，不走池（DESIGN.md §4） */
  execIsolated(command, { shell = 'powershell', cwd, timeoutMs = 30000 } = {}) {
    return new Promise((resolve, reject) => {
      const reqId = newReqId();
      const wrapped = shell === 'powershell'
        ? wrapPowerShell(reqId, cwd, command)
        : wrapCmd(reqId, cwd, command);
      const exe = shell === 'powershell' ? 'powershell.exe' : 'cmd.exe';
      const args = shell === 'powershell' ? ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', '-'] : [];
      const proc = spawn(exe, args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
      let out = '', err = '';
      const timer = setTimeout(() => {
        try { if (IS_WIN) execFile('taskkill', ['/F', '/T', '/PID', String(proc.pid)], () => {}); } catch (_) {}
        try { proc.kill('SIGKILL'); } catch (_) {}
        reject(Object.assign(new Error('isolated command timed out'), { code: 'SHELL_TIMEOUT' }));
      }, timeoutMs);
      proc.stdout.on('data', (d) => { out += d.toString('utf8'); });
      proc.stderr.on('data', (d) => { err += d.toString('utf8'); });
      proc.on('error', (e) => { clearTimeout(timer); reject(e); });
      proc.on('close', (code) => {
        clearTimeout(timer);
        // 从哨兵提取 exitCode（isolated 进程退出即结束，无需双流等待）
        const m = out.match(new RegExp(`__MUSE_OUT_END_${reqId}:(-?\\d+)__`));
        resolve({
          stdout: stripSentinels(out, reqId),
          stderr: stripSentinels(err, reqId),
          exitCode: m ? parseInt(m[1], 10) : code,
          isolated: true,
        });
      });
      proc.stdin.write(wrapped, 'utf8');
      proc.stdin.end();
    });
  }

  stats() {
    return {
      total: this.workers.length,
      idle: this.workers.filter((w) => !w.busy && !w.dead).length,
      busy: this.workers.filter((w) => w.busy).length,
      warming: this.warming,
      byShell: {
        powershell: this.workers.filter((w) => w.shell === 'powershell').length,
        cmd: this.workers.filter((w) => w.shell === 'cmd').length,
      },
    };
  }

  async shutdown() {
    const ws = this.workers.splice(0);
    ws.forEach((w) => { try { w.destroy(); } catch (_) {} });
  }
}

function stripSentinels(text, reqId) {
  return text
    .split('\n')
    .filter((l) => !l.includes(`__MUSE_OUT_BEGIN_${reqId}__`)
      && !l.includes(`__MUSE_ERR_BEGIN_${reqId}__`)
      && !l.match(new RegExp(`__MUSE_OUT_END_${reqId}:?-?\\d*__`))
      && !l.includes(`__MUSE_ERR_END_${reqId}__`))
    .join('\n')
    .trim();
}

/* ------------------------------------------------------------------ */
/* 自检（Windows 真机运行）                                              */
/* ------------------------------------------------------------------ */
async function selftest() {
  if (!IS_WIN) {
    console.log('[selftest] not Windows — skipping live tests (run on RD-928/homepc).');
    return;
  }
  const pool = new ShellPool({ powershell: 2, cmd: 2, timeoutMs: 15000 });
  console.log('[selftest] init pool...');
  await pool.init();
  console.log('[selftest] pool stats:', JSON.stringify(pool.stats()));

  const home = process.env.USERPROFILE;
  // 1. 基础 echo（powershell）
  let r = await pool.exec('echo hello-pool', { shell: 'powershell', cwd: home });
  console.log('[1] ps echo:', JSON.stringify({ ec: r.exitCode, out: r.stdout.trim(), ms: r.durationMs }));
  if (r.exitCode !== 0 || r.stdout.trim() !== 'hello-pool') throw new Error('case 1 failed');

  // 2. cmd echo
  r = await pool.exec('echo hello-cmd', { shell: 'cmd', cwd: home });
  console.log('[2] cmd echo:', JSON.stringify({ ec: r.exitCode, out: r.stdout.trim(), ms: r.durationMs }));
  if (r.exitCode !== 0 || r.stdout.trim() !== 'hello-cmd') throw new Error('case 2 failed');

  // 3. 中文输出（UTF-8）
  r = await pool.exec('echo 中文测试', { shell: 'powershell', cwd: home });
  console.log('[3] utf8:', JSON.stringify({ out: r.stdout.trim() }));
  if (!r.stdout.includes('中文测试')) throw new Error('case 3 failed: mojibake');

  // 4. cwd 切换隔离
  r = await pool.exec('(Get-Location).Path', { shell: 'powershell', cwd: 'C:\\Windows' });
  const loc1 = r.stdout.trim();
  r = await pool.exec('(Get-Location).Path', { shell: 'powershell', cwd: home });
  const loc2 = r.stdout.trim();
  console.log('[4] cwd isolation:', JSON.stringify({ loc1, loc2 }));
  if (!loc1.toLowerCase().includes('windows')) throw new Error('case 4 failed');

  // 5. 退出码透传
  r = await pool.exec('exit 42', { shell: 'powershell', cwd: home });
  console.log('[5] exit code:', r.exitCode);
  if (r.exitCode !== 42) throw new Error('case 5 failed');

  // 6. stderr 分离
  r = await pool.exec('[Console]::Error.WriteLine("boom-err")', { shell: 'powershell', cwd: home });
  console.log('[6] stderr:', JSON.stringify({ out: r.stdout.trim(), err: r.stderr.trim() }));
  if (!r.stderr.includes('boom-err')) throw new Error('case 6 failed');

  // 7. 超时自愈：Start-Sleep 30，超时 2s → worker 被销毁并补充
  const before = pool.stats().total;
  try {
    await pool.exec('Start-Sleep -Seconds 30', { shell: 'powershell', cwd: home, timeoutMs: 2000 });
    throw new Error('case 7 failed: should have timed out');
  } catch (e) {
    if (e.code !== 'SHELL_TIMEOUT') throw new Error(`case 7 failed: wrong code ${e.code}`);
    console.log('[7] timeout ok:', e.code);
  }
  await new Promise((res) => setTimeout(res, 3000)); // 等替补预热
  const after = pool.stats();
  console.log('[7] pool after heal:', JSON.stringify(after));
  if (after.total < before) throw new Error('case 7 failed: pool not replenished');

  // 8. 池化后命令仍正常（复用 worker）
  r = await pool.exec('echo after-heal', { shell: 'powershell', cwd: home });
  if (r.stdout.trim() !== 'after-heal') throw new Error('case 8 failed');
  console.log('[8] post-heal exec ok,', r.durationMs, 'ms');

  // 9. 并发 4 路
  const rs = await Promise.all([
    pool.exec('echo c1', { shell: 'powershell', cwd: home }),
    pool.exec('echo c2', { shell: 'cmd', cwd: home }),
    pool.exec('echo c3', { shell: 'powershell', cwd: home }),
    pool.exec('echo c4', { shell: 'cmd', cwd: home }),
  ]);
  console.log('[9] concurrent:', rs.map((x) => x.stdout.trim()).join(','));
  if (rs.some((x) => x.exitCode !== 0)) throw new Error('case 9 failed');

  await pool.shutdown();
  console.log('[selftest] ALL PASSED');
}

module.exports = { ShellPool, ShellWorker, wrapPowerShell, wrapCmd, newReqId };

if (require.main === module) {
  selftest().catch((e) => { console.error('[selftest] FAILED:', e); process.exit(1); });
}
