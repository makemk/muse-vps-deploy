#!/usr/bin/env python3
"""Prototype: VM -> VPS /muse-api/ over HTTPS via egress proxy (no SSH).

Chain per connection:
  TCP -> egress proxy -> CONNECT 38.207.167.51:443 (Basic auth)
  -> TLS handshake with SNI=ai.vps38.local, verified against VPS38 Root CA
  -> GET /muse-api/status with X-Bridge-Token header

Two sequential requests on one connection object prove keep-alive reuse.
Token comes from MUSE_API_TOKEN env (use a wrong one to prove the link:
expect HTTP 401 from the bridge, not a network error).
"""
import base64
import http.client
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlparse

VPS_IP = "38.207.167.51"
VPS_PORT = 443
SNI_HOST = "ai.vps38.local"
API_PATH = "/muse-api/status"
CA_PEM = "/home/hatch/workspace/muse-bridge/src/muse/vps-root-ca.pem"
TIMEOUT = 10


def parse_proxy():
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
    """HTTPSConnection tunneled through an HTTP CONNECT proxy, with a TLS
    SNI independent of the destination IP (needed: nginx routes on SNI)."""

    def __init__(self, host, port=443, *, proxy_host, proxy_port,
                 proxy_auth, sni, context, timeout=TIMEOUT):
        super().__init__(host, port, context=context, timeout=timeout)
        self._proxy = (proxy_host, proxy_port)
        self._proxy_auth = proxy_auth
        self._sni = sni
        self.connect_count = 0  # incremented per TCP+TLS establishment

    def connect(self):
        # 1. TCP to the egress proxy
        sock = socket.create_connection(self._proxy, timeout=self.timeout)
        try:
            # 2. CONNECT tunnel to VPS:443
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
            # 3. TLS over the tunnel, fixed SNI, verified against VPS CA
            self.sock = self._context.wrap_socket(
                sock, server_hostname=self._sni)
        except Exception:
            sock.close()
            raise
        self.connect_count += 1


def main():
    token = os.environ.get("MUSE_API_TOKEN", "")
    if not token:
        print("MUSE_API_TOKEN not set", file=sys.stderr)
        return 2
    proxy_host, proxy_port, proxy_auth = parse_proxy()
    ctx = ssl.create_default_context(cafile=CA_PEM)  # verify VPS cert

    conn = ProxyHTTPSConnection(
        VPS_IP, VPS_PORT,
        proxy_host=proxy_host, proxy_port=proxy_port, proxy_auth=proxy_auth,
        sni=SNI_HOST, context=ctx, timeout=TIMEOUT)

    headers = {
        "Host": SNI_HOST,               # match the nginx server block
        "X-Bridge-Token": token,
        "Connection": "keep-alive",
        "User-Agent": "museapi-proto/0.1",
    }
    try:
        for i in (1, 2):
            t0 = time.monotonic()
            conn.request("GET", API_PATH, headers=headers)
            resp = conn.getresponse()
            body = resp.read()  # must fully drain for keep-alive reuse
            dt = (time.monotonic() - t0) * 1000
            print(f"[{i}] HTTP {resp.status} {resp.reason} "
                  f"({dt:.0f} ms) body={body[:160]!r}")
    finally:
        print(f"TCP+TLS establishments: {conn.connect_count} "
              f"(1 => keep-alive reused the connection)")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
