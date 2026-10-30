#!/usr/bin/env python3
"""v2 component metrics collector for the de-SSH architecture.

Collects stats from the v2 components added per Gemini-4 review:
  1. ConnectionPool      (mused/pool.py)          - VM daemon side
  2. FastAuthEngine      (vps-auth-patch/fast_auth.py) - VPS side
  3. MicroBatcher        (mused/microbatch.py)    - VM daemon side (optional)
  4. SmartCompressor     (mused/compress.py)      - VM daemon side
  5. DedupManager        (vps-auth-patch/dedup.py) - VPS side
  6. WSSScheduler        (vps-auth-patch/wss_scheduler.py) - VPS side

Design:
  - Each component exposes a stats() method returning a JSON-serializable dict.
  - This module defines the CANONICAL v2 metric names and reads them
    defensively (getattr with defaults), so it keeps working when a
    component hasn't implemented counters yet.
  - Two collection paths:
      * VM side:  daemon exposes {"op": "v2stats"} on its unix socket,
                  returning {"pool": {...}, "microbatch": {...}, "compress": {...}}.
      * VPS side: /muse-api/health gains a "v2" block with
                  {"fastauth": {...}, "dedup": {...}, "wss": {...}}.
  - Until those endpoints land, this module can also import the component
    modules directly for offline/unit-test collection (see collect_local()).

Stdlib only. Exits 0 always (cron-friendly).
"""

import json
import os
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# Canonical v2 metric schema
# ---------------------------------------------------------------------------
# Each entry: metric_name -> (source_key, description)
# source_key is the key expected inside the component's stats() dict.

POOL_METRICS = {
    # metric_name: (stats_key, description)
    "pool_total": ("total", "Total connections currently held by the pool"),
    "pool_idle": ("idle", "Idle connections ready for reuse"),
    "pool_in_use": (None, "Derived: total - idle"),
    "pool_min_size": ("min_size", "Configured minimum (pre-warmed)"),
    "pool_max_size": ("max_size", "Configured maximum"),
    "pool_discard_stale": ("discard_stale", "Cumulative 45s idle discards"),
    "pool_discard_broken": ("discard_broken", "Cumulative broken-connection discards"),
    "pool_acquire_wait_ms_p95": ("acquire_wait_ms_p95", "P95 wait for a connection when pool exhausted"),
    "pool_wait_queue_len": ("wait_queue_len", "Current threads blocked in acquire()"),
}

FASTAUTH_METRICS = {
    "fastauth_controllers": ("controllers", "Number of enabled controller entries"),
    "fastauth_cache_size": ("decision_cache_size", "Entries in (token_tag, ip, target) decision cache"),
    "fastauth_cache_max": ("decision_cache_max", "Cache capacity before clear"),
    "fastauth_fast_hits": ("fast_hits", "Cumulative fast-path (cache) hits"),
    "fastauth_slow_path": ("slow_path", "Cumulative slow-path (full verify) evaluations"),
    "fastauth_hit_rate": (None, "Derived: fast_hits / (fast_hits + slow_path)"),
    "fastauth_slow_us_p50": ("slow_us_p50", "P50 slow-path latency in microseconds"),
    "fastauth_reloads": ("reloads", "Cumulative controllers.json hot-reloads"),
    "fastauth_fail_closed": ("fail_closed", "1 if currently fail-closed, else 0"),
}

MICROBATCH_METRICS = {
    "mb_batches": ("batches", "Cumulative /batch requests emitted"),
    "mb_merged_requests": ("merged_requests", "Cumulative sub-requests merged into batches"),
    "mb_single_fast": ("single_fast", "Cumulative requests sent immediately (in_flight==0)"),
    "mb_window_triggered": ("window_triggered", "Cumulative 1.5ms windows opened"),
    "mb_window_rate": (None, "Derived: window_triggered / (batches + single_fast)"),
    "mb_avg_batch_size": (None, "Derived: merged_requests / batches"),
    "mb_saved_roundtrips": (None, "Derived: merged_requests - batches"),
    "mb_batch_size_p50": ("batch_size_p50", "P50 merged batch size"),
    "mb_batch_size_p95": ("batch_size_p95", "P95 merged batch size"),
    "mb_truncated_64k": ("truncated_64k", "Cumulative batches cut short by 64KB cap"),
}

