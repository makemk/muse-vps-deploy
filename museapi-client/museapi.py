#!/usr/bin/env python3
"""museapi: production-grade HTTPS client for VPS /muse-api/ (no SSH).

Replaces the SSH-based musepc transport with direct HTTPS:
  TCP -> egress proxy -> CONNECT vps:443 -> TLS (SNI=ai.vps38.local,
  verified against VPS38 Root CA) -> HTTP/1.1 keep-alive -> /muse-api/*

Pure Python standard library (http.client + ssl + socket). No requests.

Features:
  - Connection pool: one keep-alive HTTPS connection per (host, port),
    reused across requests in the same process.
  - Auto-retry: exponential backoff (0.2s, 0.4s, 0.8s), max 3 attempts,
    only for idempotent methods (GET). POSTs are never auto-retried.
  - Timeouts: separate connect_timeout and read_timeout, configurable.
  - Bearer token: read once from token_file (600), injected as
    ``Authorization: Bearer <redacted>`` on every request.
  - Typed errors: AuthError (401), ForbiddenError (403), NotFoundError (404),
    ServerError (5xx), NetworkError, TimeoutError.

Typical usage:
    from museapi import MuseAPIClient
    with MuseAPIClient() as api:
        out = api.cmd("RD-928", "echo hello", timeout=30)
        print(out["stdout"])
"""
import base64
import http.client
import json
import os
import socket
import ssl
import time
from urllib.parse import urlparse

__version__ = "1.0.0"

# ------------------------------------------------------------------ errors

class MuseAPIError(Exception):
    """Base for all museapi errors."""
    def __init__(self, message, *, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(MuseAPIError):
    """HTTP 401: bad or missing token."""


class ForbiddenError(MuseAPIError):
    """HTTP 403: token valid but target not permitted."""


class NotFoundError(MuseAPIError):
    """HTTP 404: endpoint or target PC not found."""


class ConflictError(MuseAPIError):
    """HTTP 409: ambiguous target, multiple PCs online."""


class ServerError(MuseAPIError):
    """HTTP 5xx from the bridge."""


class NetworkError(MuseAPIError):
    """TCP/TLS/proxy failure before any HTTP response."""


class TimeoutError(MuseAPIError):  # noqa: A001 - intentional stdlib shadowing
    """Connect or read timed out."""


_STATUS_EXC = {
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
}


def _raise_for_status(status, reason, body_text, path):
    if 200 <= status < 300:
        return
    exc_cls = _STATUS_EXC.get(status)
    msg = f"{path}: HTTP {status} {reason}"
    if body_text:
        msg += f" - {body_text[:200]}"
    if exc_cls:
        raise exc_cls(msg, status=status, body=body_text)
    if 500 <= status < 600:
        raise ServerError(msg, status=status, body=body_text)
    raise MuseAPIError(msg, status=status, body=body_text)


# ------------------------------------------------------------------ config

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "mused")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "vps_host": "38.207.167.51",
    "vps_port": 443,
    "sni": "ai.vps38.local",
    "ca_cert_path": os.path.join(
        HOME, "workspace", "muse-bridge", "src", "muse", "vps-root-ca.pem"),
    "token_file": os.path.join(CONFIG_DIR, "token"),
    "connect_timeout": 10,
    "read_timeout": 60,
    "max_retries": 3,
    "retry_base_delay": 0.2,
    "user_agent": f"museapi/{__version__}",
}

# Methods safe to retry (idempotent, no side effects on the PC).
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def load_config(path=None):
    """Load config.json merged over DEFAULTS. Missing file -> defaults."""
    cfg = dict(DEFAULTS)
    p = os.path.expanduser(path or CONFIG_PATH)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULTS:
                    cfg[k] = v
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    # Expand ~ in paths
    for k in ("ca_cert_path", "token_file"):
        if isinstance(cfg[k], str):
            cfg[k] = os.path.expanduser(cfg[k])
    return cfg


def read_token(token_file):
    """Read the bearer token from token_file. Raises AuthError if missing."""
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            tok = f.read().strip()
    except FileNotFoundError:
        tok = ""
    if not tok:
        raise AuthError(
            f"no token: {token_file} missing or empty "
            "(generate one with the token manager)")
    return tok


# ------------------------------------------------- proxy-tunneled HTTPS conn

def _parse_proxy():
    raw = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
           or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    if not raw:
        return None
    u = urlparse(raw)
    auth = None
    if u.username:
        auth = base64.b64encode(
            f"{u.username}:{u.password or ''}".encode("utf-8")).decode("ascii")
    return u.hostname, u.port or 3128, auth


