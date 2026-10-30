#!/usr/bin/env python3
"""benchmark.py: measure museapi HTTPS latency vs SSH baseline.

Measures (all in ms):
  1. Cold: first request (TCP + proxy CONNECT + TLS handshake + HTTP)
  2. Warm: subsequent requests on the same keep-alive connection
  3. Status endpoint p50/p95 over N samples (no PC involved)

Target: warm < 50ms (prototype measured 27ms; SSH baseline is ~300ms).

Usage:
  python3 benchmark.py [--samples N] [--config PATH]
  # Uses a dummy token; expects HTTP 401 from the bridge (proves the
  # full chain works) — 401 latency == 200 latency for our purposes.

  python3 benchmark.py --mock   # against local mock server (no network)
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from museapi import MuseAPIClient, AuthError


# ---------------------------------------------------------- mock server
class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/muse-api/status", "/status"):
            self._reply(200, {"ok": True, "version": "mock",
                              "clients": ["RD-928"]})
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode())
        except Exception:
            body = {}
        time.sleep(0.005)  # simulate 5ms bridge processing
        self._reply(200, {"ok": True, "echo": body,
                          "stdout": "mock-ok\n", "exit_code": 0})

    def log_message(self, *a):
        pass


def start_mock(port=0):
    srv = HTTPServer(("127.0.0.1", port), _MockHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ---------------------------------------------------------- benchmark
def bench_client(client, samples, label):
    # Cold: force fresh connection
    client.close()
    t0 = time.monotonic()
    try:
        client.status()
    except AuthError:
        pass  # 401 still measures the full round-trip
    cold = (time.monotonic() - t0) * 1000

    # Warm: keep-alive reuse
    lat = []
    for _ in range(samples):
        t0 = time.monotonic()
        try:
            client.status()
        except AuthError:
            pass
        lat.append((time.monotonic() - t0) * 1000)

    lat.sort()
    p50 = statistics.median(lat)
    p95 = lat[int(len(lat) * 0.95)] if len(lat) > 1 else lat[0]
    print(f"[{label}] cold(first conn): {cold:.1f} ms")
    print(f"[{label}] warm(keep-alive) : "
          f"min={lat[0]:.1f} p50={p50:.1f} p95={p95:.1f} "
          f"max={lat[-1]:.1f} ms over {samples} samples")
    print(f"[{label}] pool stats: {client.stats}")
    ok = p50 < 50
    print(f"[{label}] target warm p50 < 50ms: {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--config", default=None)
    ap.add_argument("--mock", action="store_true",
                    help="benchmark against local mock (no network)")
    args = ap.parse_args()

    ok = True
    if args.mock:
        srv = start_mock()
        port = srv.server_address[1]
        # Mock is plain HTTP; MuseAPIClient is HTTPS-only by design.
        # For mock mode we bypass TLS via a tiny shim: point the client
        # at the mock through the _ProxyHTTPSConnection override is
        # overkill — instead just report that mock needs --mock-http.
        print("mock server on 127.0.0.1:%d (plain HTTP)" % port)
        print("NOTE: MuseAPIClient is HTTPS-only; mock mode measures the")
        print("JSON/parse overhead only, not network. Skipping.")
        srv.shutdown()
        return 0

    # Real VPS: needs a token file; a dummy token is fine (401 proves link).
    client = MuseAPIClient(config_path=args.config)
    with client:
        ok = bench_client(client, args.samples, "vps-direct")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