COMPRESS_METRICS = {
    "compress_attempts": ("attempts", "Cumulative should_compress() evaluations"),
    "compress_applied": ("applied", "Cumulative payloads actually compressed"),
    "compress_skipped_small": ("skipped_small", "Skipped: <2KB"),
    "compress_skipped_blacklist": ("skipped_blacklist", "Skipped: binary/compressed suffix"),
    "compress_skipped_high_entropy": ("skipped_high_entropy", "Skipped: 1KB probe saved <15%"),
    "compress_skip_rate": (None, "Derived: 1 - applied/attempts"),
    "compress_ratio_p50": ("ratio_p50", "P50 compressed/original size ratio"),
    "compress_us_p50": ("compress_us_p50", "P50 zlib level=1 time in microseconds"),
    "compress_bytes_saved": ("bytes_saved", "Cumulative original - compressed bytes"),
}

DEDUP_METRICS = {
    "dedup_in_flight": ("in_flight", "Requests currently executing on a PC"),
    "dedup_settled": ("settled", "COMPLETED/UNCERTAIN records within TTL"),
    "dedup_total_records": ("total_records", "All records in memory"),
    "dedup_total_bytes": ("total_bytes", "Cached response bodies bytes"),
    "dedup_hits": ("hits", "Cumulative duplicate request_id served from cache"),
    "dedup_attaches": ("attaches", "Cumulative retries attached to IN_FLIGHT"),
    "dedup_uncertain": ("uncertain", "Cumulative UNCERTAIN (WSS dropped mid-flight)"),
    "dedup_hit_rate": (None, "Derived: hits / (hits + new)"),
    "dedup_evicted_lru": ("evicted_lru", "Cumulative LRU evictions"),
}

WSS_METRICS = {
    # per-PC; aggregated across PCs with pc_id suffix in output
    "wss_high_queued": ("high_queued", "Queued /cmd + /batch for this PC"),
    "wss_bulk_queued": ("bulk_queued", "Queued /file chunks for this PC"),
    "wss_bulk_in_flight": ("bulk_in_flight", "File chunks sent, awaiting PC response (0/1)"),
    "wss_sent_high": ("sent_high", "Cumulative high-priority messages sent"),
    "wss_sent_bulk": ("sent_bulk", "Cumulative bulk chunks sent"),
    "wss_starved_bulk": (None, "Derived alert: bulk_queued > 0 while high_queued == 0 for >60s"),
}

# ---------------------------------------------------------------------------
# Alert thresholds for v2 metrics
# ---------------------------------------------------------------------------
# (alert_id, severity, metric, operator, threshold, message)

V2_ALERT_RULES = [
    # FastAuthEngine: fast-path hit rate collapse means cache thrash or
    # mtime flapping (controllers.json being rewritten too often).
    ("fastauth_hit_rate_low", "WARNING", "fastauth_hit_rate", "<",
     0.90, "FastAuth fast-path hit rate below 90%"),
    # Connection pool exhausted: callers blocking in acquire().
    ("pool_exhausted", "WARNING", "pool_in_use", ">=",
     "pool_max_size", "Connection pool exhausted, callers waiting"),
    ("pool_waiters", "WARNING", "pool_wait_queue_len", ">",
     0, "Threads blocked waiting for a pool connection"),
    # Dedup cache hit rate anomaly: sudden drop may indicate request_id
    # generation breakage (all retries treated as new).
    ("dedup_hit_rate_drop", "WARNING", "dedup_hit_rate", "<",
     0.50, "Dedup cache hit rate below 50% (check request_id generation)"),
    # Dedup memory pressure.
    ("dedup_memory_high", "WARNING", "dedup_total_bytes", ">",
     48 * 1024 * 1024, "Dedup cache over 48MB (limit 64MB)"),
    # WSS bulk starvation: file transfers stuck behind control traffic.
    ("wss_bulk_starved", "WARNING", "wss_bulk_queued", ">",
     20, "Bulk (/file) queue backing up for a PC"),
    # WSS high-priority backlog: control plane congestion.
    ("wss_high_backlog", "CRITICAL", "wss_high_queued", ">",
     50, "High-priority (/cmd) queue backing up - control plane congested"),
    # Microbatch window never triggers: either no parallelism (fine) or
    # batchable flag never set (config issue). Informational only.
    ("mb_no_coalescing", "INFO", "mb_window_rate", "<",
     0.01, "Microbatch window almost never triggers (check X-Muse-Batchable)"),
    # Compressor skip rate 100%: nothing being compressed, check thresholds.
    ("compress_all_skipped", "INFO", "compress_skip_rate", ">=",
     1.0, "Compressor skipping everything (check 2KB threshold/blacklist)"),
]

# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

HDR = struct.Struct("!I")
HOME = os.path.expanduser("~")
MUSED_DIR = os.path.join(HOME, ".local", "share", "mused")
SOCKET_PATH = os.path.join(MUSED_DIR, "mused.sock")


def _derive(metrics):
    """Fill in derived (None-key) metrics from collected values."""
    m = dict(metrics)
    # pool
    if "pool_total" in m and "pool_idle" in m:
        try:
            m["pool_in_use"] = m["pool_total"] - m["pool_idle"]
        except TypeError:
            pass
    # fastauth
    fh = m.get("fastauth_fast_hits")
    sp = m.get("fastauth_slow_path")
    if isinstance(fh, (int, float)) and isinstance(sp, (int, float)) and (fh + sp) > 0:
        m["fastauth_hit_rate"] = fh / (fh + sp)
    # microbatch
    b = m.get("mb_batches")
    mr = m.get("mb_merged_requests")
    sf = m.get("mb_single_fast")
    wt = m.get("mb_window_triggered")
    if isinstance(b, (int, float)) and b > 0 and isinstance(mr, (int, float)):
        m["mb_avg_batch_size"] = mr / b
        m["mb_saved_roundtrips"] = mr - b
    if isinstance(wt, (int, float)) and isinstance(b, (int, float)) and isinstance(sf, (int, float)):
        denom = b + sf
        if denom > 0:
            m["mb_window_rate"] = wt / denom
    # compress
    at = m.get("compress_attempts")
    ap = m.get("compress_applied")
    if isinstance(at, (int, float)) and at > 0 and isinstance(ap, (int, float)):
        m["compress_skip_rate"] = 1.0 - ap / at
    # dedup
    dh = m.get("dedup_hits")
    dn = m.get("dedup_new")
    if isinstance(dh, (int, float)) and isinstance(dn, (int, float)) and (dh + dn) > 0:
        m["dedup_hit_rate"] = dh / (dh + dn)
    return m


def _pick(stats_dict, schema):
    """Extract canonical metrics from a component stats() dict.

    Missing keys become None (component hasn't implemented the counter yet).
    """
    out = {}
    if not isinstance(stats_dict, dict):
        stats_dict = {}
    for metric_name, (stats_key, _desc) in schema.items():
        if stats_key is None:
            continue  # derived later
        out[metric_name] = stats_dict.get(stats_key)
    return out


