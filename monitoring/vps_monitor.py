#!/usr/bin/env python3
"""VPS-side monitor for muse-bridge.py + nginx. Cron (root): every minute.

Checks:
  1. muse-bridge.py process alive, RSS, CPU%
  2. GET /muse-api/health (localhost) -> wss clients, pending, api counters
  3. 401/403 spike detection (from health counters, 1-min rate)
  4. WSS client list -> per-PC offline detection
  5. Threshold evaluation -> alerts via alert.py

Stdlib only. Exits 0 always. Deploy to /opt/muse-bridge/monitoring/.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alert import Alerter

STATE_DIR = "/var/log/muse-bridge"
STATE_PATH = os.path.join(STATE_DIR, "monitor_state.json")
ALERTS_LOG = os.path.join(STATE_DIR, "alerts.log")
HOOK_PATH = os.path.join(STATE_DIR, "alert_hook.sh")
PREV_PATH = os.path.join(STATE_DIR, "monitor_prev.json")
HEALTH_URL = "http://127.0.0.1:8471/muse-api/health"

# Thresholds
BRIDGE_MEM_WARN_MB = 500
BRIDGE_CPU_WARN_PCT = 80.0
DENIED_SPIKE_PER_MIN = 20   # 401 or 403
NO_PCS_CRITICAL_MIN = 2
PC_OFFLINE_WARN_MIN = 5
PENDING_WARN = 50


def find_bridge_pid():
    try:
        out = subprocess.run(
            ["pgrep", "-f", "muse-bridge.py"], capture_output=True,
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
    """Return (rss_mb, cpu_pct_1min_approx) via /proc. cpu needs 2 samples."""
    rss_mb = None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) / 1024
                    break
    except OSError:
        pass
    # cpu: read utime+stime jiffies; compare with prev sample in state file
    cpu_pct = None
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        jiffies = int(parts[13]) + int(parts[14])
        clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        prev = load_prev()
        pj, pt = prev.get("bridge_jiffies"), prev.get("ts")
        now = int(time.time())
        if pj is not None and pt and now > pt:
            cpu_pct = (jiffies - pj) / clk_tck / (now - pt) * 100
        prev["bridge_jiffies"] = jiffies
        save_prev(prev, ts=now)
    except (OSError, ValueError, IndexError):
        pass
    return rss_mb, cpu_pct


def fetch_health(timeout=5):
    try:
        req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "vps-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def load_prev():
    try:
        with open(PREV_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prev(data, ts=None):
    data = dict(data)
    data["ts"] = ts or int(time.time())
    try:
        tmp = PREV_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PREV_PATH)
    except OSError:
        pass


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    alerter = Alerter(STATE_PATH, ALERTS_LOG, HOOK_PATH, host="vps")
    now = int(time.time())

    # --- 1. bridge process ---
    pid = find_bridge_pid()
    try:
        with open(PREV_PATH) as f:
            d0 = json.load(f)
    except (OSError, ValueError):
        d0 = {}
    down_fails = d0.get("down_fails", 0)
    down_fails = down_fails + 1 if pid is None else 0

    alerter.check(
        "vps_bridge_down", "CRITICAL", down_fails >= 2,
        f"muse-bridge.py not found ({down_fails} consecutive checks)",
        {},
    )
    if pid is None:
        d0["down_fails"] = down_fails
        d0["ts"] = now
        save_prev(d0, ts=now)
        alerter.flush()
        return 0
    d0["down_fails"] = 0

    rss_mb, cpu_pct = proc_metrics(pid)
    if rss_mb is not None:
        alerter.check(
            "vps_mem_high", "WARNING", rss_mb > BRIDGE_MEM_WARN_MB,
            f"muse-bridge RSS {rss_mb:.0f}MB > {BRIDGE_MEM_WARN_MB}MB",
            {"rss_mb": round(rss_mb, 1), "pid": pid},
        )
    if cpu_pct is not None:
        alerter.check(
            "vps_cpu_high", "WARNING", cpu_pct > BRIDGE_CPU_WARN_PCT,
            f"muse-bridge CPU {cpu_pct:.0f}% > {BRIDGE_CPU_WARN_PCT}%",
            {"cpu_pct": round(cpu_pct, 1)},
        )

    # --- 2. health endpoint ---
    h = fetch_health()
    prev = load_prev()
    dt = max(now - prev.get("ts", now - 60), 1)

    if h and h.get("status") == "ok":
        api = h.get("api", {})

        # 401 / 403 spike (security signals)
        for code in ("http_401", "http_403"):
            cur = api.get(code, 0)
            rate = max(cur - prev.get(code, cur), 0) / dt * 60  # per min
            alert_id = "vps_401_spike" if code == "http_401" else "vps_403_spike"
            what = "auth failures (401)" if code == "http_401" else "forbidden (403)"
            alerter.check(
                alert_id, "WARNING", rate > DENIED_SPIKE_PER_MIN,
                f"VPS {what} {rate:.0f}/min > {DENIED_SPIKE_PER_MIN}/min "
                f"(possible attack or misconfig)",
                {f"{code}_per_min": round(rate, 1)},
            )
            prev[code] = cur

        # WSS clients
        n_clients = h.get("wss_clients", 0)
        no_pcs_min = prev.get("no_pcs_min", 0)
        no_pcs_min = no_pcs_min + dt / 60 if n_clients == 0 else 0
        prev["no_pcs_min"] = no_pcs_min
        alerter.check(
            "vps_no_pcs", "CRITICAL", no_pcs_min >= NO_PCS_CRITICAL_MIN,
            f"no WSS clients for {no_pcs_min:.1f} min (all PCs offline)",
            {"wss_clients": n_clients},
        )

        # pending futures buildup
        pending = h.get("pending_futures", 0)
        alerter.check(
            "vps_pending_buildup", "WARNING", pending > PENDING_WARN,
            f"{pending} pending PC requests > {PENDING_WARN} (results stuck)",
            {"pending_futures": pending},
        )

        # per-PC offline
        seen = {}
        for c in h.get("client_list", []):
            name = c.get("name")
            if name:
                seen[name] = c.get("last_seen", now)
        known = set(prev.get("known_pcs", [])) | set(seen)
        prev["known_pcs"] = sorted(known)
        for name in known:
            last = seen.get(name)
            if last is None:
                offline_min = (now - prev.get(f"pc_{name}_last", now)) / 60
                prev[f"pc_{name}_last"] = prev.get(f"pc_{name}_last", now)
            else:
                offline_min = 0
                prev[f"pc_{name}_last"] = last
            alerter.check(
                f"pc_offline_{name}", "WARNING",
                offline_min >= PC_OFFLINE_WARN_MIN,
                f"PC {name} offline {offline_min:.0f} min",
                {"pc": name, "offline_min": round(offline_min, 1)},
            )
    # else: health endpoint not implemented yet -> skip silently

    save_prev(prev, ts=now)
    alerter.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
