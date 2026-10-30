#!/usr/bin/env python3
"""mused: resident daemon, VM-side front for VPS /muse-api/ (no SSH).

- Holds one HTTPS keep-alive connection to the VPS (CONNECT+TLS via the
  egress proxy, SNI from config, cert verified against the VPS CA).
- Listens on a unix socket for JSON requests:
    {"token": "...", "method": "POST", "path": "/cmd", "body": {...}}
  and forwards them to https://<vps>/muse-api/<path>, returning the
  upstream response as JSON:
    {"status": 200, "body": "<raw body text>"}
- Heartbeat thread: GET /muse-api/status every heartbeat_interval seconds
  (401 also proves the link). On failure, reconnect with exponential
  backoff.

Config: ~/.config/mused/config.json
  {vps_host, vps_port, sni, ca_cert_path, token_file, socket_path,
   heartbeat_interval}
Token: read from token_file (600), never in the config JSON.
Logs: ~/.local/share/mused/mused.log (rotating, 10MB x 5).

SIGTERM/SIGINT: graceful shutdown — close listener, drop upstream
connection, remove the socket file.
"""
import base64
import http.client
import json
import logging
import logging.handlers
import os
import signal
import socket
import ssl
import struct
import sys
import threading
import zlib
from urllib.parse import urlparse

try:
    from microbatch import MicroBatcher, is_read_only
except ImportError:  # running from an installed location without the module
    MicroBatcher = None

    def is_read_only(method, path, body):  # noqa: D103
        return False

try:
    from tls_resume import ResumableHTTPSConnection, TLSSessionCache
except ImportError:  # allow running as `python -m` from parent dir
    from mused.tls_resume import ResumableHTTPSConnection, TLSSessionCache

try:
    from pool import ConnectionPool
except ImportError:  # allow running as `python -m` from parent dir
    from mused.pool import ConnectionPool

try:
    from compress import request_headers, response_encoding, decompress
except ImportError:  # allow running as `python -m` from parent dir
    from mused.compress import request_headers, response_encoding, decompress

# ---------------------------------------------------------------- config

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "mused")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_DIR = os.path.join(HOME, ".local", "share", "mused")
LOG_PATH = os.path.join(LOG_DIR, "mused.log")

DEFAULTS = {
    "vps_host": "38.207.167.51",
    "vps_port": 443,
    "sni": "ai.vps38.local",
    "ca_cert_path": os.path.join(
        HOME, "workspace", "muse-bridge", "src", "muse", "vps-root-ca.pem"),
    "token_file": os.path.join(CONFIG_DIR, "token"),
    "socket_path": "/tmp/mused.sock",
    "heartbeat_interval": 30,
    "log_level": "INFO",
    # Micro-batching (In-Flight Triggered Coalescing, see microbatch.py).
    # OFF by default: the VPS must first implement the multiplexing
    # POST /muse-api/batch wire format
    #   {"batch_id":..., "requests":[{"id","method","path","body"},...]}
    #   -> 200 [{"id","status","body"},...]
    # which is NOT the legacy shell {"commands":[...]} batch.
    "microbatch_enabled": False,
    # Application-layer end-to-end compression (see compress.py, Gemini-4 §5.3).
    # ON by default: payloads >= 2KB get zlib(level=1) with a
    # "Content-Encoding: deflate" header; the VPS passes bytes through
    # untouched. DEPLOYMENT NOTE: the PC plugin must implement the
    # decompression side (DECOMPRESS_SPEC.md, Phase 2) BEFORE this is
    # enabled in production, otherwise >2KB request bodies arrive at the
    # PC as opaque deflate bytes. Flip to false until the plugin lands it.
    "compress_enabled": True,
}

API_PREFIX = "/muse-api"
TIMEOUT = 10
BACKOFF_CAP = 30

# length-prefixed framing on the unix socket: 4-byte big-endian length
HDR = struct.Struct("!I")