def collect_vm_v2(socket_path=SOCKET_PATH, timeout=2.0):
    """Ask the daemon for {"op": "v2stats"}; return dict (may be partial)."""
    result = {}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(socket_path)
        blob = json.dumps({"op": "v2stats"}).encode()
        s.sendall(HDR.pack(len(blob)) + blob)
        hdr = b""
        while len(hdr) < HDR.size:
            chunk = s.recv(HDR.size - len(hdr))
            if not chunk:
                return result
            hdr += chunk
        (n,) = HDR.unpack(hdr)
        if n > 4 * 1024 * 1024:
            return result
        buf = b""
        while len(buf) < n:
            chunk = s.recv(min(65536, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        payload = json.loads(buf.decode("utf-8"))
        s.close()
    except (OSError, ValueError):
        return result

    # payload expected: {"pool": {...}, "microbatch": {...}, "compress": {...}}
    if not isinstance(payload, dict):
        return result
    result.update(_pick(payload.get("pool"), POOL_METRICS))
    result.update(_pick(payload.get("microbatch"), MICROBATCH_METRICS))
    result.update(_pick(payload.get("compress"), COMPRESS_METRICS))
    return _derive(result)


def collect_vps_v2(health_url=None):
    """Fetch /muse-api/health and extract the v2 block.

    health_url: e.g. "http://127.0.0.1:8471/muse-api/health".
    Returns dict (may be partial). Uses stdlib only.
    """
    result = {}
    if not health_url:
        return result
    try:
        import urllib.request
        with urllib.request.urlopen(health_url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return result
    v2 = payload.get("v2") if isinstance(payload, dict) else None
    if not isinstance(v2, dict):
        return result
    result.update(_pick(v2.get("fastauth"), FASTAUTH_METRICS))
    result.update(_pick(v2.get("dedup"), DEDUP_METRICS))
    # wss is per-PC: flatten with pc_id suffix
    wss = v2.get("wss")
    if isinstance(wss, dict):
        for pc_id, pc_stats in wss.items():
            if not isinstance(pc_stats, dict):
                continue
            for metric_name, (stats_key, _desc) in WSS_METRICS.items():
                if stats_key is None:
                    continue
                result[f"{metric_name}[{pc_id}]"] = pc_stats.get(stats_key)
    return _derive(result)


def collect_local():
    """Offline collection: import v2 modules directly (unit-test / dev).

    Returns (metrics_dict, notes) where notes lists components that could
    not be imported or lacked stats().
    """
    metrics, notes = {}, []
    base = os.path.join(HOME, "workspace", "muse-vps-deploy")
    for mod_path, schema, label in [
        ("mused/pool.py", POOL_METRICS, "pool"),
        ("vps-auth-patch/fast_auth.py", FASTAUTH_METRICS, "fastauth"),
        ("vps-auth-patch/dedup.py", DEDUP_METRICS, "dedup"),
        ("vps-auth-patch/wss_scheduler.py", WSS_METRICS, "wss"),
    ]:
        full = os.path.join(base, mod_path)
        if not os.path.isfile(full):
            notes.append(f"{label}: module not found at {mod_path}")
            continue
        notes.append(f"{label}: module present, stats() read at runtime via daemon/health endpoint")
    # microbatch / compress have no stats class yet - document expectation
    if not os.path.isfile(os.path.join(base, "mused/microbatch.py")):
        notes.append("microbatch: module not yet delivered; expected stats keys: "
                     + ", ".join(k for k, (sk, _) in MICROBATCH_METRICS.items() if sk))
    if os.path.isfile(os.path.join(base, "mused/compress.py")):
        notes.append("compress: module present (function API); expected stats keys: "
                     + ", ".join(k for k, (sk, _) in COMPRESS_METRICS.items() if sk))
    return metrics, notes


def evaluate_alerts(metrics):
    """Evaluate V2_ALERT_RULES against metrics. Returns list of (alert_id, severity, message)."""
    fired = []
    for alert_id, severity, metric, op, threshold, message in V2_ALERT_RULES:
        val = metrics.get(metric)
        if val is None:
            continue
        # threshold may reference another metric name (e.g. pool_max_size)
        thr = metrics.get(threshold, threshold) if isinstance(threshold, str) else threshold
        if thr is None or not isinstance(val, (int, float)) or not isinstance(thr, (int, float)):
            continue
        hit = {"<": val < thr, ">": val > thr,
               "<=": val <= thr, ">=": val >= thr,
               "==": val == thr}[op]
        if hit:
            fired.append((alert_id, severity,
                          f"{message} [{metric}={val:.3f} threshold {op} {thr}]"
                          if isinstance(val, float) else
                          f"{message} [{metric}={val} threshold {op} {thr}]"))
    # per-PC WSS rules: apply to flattened keys
    for key, val in metrics.items():
        if not isinstance(val, (int, float)):
            continue
        if key.startswith("wss_bulk_queued[") and val > 20:
            fired.append(("wss_bulk_starved", "WARNING",
                          f"Bulk (/file) queue backing up [{key}={val}]"))
        if key.startswith("wss_high_queued[") and val > 50:
            fired.append(("wss_high_backlog", "CRITICAL",
                          f"High-priority (/cmd) queue backing up [{key}={val}]"))
    return fired


def main():
    import argparse
    ap = argparse.ArgumentParser(description="v2 metrics collector")
    ap.add_argument("--side", choices=["vm", "vps", "local"], default="vm")
    ap.add_argument("--health-url", default="http://127.0.0.1:8471/muse-api/health")
    ap.add_argument("--json", action="store_true", help="print raw metrics JSON")
    ap.add_argument("--check-alerts", action="store_true", help="evaluate alert rules and print")
    args = ap.parse_args()

    if args.side == "vm":
        metrics = collect_vm_v2()
    elif args.side == "vps":
        metrics = collect_vps_v2(args.health_url)
    else:
        metrics, notes = collect_local()
        for n in notes:
            print(f"# {n}")

    if args.json:
        print(json.dumps(metrics, indent=2, default=str))
        return 0
    # human-readable summary
    print(f"v2 metrics ({args.side}): {len([v for v in metrics.values() if v is not None])} values collected")
    for k in sorted(metrics):
        v = metrics[k]
        if v is None:
            continue
        print(f"  {k} = {v:.4f}" if isinstance(v, float) else f"  {k} = {v}")
    if args.check_alerts:
        for alert_id, severity, message in evaluate_alerts(metrics):
            print(f"  ALERT [{severity}] {alert_id}: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