class _ProxyHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection via HTTP CONNECT proxy, TLS SNI independent of IP.

    Needed because nginx routes on SNI (ai.vps38.local) while we dial the
    VPS by raw IP through the egress proxy.
    """

    def __init__(self, host, port=443, *, proxy, sni, context,
                 connect_timeout=10):
        super().__init__(host, port, context=context,
                         timeout=connect_timeout)
        self._proxy = proxy          # (host, port, auth) or None
        self._sni = sni

    def connect(self):
        if self._proxy:
            phost, pport, pauth = self._proxy
            sock = socket.create_connection((phost, pport),
                                            timeout=self.timeout)
            try:
                req = (f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                       f"Host: {self.host}:{self.port}\r\n")
                if pauth:
                    req += f"Proxy-Authorization: Basic {pauth}\r\n"
                req += "\r\n"
                sock.sendall(req.encode("ascii"))
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise NetworkError(
                            "proxy closed connection during CONNECT")
                    resp += chunk
                    if len(resp) > 65536:
                        raise NetworkError(
                            "proxy CONNECT response too large")
                line = resp.split(b"\r\n", 1)[0].decode("latin1")
                if not (line.startswith("HTTP/1.1 200")
                        or line.startswith("HTTP/1.0 200")):
                    raise NetworkError(f"CONNECT rejected: {line}")
            except Exception:
                sock.close()
                raise
        else:
            try:
                sock = socket.create_connection((self.host, self.port),
                                                timeout=self.timeout)
            except socket.timeout as e:
                raise TimeoutError(f"connect to {self.host}:{self.port} "
                                   f"timed out") from e
            except OSError as e:
                raise NetworkError(f"connect to {self.host}:{self.port} "
                                   f"failed: {e}") from e
        try:
            self.sock = self._context.wrap_socket(sock,
                                                  server_hostname=self._sni)
        except ssl.SSLError as e:
            sock.close()
            raise NetworkError(f"TLS handshake failed: {e}") from e
        except socket.timeout as e:
            sock.close()
            raise TimeoutError("TLS handshake timed out") from e


# ------------------------------------------------------------------ client

class MuseAPIClient:
    """Pooled HTTPS client for VPS /muse-api/.

    Usage:
        with MuseAPIClient() as api:
            api.cmd("RD-928", "echo hi")
    """

    API_PREFIX = "/muse-api"

    def __init__(self, config_path=None, *, token=None, **overrides):
        self.config = load_config(config_path)
        self.config.update({k: v for k, v in overrides.items()
                            if k in DEFAULTS})
        self._token = token or read_token(self.config["token_file"])
        self._proxy = _parse_proxy()
        self._ctx = ssl.create_default_context(
            cafile=self.config["ca_cert_path"])
        # Post-handshake read timeout is enforced per-request via
        # socket.settimeout around getresponse()/read().
        self._pool = {}  # (host, port) -> _ProxyHTTPSConnection
        self._pool_lock = __import__("threading").Lock()
        self.stats = {"requests": 0, "reused": 0, "reconnects": 0}

    # -- context manager ------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        with self._pool_lock:
            for conn in self._pool.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._pool.clear()

    # -- low-level -------------------------------------------------------
    def _get_conn(self):
        key = (self.config["vps_host"], self.config["vps_port"])
        with self._pool_lock:
            conn = self._pool.get(key)
            if conn is not None and conn.sock is not None:
                self.stats["reused"] += 1
                return conn
            self.stats["reconnects"] += 1
            conn = _ProxyHTTPSConnection(
                self.config["vps_host"], self.config["vps_port"],
                proxy=self._proxy,
                sni=self.config["sni"],
                context=self._ctx,
                connect_timeout=self.config["connect_timeout"])
            self._pool[key] = conn
            return conn

    def _drop_conn(self):
        key = (self.config["vps_host"], self.config["vps_port"])
        with self._pool_lock:
            conn = self._pool.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def request(self, method, path, body=None, *, timeout=None,
                _attempt=0):
        """Raw request. Returns (status, parsed_json_or_text).

        Retries idempotent methods on network failure with exponential
        backoff. Never retries POST/PUT (non-idempotent).
        """
        method = method.upper()
        url_path = path if path.startswith(self.API_PREFIX) else \
            self.API_PREFIX + path
        payload = None
        headers = {
            "Host": self.config["sni"],
            "Authorization": f"Bearer {self._token}",
            "Connection": "keep-alive",
            "User-Agent": self.config["user_agent"],
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))

        read_timeout = timeout if timeout is not None else \
            self.config["read_timeout"]
        max_retries = self.config["max_retries"]
        base_delay = self.config["retry_base_delay"]

        try:
            conn = self._get_conn()
            # Stale pooled connection (server closed it): http.client
            # raises on send; we detect via a cheap probe below.
            conn.request(method, url_path, body=payload, headers=headers)
            # Enforce read timeout on the underlying socket for this call.
            if conn.sock is not None:
                conn.sock.settimeout(read_timeout)
            resp = conn.getresponse()
            raw = resp.read()  # fully drain -> keep-alive reusable
            self.stats["requests"] += 1
        except (TimeoutError, socket.timeout) as e:
            self._drop_conn()
            raise TimeoutError(
                f"{method} {url_path}: read timed out after "
                f"{read_timeout}s") from e
        except (http.client.HTTPException, NetworkError, OSError) as e:
            # Connection died mid-flight (stale keep-alive, RST, proxy
            # hiccup). Drop it and retry once for idempotent methods.
            self._drop_conn()
            if method in IDEMPOTENT_METHODS and _attempt < max_retries:
                time.sleep(base_delay * (2 ** _attempt))
                return self.request(method, path, body, timeout=timeout,
                                    _attempt=_attempt + 1)
            if isinstance(e, MuseAPIError):
                raise
            raise NetworkError(
                f"{method} {url_path}: {e.__class__.__name__}: {e}") from e

        ctype = resp.getheader("Content-Type", "")
        text = raw.decode("utf-8", errors="replace")
        _raise_for_status(resp.status, resp.reason, text, url_path)
        if "application/json" in ctype:
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                pass
        return resp.status, text

    # -- high-level API ---------------------------------------------------
    def status(self):
        """GET /muse-api/status -> dict."""
        _, data = self.request("GET", "/status")
        return data

    def cmd(self, target, command, *, cwd="", timeout=60, wait=True):
        """Run a shell command on the target PC. Returns result dict.

        Result dict (wait=True): {"ok": True, "stdout": ..., "stderr": ...,
        "exit_code": ...} — exact keys depend on the PC plugin.
        """
        _, data = self.request("POST", "/cmd", {
            "target": target,
            "command": command,
            "cwd": cwd,
            "timeout": timeout,
            "wait": wait,
        }, timeout=timeout + 10)
        return data

    def batch(self, target, commands, *, timeout=180, wait=True):
        """Run several commands. commands: list of str or dicts."""
        norm = [c if isinstance(c, dict) else {"command": c}
                for c in commands]
        _, data = self.request("POST", "/batch", {
            "target": target,
            "commands": norm,
            "timeout": timeout,
            "wait": wait,
        }, timeout=timeout + 10)
        return data

    def file_op(self, target, op, path, *, content=None, old=None, new=None,
                pattern=None, out_dir=None, timeout=60, wait=True, **extra):
        """File operations via POST /muse-api/file.

        op: read | write | edit | glob | grep | info | list | archive | extract
        """
        body = {"target": target, "op": op, "path": path,
                "timeout": timeout, "wait": wait}
        if content is not None:
            body["content"] = content
        if old is not None:
            body["old"] = old
        if new is not None:
            body["new"] = new
        if pattern is not None:
            body["pattern"] = pattern
        if out_dir is not None:
            body["out_dir"] = out_dir
        body.update(extra)
        _, data = self.request("POST", "/file", body,
                               timeout=timeout + 10)
        return data

    # Convenience wrappers matching musepc flags
    def read_file(self, target, path, **kw):
        return self.file_op(target, "read", path, **kw)

    def write_file(self, target, path, content, **kw):
        return self.file_op(target, "write", path, content=content, **kw)

    def edit_file(self, target, path, old, new, **kw):
        return self.file_op(target, "edit", path, old=old, new=new, **kw)

    def glob(self, target, pattern, path="", **kw):
        return self.file_op(target, "glob", path, pattern=pattern, **kw)

    def grep(self, target, pattern, path="", **kw):
        return self.file_op(target, "grep", path, pattern=pattern, **kw)

    def ls(self, target, path, **kw):
        return self.file_op(target, "list", path, **kw)

    def archive(self, target, path, *extra_paths, **kw):
        body_extra = {"extra_paths": list(extra_paths)} if extra_paths else {}
        return self.file_op(target, "archive", path, **body_extra, **kw)

    def extract(self, target, path, out_dir="", **kw):
        return self.file_op(target, "extract", path, out_dir=out_dir, **kw)