log = logging.getLogger("mused")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("config root must be an object")
        for k, v in user.items():
            if k in DEFAULTS:
                cfg[k] = v
            else:
                log.warning("ignoring unknown config key: %s", k)
    except FileNotFoundError:
        log.info("no config at %s, using defaults", CONFIG_PATH)
    except (json.JSONDecodeError, ValueError) as e:
        log.error("bad config %s: %s -- using defaults", CONFIG_PATH, e)
    # expand ~ in paths
    for k in ("ca_cert_path", "token_file", "socket_path"):
        cfg[k] = os.path.expanduser(cfg[k])
    return cfg


def setup_logging(log_level="INFO"):
    os.makedirs(LOG_DIR, exist_ok=True)
    level = getattr(logging, str(log_level).upper(), None)
    if not isinstance(level, int):
        print(f"warning: bad log_level {log_level!r}, using INFO",
              file=sys.stderr)
        level = logging.INFO
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8")
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(threadName)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    # also echo to stderr so foreground runs stay visible;
    # under systemd, stderr is captured by journald (dual logging)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)


# ---------------------------------------------------------------- upstream

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


class ProxyHTTPSConnection(ResumableHTTPSConnection):
    """HTTPSConnection tunneled through an HTTP CONNECT proxy, TLS SNI
    independent of the destination IP (nginx routes on SNI).

    Inherits TLS session resumption from ResumableHTTPSConnection: the
    CONNECT tunnel is built first, then _tls_wrap() injects the cached
    session. The fresh session is extracted in getresponse() (inherited),
    never in connect() -- TLS 1.3 tickets arrive post-handshake.
    """

    def __init__(self, host, port=443, *, proxy_host, proxy_port,
                 proxy_auth, sni, context, timeout=TIMEOUT,
                 session_getter=None, session_setter=None,
                 handshake_stats=None):
        super().__init__(host, port, ssl_context=context,
                         session_getter=session_getter,
                         session_setter=session_setter,
                         timeout=timeout)
        self._proxy = (proxy_host, proxy_port)
        self._proxy_auth = proxy_auth
        self._sni = sni
        self.connect_count = 0
        if handshake_stats is not None:
            self.attach_handshake_stats(handshake_stats)

    def connect(self):
        sock = socket.create_connection(self._proxy, timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
            # TLS over the tunnel, with cached-session injection.
            # (Never extract sock.session here -- TLS 1.3 ticket arrives later.)
            self._tls_wrap(sock, self._sni)
        except Exception:
            sock.close()
            raise
        self.connect_count += 1


class Upstream:
    """Pool of keep-alive HTTPS connections to the VPS (no SSH).

    Holds min_size=4 .. max_size=16 pooled connections (Gemini-4 spec):
    pre-warmed at startup, idle>45s discarded (nginx keepalive_timeout=65s
    race), callers block up to 5s when exhausted.
    """

    def __init__(self, cfg):
        self._cfg = cfg
        proxy_host, proxy_port, proxy_auth = parse_proxy()
        self._proxy = (proxy_host, proxy_port, proxy_auth)
        ca = cfg["ca_cert_path"]
        if not os.path.isfile(ca):
            raise RuntimeError(f"CA cert not found: {ca}")
        self._ctx = ssl.create_default_context(cafile=ca)
        # TLS session resumption cache (Gemini-4 §3.3): shared by every
        # pooled connection; thread-safe. 0-RTT is never enabled.
        self.tls_cache = TLSSessionCache()
        self._pool = ConnectionPool(
            connect_fn=self._new_verified_conn,
            min_size=4, max_size=16)

    def _new_verified_conn(self):
        """Create one connection: CONNECT+TLS, then a lightweight GET
        /status to prove the full chain (401 also proves the link)."""
        proxy_host, proxy_port, proxy_auth = self._proxy
        c = ProxyHTTPSConnection(
            self._cfg["vps_host"], self._cfg["vps_port"],
            proxy_host=proxy_host, proxy_port=proxy_port,
            proxy_auth=proxy_auth, sni=self._cfg["sni"],
            context=self._ctx, timeout=TIMEOUT,
            session_getter=self.tls_cache.get,
            session_setter=self.tls_cache.set,
            handshake_stats=self.tls_cache.note_handshake)
        c.connect()
        st = self.tls_cache.stats()
        log.info("upstream connected (tls resumed=%d full=%d)",
                 st["resumed"], st["full"])
        headers = {
            "Host": self._cfg["sni"],
            "Connection": "keep-alive",
            "User-Agent": "mused/1.0",
        }
        try:
            c.request("GET", API_PREFIX + "/status", body=None,
                      headers=headers)
            resp = c.getresponse()
            resp.read()  # drain fully so the socket is reusable
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            raise
        return c

    def close(self):
        self._pool.close_all()

    def stats(self):
        return self._pool.stats()

    def request(self, token, method, path, body):
        """Forward one request. Returns (status, body_bytes).

        If compress_enabled, request bodies >= 2KB are zlib-compressed
        (Content-Encoding: deflate) before sending, and responses are
        decompressed according to their Content-Encoding header.
        Compression is end-to-end (VM <-> PC): the VPS passes the bytes
        through untouched, so it costs the VPS zero CPU.
        """
        if not path.startswith("/"):
            path = "/" + path
        url = API_PREFIX + path
        payload = None
        headers = {
            "Host": self._cfg["sni"],
            "X-Bridge-Token": token,
            "Connection": "keep-alive",
            "User-Agent": "mused/1.0",
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if self._cfg.get("compress_enabled", True):
                payload, comp_hdrs = request_headers(payload)
                headers.update(comp_hdrs)
            headers["Content-Length"] = str(len(payload))
        pc = self._pool.get_connection()
        try:
            pc.conn.request(method, url, body=payload, headers=headers)
            resp = pc.conn.getresponse()
            data = resp.read()  # drain fully so the socket is reusable
            # server asked to close: don't return a dead conn to the pool
            broken = bool(getattr(resp, "will_close", False))
            self._pool.return_connection(pc, is_broken=broken)
            if self._cfg.get("compress_enabled", True):
                try:
                    data = decompress(data,
                                      response_encoding(resp.headers))
                except (ValueError, zlib.error) as e:
                    # Corrupt deflate must never be delivered silently.
                    # HTTP framing (Content-Length) guarantees the message
                    # is complete, so the payload itself is bad data.
                    log.warning("decompress failed for %s %s: %s",
                                method, path, e)
                    raise RuntimeError(
                        f"response decompress failed: {e}") from e
            return resp.status, data
        except (http.client.HTTPException, OSError, ssl.SSLError):
            # connection went stale; drop it so next call rebuilds
            self._pool.return_connection(pc, is_broken=True)
            raise


# ---------------------------------------------------------------- socket IPC

def send_frame(sock, obj):
    blob = json.dumps(obj).encode("utf-8")
    sock.sendall(HDR.pack(len(blob)) + blob)


def recv_frame(sock):
    hdr = b""
    while len(hdr) < HDR.size:
        chunk = sock.recv(HDR.size - len(hdr))
        if not chunk:
            raise ConnectionError("client closed")
        hdr += chunk
    (n,) = HDR.unpack(hdr)
    if n > 16 * 1024 * 1024:
        raise ValueError("frame too large")
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("client closed mid-frame")
        buf += chunk
    return json.loads(buf.decode("utf-8"))


def handle_client(client_sock, upstream, batcher=None):
    try:
        req = recv_frame(client_sock)
        token = req.get("token", "")
        method = req.get("method", "GET").upper()
        path = req.get("path", "/status")
        body = req.get("body")
        try:
            if batcher is not None:
                status, body_text = _via_batcher(batcher, req,
                                                 token, method, path, body)
            else:
                status, data = upstream.request(token, method, path, body)
                body_text = data.decode("utf-8", "replace")
            send_frame(client_sock, {"status": status, "body": body_text})
        except Exception as e:
            log.warning("upstream error for %s %s: %s", method, path, e)
            send_frame(client_sock,
                       {"status": 0, "body": "",
                        "error": f"upstream: {type(e).__name__}: {e}"})
    except Exception as e:
        try:
            send_frame(client_sock, {"status": 0, "body": "",
                                     "error": f"bad request: {e}"})
        except Exception:
            pass
    finally:
        client_sock.close()


def _via_batcher(batcher, req, token, method, path, body):
    """Route one socket request through the MicroBatcher.

    Optional socket fields: "target" (else taken from body["target"]),
    "batchable" (bool; else inferred via is_read_only), "request_id".
    """
    target = req.get("target")
    if target is None and isinstance(body, dict):
        target = body.get("target")
    batchable = req.get("batchable")
    if batchable is None:
        batchable = is_read_only(method, path, body)
    return batcher.submit(token, target, method, path, body,
                          batchable=bool(batchable),
                          request_id=req.get("request_id"))


def heartbeat_loop(upstream, stop_event, interval):
    backoff = 1
    while not stop_event.is_set():
        try:
            # token not needed for liveness: 401 also proves the link
            upstream.request("", "GET", "/status", None)
            backoff = 1  # link alive (even 401 means TLS+nginx+bridge OK)
        except Exception as e:
            log.warning("heartbeat failed: %s", e)
            upstream.close()
            # exponential backoff before next probe
            stop_event.wait(min(backoff, BACKOFF_CAP))
            backoff = min(backoff * 2, BACKOFF_CAP)
            continue
        stop_event.wait(interval)


# ---------------------------------------------------------------- main

def main():
    setup_logging()  # default INFO first so config-load warnings are visible
    cfg = load_config()
    level = getattr(logging, str(cfg.get("log_level", "INFO")).upper(), None)
    if isinstance(level, int):
        logging.getLogger().setLevel(level)
    log.info("mused starting (vps=%s:%s sni=%s socket=%s)",
             cfg["vps_host"], cfg["vps_port"], cfg["sni"],
             cfg["socket_path"])

    try:
        upstream = Upstream(cfg)
    except Exception as e:
        log.error("cannot initialize upstream: %s", e)
        return 1

    batcher = None
    if cfg.get("microbatch_enabled"):
        if MicroBatcher is None:
            log.error("microbatch_enabled but microbatch.py not importable")
            return 1

        def _send_fn(tok, m, p, b):
            status, data = upstream.request(tok, m, p, b)
            return status, data.decode("utf-8", "replace")

        batcher = MicroBatcher(_send_fn)
        log.info("microbatch enabled (1.5ms window, max 10/batch, 64KB cap)")

    # warm the connection at startup so the first real request is fast
    try:
        upstream.request("", "GET", "/status", None)
        log.info("upstream warm OK")
    except Exception as e:
        log.warning("initial upstream connect failed: %s", e)

    stop_event = threading.Event()
    srv_holder = {}

    def shutdown(signum, _frame):
        sig = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        log.info("received %s, shutting down", sig)
        stop_event.set()
        srv = srv_holder.get("srv")
        if srv is not None:
            try:
                srv.close()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    hb = threading.Thread(target=heartbeat_loop,
                          args=(upstream, stop_event,
                                cfg["heartbeat_interval"]),
                          daemon=True, name="heartbeat")
    hb.start()

    sock_path = cfg["socket_path"]
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv_holder["srv"] = srv
    try:
        srv.bind(sock_path)
    except OSError as e:
        log.error("cannot bind %s: %s", sock_path, e)
        return 1
    os.chmod(sock_path, 0o600)
    srv.listen(64)
    log.info("listening on %s", sock_path)

    try:
        while not stop_event.is_set():
            try:
                srv.settimeout(1.0)
                client, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed by shutdown handler
            t = threading.Thread(target=handle_client,
                                 args=(client, upstream, batcher), daemon=True)
            t.start()
    finally:
        stop_event.set()
        hb.join(timeout=5)
        try:
            srv.close()
        except Exception:
            pass
        upstream.close()
        try:
            os.unlink(sock_path)
            log.info("removed socket %s", sock_path)
        except FileNotFoundError:
            pass
        log.info("mused stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
