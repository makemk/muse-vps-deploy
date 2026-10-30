#!/usr/bin/env python3
"""VM-side monitor for mused daemon. Cron: every minute.

Checks:
  1. Process alive (pidfile, fallback pgrep)
  2. Unix socket responsive (health op, 2s timeout)
  3. Memory RSS + FD count from /proc
  4. Stats op -> QPS, latency P50/P95/P99, error rate, reconnects
  5. Threshold evaluation -> alerts via alert.py

Stdlib only. Exits 0 always (cron-friendly); failures become alerts.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alert import Alerter

HOME = os.path.expanduser("~")
MUSED_DIR = os.path.join(HOME, ".local", "share", "mused")
SOCKET_PATH = os.path.join(MUSED_DIR, "mused.sock")
PIDFILE = os.path.join(MUSED_DIR, "mused.pid")
STATE_PATH = os.path.join(MUSED_DIR, "monitor_state.json")
ALERTS_LOG = os.path.join(MUSED_DIR, "alerts.log")
HOOK_PATH = os.path.join(MUSED_DIR, "alert_hook.sh")
PREV_STATS_PATH = os.path.join(MUSED_DIR, "monitor_prev.json")

HDR = struct.Struct("!I")
SOCK_TIMEOUT = 2.0
CONSECUTIVE_FAILS = 2  # daemon_down needs 2 consecutive failures

# Thresholds
MEM_WARN_MB = 100
FD_WARN = 200
ERR_RATE_WARN = 0.05      # 5%
P99_WARN_MS = 2000
RECONNECT_WARN = 10       # per 5 min
UPTIME_RESTART_WARN_S = 120


def find_pid():
    """Return daemon pid or None."""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # exists?
        return pid
    except (OSError, ValueError):
        pass
    # fallback: pgrep
    try:
        out = subprocess.run(
            ["pgrep", "-f", "mused.py"], capture_output=True,
            text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            try:
                return int(line.strip())
            except ValueError:
                continue
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def proc_metrics(pid):
    """Return (rss_mb, fd_count) or (None, None)."""
    rss_mb, fd_count = None, None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) / 1024
                    break
    except OSError:
        pass
    try:
        fd_count = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        pass
    return rss_mb, fd_count


def socket_op(op, timeout=SOCK_TIMEOUT):
    """Send {"op": ...} to daemon, return parsed response or None."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
        blob = json.dumps({"op": op}).encode()
        s.sendall(HDR.pack(len(blob)) + blob)
        hdr = b""
        while len(hdr) < HDR.size:
            chunk = s.recv(HDR.size - len(hdr))
            if not chunk:
                return None
            hdr += chunk
        (n,) = HDR.unpack(hdr)
        if n > 16 * 1024 * 1024:
            return None
        buf = b""
        while len(buf) < n:
            chunk = s.recv(min(65536, n - len(buf)))
            if not chunk:
                return None
            buf += chunk
        s.close()
        return json.loads(buf.decode())
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def load_prev():
    try:
        with open(PREV_STATS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prev(data):
    try:
        tmp = PREV_STATS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PREV_STATS_PATH)
    except OSError:
        pass


