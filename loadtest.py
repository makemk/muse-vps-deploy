#!/usr/bin/env python3
"""Load test: VM -> VPS /muse-api/ over HTTPS (no SSH).

Measures steady-state throughput and latency of the new HTTPS path.
Two test modes:
  status  - GET /muse-api/status   (VPS HTTP layer only, no PC involved)
  cmd     - POST /muse-api/cmd     (end-to-end: VPS -> WSS -> PC -> execute)

An optional --baseline-ssh mode runs the same workload through the legacy
SSH transport (~/workspace/bin/musepc) so you can compare numbers directly.

Each worker thread holds ONE keep-alive HTTPS connection (mirrors what the
mused daemon will do in production) and loops until --duration elapses.

Stdlib only. Do NOT run against the VPS before the auth patch is deployed
(the requests will 401/404 and the numbers are meaningless).

Usage:
  MUSE_API_TOKEN=$(cat ~/.config/mused/token) python3 loadtest.py \
      --mode status --concurrency 50 --duration 60 --output results.csv

  # end-to-end through RD-928:
  MUSE_API_TOKEN=$(cat ~/.config/mused/token) python3 loadtest.py \
      --mode cmd --target RD-928 --concurrency 10 --duration 60

  # SSH baseline for comparison:
  python3 loadtest.py --baseline-ssh --concurrency 10 --duration 60
"""
import argparse
import base64
import csv
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

VPS_IP = "38.207.167.51"
VPS_PORT = 443
SNI_HOST = "ai.vps38.local"
CA_PEM = "/home/hatch/workspace/muse-bridge/src/muse/vps-root-ca.pem"
MUSEPC_WRAPPER = "/home/hatch/workspace/bin/musepc"

STATUS_PATH = "/muse-api/status"
CMD_PATH = "/muse-api/cmd"

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 30


# ---------------------------------------------------------------- connection

def _parse_proxy():
    raw = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if not raw:
        raise RuntimeError("https_proxy / HTTPS_PROXY not set")
    u = urlparse(raw)
    auth = None
    if u.username:
        auth = base64.b64encode(
            f"{u.username}:{u.password or ''}".encode()).decode()
    return u.hostname, u.port or 3128, auth


class ProxyHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection through an HTTP CONNECT proxy with fixed SNI."""

    def __init__(self, host, port=443, *, proxy_host, proxy_port,
                 proxy_auth, sni, context, timeout=CONNECT_TIMEOUT):
        super().__init__(host, port, context=context, timeout=timeout)
        self._proxy = (proxy_host, proxy_port)
        self._proxy_auth = proxy_auth
        self._sni = sni

    def connect(self):
        sock = socket.create_connection(self._proxy, timeout=self.timeout)
        try:
            req = (f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                   f"Host: {self.host}:{self.port}\r\n")
            if self._proxy_auth:
                req += f"Proxy-Authorization: Basic {self._proxy_auth}\r\n"
            req += "\r\n"
            sock.sendall(req.encode("ascii"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError(
                        "proxy closed connection during CONNECT")
                resp += chunk
                if len(resp) > 65536:
                    raise ConnectionError("proxy CONNECT response too large")
            status_line = resp.split(b"\r\n", 1)[0].decode("latin1")
            if not (status_line.startswith("HTTP/1.1 200")
                    or status_line.startswith("HTTP/1.0 200")):
                raise ConnectionError(f"CONNECT rejected: {status_line}")
            self.sock = self._context.wrap_socket(
                sock, server_hostname=self._sni)
        except Exception:
            sock.close()
            raise


def _ssl_context():
    ctx = ssl.create_default_context(cafile=CA_PEM)
    return ctx


# ---------------------------------------------------------------- workers

class Result:
    __slots__ = ("ok", "latency_ms", "status", "error", "t_wall")

    def __init__(self, ok, latency_ms, status, error="", t_wall=None):
        self.ok = ok
        self.latency_ms = latency_ms
        self.status = status
        self.error = error
        # wall-clock completion time; defaults to now so call sites stay clean
        self.t_wall = t_wall if t_wall is not None else time.time()


def _https_worker(args, stop_at, results, token):
    """One worker: single keep-alive connection, loop until stop_at."""
    proxy_host, proxy_port, proxy_auth = _parse_proxy()
    ctx = _ssl_context()
    headers = {
        "Host": SNI_HOST,
        "Authorization": f"Bearer {token}",
        "Connection": "keep-alive",
        "User-Agent": "muse-loadtest/1.0",
    }

    conn = ProxyHTTPSConnection(
        VPS_IP, VPS_PORT,
        proxy_host=proxy_host, proxy_port=proxy_port, proxy_auth=proxy_auth,
        sni=SNI_HOST, context=ctx, timeout=CONNECT_TIMEOUT)

    if args.mode == "cmd":
        headers["Content-Type"] = "application/json"
        body = json.dumps({
            "target": args.target,
            "command": "echo loadtest-ping",
            "wait": True,
        }).encode()

    try:
        while time.monotonic() < stop_at:
            t0 = time.monotonic()
            try:
                if args.mode == "status":
                    conn.request("GET", STATUS_PATH, headers=headers)
                else:
                    conn.request("POST", CMD_PATH, body=body, headers=headers)
                # bound the read so a hung VPS can't stall the worker forever
                conn.sock.settimeout(READ_TIMEOUT)
                resp = conn.getresponse()
                payload = resp.read()
                dt = (time.monotonic() - t0) * 1000.0
                ok = resp.status == 200
                results.append(Result(ok, dt, resp.status,
                                      "" if ok else payload[:120].decode(
                                          "utf-8", "replace")))
            except (socket.timeout, TimeoutError) as e:
                dt = (time.monotonic() - t0) * 1000.0
                results.append(Result(False, dt, -1, f"timeout: {e}"))
                # connection is suspect after a timeout: rebuild it
                try:
                    conn.close()
                except Exception:
                    pass
                conn = ProxyHTTPSConnection(
                    VPS_IP, VPS_PORT, proxy_host=proxy_host,
                    proxy_port=proxy_port, proxy_auth=proxy_auth,
                    sni=SNI_HOST, context=ctx, timeout=CONNECT_TIMEOUT)
            except Exception as e:
                dt = (time.monotonic() - t0) * 1000.0
                results.append(Result(False, dt, -2, f"{type(e).__name__}: {e}"))
                # brief backoff before retrying on a fresh connection
                time.sleep(0.5)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = ProxyHTTPSConnection(
                    VPS_IP, VPS_PORT, proxy_host=proxy_host,
                    proxy_port=proxy_port, proxy_auth=proxy_auth,
                    sni=SNI_HOST, context=ctx, timeout=CONNECT_TIMEOUT)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ssh_worker(args, stop_at, results):
    """Baseline worker: one SSH call per iteration via musepc wrapper."""
    cmd = [MUSEPC_WRAPPER, "--target", args.target, "echo loadtest-ping"]
    while time.monotonic() < stop_at:
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=READ_TIMEOUT)
            dt = (time.monotonic() - t0) * 1000.0
            ok = p.returncode == 0
            results.append(Result(
                ok, dt, p.returncode,
                "" if ok else p.stderr[:120].decode("utf-8", "replace")))
        except subprocess.TimeoutExpired:
            dt = (time.monotonic() - t0) * 1000.0
            results.append(Result(False, dt, -1, "timeout"))
        except Exception as e:
            dt = (time.monotonic() - t0) * 1000.0
            results.append(Result(False, dt, -2, f"{type(e).__name__}: {e}"))


# ---------------------------------------------------------------- stats

def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def summarize(results, elapsed_s):
    ok_lat = sorted(r.latency_ms for r in results if r.ok)
    all_lat = sorted(r.latency_ms for r in results)
    n = len(results)
    n_ok = len(ok_lat)
    n_err = n - n_ok
    timeouts = sum(1 for r in results if r.status == -1)
    return {
        "total": n,
        "ok": n_ok,
        "errors": n_err,
        "timeouts": timeouts,
        "error_rate_pct": (n_err / n * 100.0) if n else 0.0,
        "qps": (n / elapsed_s) if elapsed_s > 0 else 0.0,
        "ok_qps": (n_ok / elapsed_s) if elapsed_s > 0 else 0.0,
        "mean_ms": (sum(all_lat) / len(all_lat)) if all_lat else 0.0,
        "p50_ms": _percentile(all_lat, 50),
        "p95_ms": _percentile(all_lat, 95),
        "p99_ms": _percentile(all_lat, 99),
        "max_ms": max(all_lat) if all_lat else 0.0,
        "ok_p50_ms": _percentile(ok_lat, 50),
        "ok_p95_ms": _percentile(ok_lat, 95),
        "ok_p99_ms": _percentile(ok_lat, 99),
    }


def print_report(summary, args):
    print()
    print("=" * 64)
    print(f"LOAD TEST  mode={args.mode}  concurrency={args.concurrency}  "
          f"duration={args.duration}s")
    if args.mode == "cmd" or args.baseline_ssh:
        print(f"           target={args.target}")
    print("=" * 64)
    print(f"  total requests : {summary['total']}")
    print(f"  ok             : {summary['ok']}")
    print(f"  errors         : {summary['errors']} "
          f"({summary['error_rate_pct']:.2f}%)")
    print(f"  timeouts       : {summary['timeouts']}")
    print(f"  throughput     : {summary['qps']:.1f} req/s "
          f"(ok: {summary['ok_qps']:.1f} req/s)")
    print(f"  latency mean   : {summary['mean_ms']:.0f} ms")
    print(f"  latency p50    : {summary['p50_ms']:.0f} ms")
    print(f"  latency p95    : {summary['p95_ms']:.0f} ms")
    print(f"  latency p99    : {summary['p99_ms']:.0f} ms")
    print(f"  latency max    : {summary['max_ms']:.0f} ms")
    print("=" * 64)


def write_csv(path, results, t_start):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_time", "rel_time_s", "latency_ms", "ok",
                    "status", "error"])
        for r in results:
            w.writerow([
                f"{r.t_wall:.3f}",
                f"{r.t_wall - t_start:.3f}",
                f"{r.latency_ms:.1f}",
                int(r.ok), r.status, r.error,
            ])
    print(f"CSV written: {path} ({len(results)} rows)")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Load test VM->VPS /muse-api/ over HTTPS (stdlib only).")
    ap.add_argument("--mode", choices=["status", "cmd"], default="status",
                    help="status=VPS-only, cmd=end-to-end via PC (default: status)")
    ap.add_argument("--concurrency", type=int, default=10,
                    help="worker threads, each holds one keep-alive conn")
    ap.add_argument("--duration", type=int, default=60,
                    help="test duration in seconds (default: 60)")
    ap.add_argument("--target", default="RD-928",
                    help="PC target for --mode cmd (default: RD-928)")
    ap.add_argument("--token", default=None,
                    help="controller token (default: $MUSE_API_TOKEN)")
    ap.add_argument("--output", default=None,
                    help="CSV output path (default: no CSV)")
    ap.add_argument("--baseline-ssh", action="store_true",
                    help="run the SSH baseline instead (via musepc wrapper)")
    args = ap.parse_args()

    token = args.token or os.environ.get("MUSE_API_TOKEN", "")
    if not args.baseline_ssh and not token:
        print("error: MUSE_API_TOKEN not set (or pass --token)",
              file=sys.stderr)
        return 2
    if args.mode == "cmd" and not args.target:
        print("error: --mode cmd needs --target", file=sys.stderr)
        return 2

    if args.baseline_ssh:
        print(f"SSH baseline: {args.concurrency} workers x {args.duration}s, "
              f"target={args.target}")
    else:
        print(f"HTTPS load test: mode={args.mode}, {args.concurrency} workers "
              f"x {args.duration}s, target={args.target}")

    # warmup: one request to absorb first-connection cost out of the numbers
    if not args.baseline_ssh:
        print("warmup: single request ...", end=" ", flush=True)
        try:
            tmp = []
            _https_worker(
                argparse.Namespace(mode=args.mode, target=args.target),
                time.monotonic() + 30, tmp, token)
            print(f"done ({len(tmp)} req, "
                  f"{tmp[0].latency_ms:.0f} ms first)" if tmp else "done")
        except Exception as e:
            print(f"FAILED: {e}")
            print("Is the VPS deployed and reachable? Aborting.",
                  file=sys.stderr)
            return 1

    results = []

    # each worker appends to its own list, merged afterwards, to avoid
    # lock contention on the hot path
    worker_lists = [[] for _ in range(args.concurrency)]
    stop_at = time.monotonic() + args.duration
    t_start_mono = time.monotonic()
    t_start_wall = time.time()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = []
        for wl in worker_lists:
            if args.baseline_ssh:
                futs.append(pool.submit(_ssh_worker, args, stop_at, wl))
            else:
                futs.append(pool.submit(_https_worker, args, stop_at, wl,
                                        token))
        # progress ticker
        while any(not f.done() for f in futs):
            done = sum(len(wl) for wl in worker_lists)
            elapsed = time.monotonic() - t_start_mono
            print(f"\r  ... {elapsed:.0f}s elapsed, {done} requests",
                  end="", flush=True)
            time.sleep(2)
        for f in futs:
            f.result()  # re-raise worker exceptions
    elapsed = time.monotonic() - t_start_mono
    print()

    for wl in worker_lists:
        results.extend(wl)

    summary = summarize(results, elapsed)
    print_report(summary, args)

    if args.output:
        write_csv(args.output, results, t_start_wall)

    # also dump a JSON summary next to the CSV for machine consumption
    if args.output and args.output.endswith(".csv"):
        jpath = args.output[:-4] + "_summary.json"
        with open(jpath, "w") as f:
            json.dump({"args": vars(args), "summary": summary}, f, indent=2)
        print(f"summary JSON: {jpath}")

    return 0 if summary["error_rate_pct"] < 5.0 else 3


if __name__ == "__main__":
    sys.exit(main())