def main():
    os.makedirs(MUSED_DIR, exist_ok=True)
    alerter = Alerter(STATE_PATH, ALERTS_LOG, HOOK_PATH, host="vm")
    now = int(time.time())

    # --- 1. process alive ---
    pid = find_pid()
    # consecutive-failure tracking for daemon_down
    fail_state_path = os.path.join(MUSED_DIR, "monitor_failcount.json")
    try:
        with open(fail_state_path) as f:
            fail_count = json.load(f).get("down_fails", 0)
    except (OSError, ValueError):
        fail_count = 0

    if pid is None:
        fail_count += 1
    else:
        fail_count = 0
    try:
        with open(fail_state_path, "w") as f:
            json.dump({"down_fails": fail_count}, f)
    except OSError:
        pass

    alerter.check(
        "daemon_down", "CRITICAL",
        fail_count >= CONSECUTIVE_FAILS,
        f"mused process not found ({fail_count} consecutive checks)",
        {"pidfile": PIDFILE},
    )
    if pid is None:
        alerter.flush()
        return 0

    # --- 2. socket health ---
    health = socket_op("health")
    socket_ok = bool(health and health.get("status") == "ok")
    alerter.check(
        "daemon_socket_dead", "CRITICAL",
        not socket_ok,
        "mused socket not responding to health op",
        {"socket": SOCKET_PATH},
    )

    # --- 3. process metrics ---
    rss_mb, fd_count = proc_metrics(pid)
    if rss_mb is not None:
        alerter.check(
            "daemon_mem_high", "WARNING", rss_mb > MEM_WARN_MB,
            f"mused RSS {rss_mb:.1f}MB > {MEM_WARN_MB}MB",
            {"rss_mb": round(rss_mb, 1), "pid": pid},
        )
    if fd_count is not None:
        alerter.check(
            "daemon_fd_high", "WARNING", fd_count > FD_WARN,
            f"mused FD count {fd_count} > {FD_WARN}",
            {"fd_count": fd_count, "pid": pid},
        )

    # --- 4. stats: QPS, latency, errors, reconnects ---
    uptime_s = health.get("uptime_s") if health else None
    if uptime_s is not None and uptime_s < UPTIME_RESTART_WARN_S:
        # possible restart loop: check 3 consecutive low-uptime readings
        try:
            with open(fail_state_path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        low = d.get("low_uptime", 0) + 1
        d["low_uptime"] = low
        d["down_fails"] = fail_count
        try:
            with open(fail_state_path, "w") as f:
                json.dump(d, f)
        except OSError:
            pass
        alerter.check(
            "daemon_restart_loop", "WARNING", low >= 3,
            f"mused uptime only {uptime_s}s ({low} consecutive checks)",
            {"uptime_s": uptime_s},
        )
    else:
        try:
            with open(fail_state_path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d["low_uptime"] = 0
        d["down_fails"] = fail_count
        try:
            with open(fail_state_path, "w") as f:
                json.dump(d, f)
        except OSError:
            pass
        alerter.resolve("daemon_restart_loop")

    stats = socket_op("stats")
    if stats:
        prev = load_prev()
        dt = now - prev.get("ts", now - 60)
        dt = max(dt, 1)

        # QPS + error rate from counter deltas
        total = stats.get("req_total", 0)
        prev_total = prev.get("req_total", total)
        d_total = max(total - prev_total, 0)
        qps = d_total / dt

        err_5xx = stats.get("req_5xx", 0)
        err_tr = stats.get("req_transport_err", 0)
        d_err = max((err_5xx + err_tr) - (prev.get("req_5xx", 0)
                                          + prev.get("req_transport_err", 0)), 0)
        err_rate = (d_err / d_total) if d_total > 0 else 0.0

        alerter.check(
            "daemon_err_rate", "WARNING", d_total >= 10 and err_rate > ERR_RATE_WARN,
            f"mused error rate {err_rate:.1%} over ~{dt}s "
            f"({d_err}/{d_total} failed)",
            {"err_rate": round(err_rate, 4), "qps": round(qps, 2),
             "window_s": dt},
        )

        # P99 latency (daemon-reported rolling window)
        p99 = stats.get("lat_p99_ms")
        if p99 is not None:
            alerter.check(
                "daemon_p99_high", "WARNING", p99 > P99_WARN_MS,
                f"mused P99 latency {p99}ms > {P99_WARN_MS}ms",
                {"lat_p50_ms": stats.get("lat_p50_ms"),
                 "lat_p95_ms": stats.get("lat_p95_ms"),
                 "lat_p99_ms": p99},
            )

        # reconnect storm
        rc = stats.get("reconnect_count", 0)
        d_rc = max(rc - prev.get("reconnect_count", rc), 0)
        # scale to 5-min window
        rc_5min = d_rc * (300 / dt)
        alerter.check(
            "daemon_reconnect_storm", "WARNING", rc_5min > RECONNECT_WARN,
            f"mused reconnects ~{rc_5min:.0f}/5min > {RECONNECT_WARN}",
            {"reconnect_5min": round(rc_5min, 1)},
        )

        save_prev({
            "ts": now,
            "req_total": total,
            "req_5xx": err_5xx,
            "req_transport_err": err_tr,
            "reconnect_count": rc,
            "qps": round(qps, 2),
        })
    # else: stats op not implemented yet in daemon -> skip silently

    alerter.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
