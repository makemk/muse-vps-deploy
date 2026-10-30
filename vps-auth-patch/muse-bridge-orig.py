#!/usr/bin/env python3
"""muse-bridge: Ultra-lightweight WebSocket command bus for remote PC control.

Listens on 127.0.0.1:8471, exposed via Nginx TLS at location /muse-bridge.

WS Clients (Win11 PC, VS Code extension):
    wss://<vps>/muse-bridge (Header X-Bridge-Token or ?token=)
    Server -> Client: {"type":"cmd","id":..,"command":..,"timeout":..,"cwd":..}
    Client -> Server: {"type":"hello","client":..,"hostname":..}
    Client -> Server: {"type":"result","id":..,"exit":..,"stdout":..,"stderr":..}
    Client -> Server: {"type":"file_result","id":..,"ok":..,...}
    Client -> Server: {"type":"batch_result","id":..,"ok":..,"results":[...]}

HTTP API (localhost only, invoked by musepc CLI / AI assistants):
    POST /cmd          {"token":..,"command":..,"timeout":..,"cwd":..,"wait":true}
    POST /batch        {"token":..,"commands":[{"command":..},...],"wait":true}
    POST /file         {"token":..,"op":"read|write|file_info|list_dir",...}
    GET  /result/<id>  blocks until result arrives
    GET  /status       {"ok":true,"clients":n,"pending":n}

Token lives in /etc/muse-bridge/token (mode 600).
Stdlib only, zero third-party dependencies, <15MB RSS.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.parse
import uuid

BRIDGE_VERSION = "1.1.0"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8471
TOKEN_PATH = "/etc/muse-bridge/token"
PING_INTERVAL = 15
PING_TIMEOUT = 35
DEFAULT_CMD_TIMEOUT = 60
MAX_PAYLOAD_SIZE = 64 * 1024 * 1024  # 64MB
COMPLETED_RESULT_TTL = 300  # seconds to keep wait=false results for GET /result
COMPLETED_RESULTS_MAX = 256

# Global state
CONFIGURED_TOKEN = ""
connected_clients = set()
pending_futures = {}  # id -> asyncio.Future
completed_results = {}  # id -> (msg, completed_at) for wait=false results


def _remember_result(req_id, msg):
    """Keep a finished wait=false result for GET /result, bounded by TTL/count."""
    now = time.time()
    completed_results[req_id] = (msg, now)
    for k, (_, ts) in list(completed_results.items()):
        if now - ts > COMPLETED_RESULT_TTL:
            completed_results.pop(k, None)
    while len(completed_results) > COMPLETED_RESULTS_MAX:
        oldest = min(completed_results, key=lambda k: completed_results[k][1])
        completed_results.pop(oldest, None)


def load_token():
    global CONFIGURED_TOKEN
    env_token = os.environ.get("MUSE_BRIDGE_TOKEN", "").strip()
    if env_token:
        CONFIGURED_TOKEN = env_token
        return CONFIGURED_TOKEN
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                CONFIGURED_TOKEN = f.read().strip()
                return CONFIGURED_TOKEN
        except Exception as e:
            print(f"[bridge] Warning reading {TOKEN_PATH}: {e}", file=sys.stderr)
    return ""


def check_auth(provided_token):
    if not CONFIGURED_TOKEN:
        load_token()
    if not CONFIGURED_TOKEN:
        return False
    return hmac.compare_digest(CONFIGURED_TOKEN, provided_token.strip())


class WSConnection:
    def __init__(self, reader, writer, client_info=None):
        self.reader = reader
        self.writer = writer
        self.client_info = client_info or {}
        self.closed = False
        self.addr = writer.get_extra_info("peername")
        self.write_lock = asyncio.Lock()
        self.connected_at = time.time()
        self.last_active_time = time.time()

    async def send_frame(self, opcode, payload: bytes):
        if self.closed:
            return
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        async with self.write_lock:
            try:
                self.writer.write(header + payload)
                await self.writer.drain()
            except Exception:
                self.closed = True

    async def send_text(self, text: str):
        await self.send_frame(0x1, text.encode("utf-8"))

    async def send_ping(self):
        await self.send_frame(0x9, b"ping")

    async def send_pong(self, data: bytes = b""):
        await self.send_frame(0xA, data)

    async def read_frame(self):
        try:
            # 初始帧头读取增加超时保护 (PING_TIMEOUT + 5)，杜绝半开死连接无限阻塞
            h = await asyncio.wait_for(self.reader.readexactly(2), timeout=PING_TIMEOUT + 5)
        except Exception:
            return None, None
        self.last_active_time = time.time()
        b1, b2 = h[0], h[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        is_masked = bool(b2 & 0x80)
        payload_len = b2 & 0x7F

        try:
            if payload_len == 126:
                ext = await asyncio.wait_for(self.reader.readexactly(2), timeout=10)
                payload_len = struct.unpack("!H", ext)[0]
            elif payload_len == 127:
                ext = await asyncio.wait_for(self.reader.readexactly(8), timeout=10)
                payload_len = struct.unpack("!Q", ext)[0]

            if payload_len > MAX_PAYLOAD_SIZE:
                raise ValueError(f"Payload too large: {payload_len}")

            mask = None
            if is_masked:
                mask = await asyncio.wait_for(self.reader.readexactly(4), timeout=10)

            payload = await asyncio.wait_for(self.reader.readexactly(payload_len), timeout=30) if payload_len > 0 else b""
            if is_masked and mask:
                payload = bytes([b ^ mask[i % 4] for i, b in enumerate(payload)])

            return opcode, payload
        except Exception:
            return None, None

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.write(bytearray([0x88, 0x00]))
            await self.writer.drain()
        except Exception:
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass



async def handle_ws_session(ws: WSConnection):
    connected_clients.add(ws)
    print(f"[bridge] Client connected: {ws.addr} (active: {len(connected_clients)})", flush=True)

    ping_task = asyncio.create_task(ws_ping_loop(ws))
    try:
        while not ws.closed:
            opcode, payload = await ws.read_frame()
            if opcode is None or opcode == 0x8:  # Close or EOF/Timeout
                break
            ws.last_active_time = time.time()
            if opcode == 0x9:  # Ping
                await ws.send_pong(payload)
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode == 0x1:  # Text frame
                try:
                    text = payload.decode("utf-8", errors="replace")
                    msg = json.loads(text)
                    msg_type = msg.get("type")
                    if msg_type == "hello":
                        ws.client_info = msg
                        hostname = msg.get("hostname")
                        client_name = msg.get("client")
                        print(f"[bridge] Hello from client: {hostname} ({client_name})", flush=True)
                        # 收到同名主机新连接时，快速剔除旧连接 (Dead connection fast reap)
                        for old_ws in list(connected_clients):
                            if old_ws is not ws and old_ws.client_info.get("hostname") == hostname:
                                print(f"[bridge] Evicting superseded connection from host '{hostname}': {old_ws.addr}", flush=True)
                                old_ws.closed = True
                                asyncio.create_task(old_ws.close())
                                connected_clients.discard(old_ws)
                    elif msg_type in ("result", "file_result", "batch_result"):
                        msg_id = msg.get("id")
                        if msg_id:
                            fut = pending_futures.pop(msg_id, None)
                            if fut is not None:
                                if not fut.done():
                                    fut.set_result(msg)
                                # wait=false: keep result briefly so GET /result can collect it
                                if getattr(fut, "_mb_async", False):
                                    _remember_result(msg_id, msg)
                            else:
                                # Late result (dispatcher timed out or GET consumed it)
                                _remember_result(msg_id, msg)
                except Exception as e:
                    print(f"[bridge] Error parsing client message: {e}", flush=True)
    except Exception as e:
        print(f"[bridge] Client error: {e}", flush=True)
    finally:
        ws.closed = True
        ping_task.cancel()
        connected_clients.discard(ws)
        # Fail fast: resolve every pending request bound to this client instead of
        # letting waiters sit until their full timeout.
        for req_id, fut in list(pending_futures.items()):
            if getattr(fut, "_mb_client", None) is ws:
                pending_futures.pop(req_id, None)
                if not fut.done():
                    fut.set_result({"ok": False, "id": req_id,
                                    "error": "Target disconnected before result"})
        print(f"[bridge] Client disconnected: {ws.addr} (active: {len(connected_clients)})", flush=True)
        await ws.close()


async def ws_ping_loop(ws: WSConnection):
    try:
        while not ws.closed:
            await asyncio.sleep(PING_INTERVAL)
            if ws.closed:
                break
            # 心跳检测：静默超时快速主动收割 (Dead connection fast reap)
            idle_sec = time.time() - getattr(ws, "last_active_time", time.time())
            if idle_sec > PING_TIMEOUT:
                print(f"[bridge] Heartbeat timeout: client {ws.addr} inactive for {int(idle_sec)}s. Evicting dead connection.", flush=True)
                ws.closed = True
                await ws.close()
                connected_clients.discard(ws)
                break
            await ws.send_ping()
    except asyncio.CancelledError:
        pass
    except Exception:
        ws.closed = True


def list_active_clients():
    # 动态过滤并即时清理已关闭或已超时的死连接
    now = time.time()
    valid_clients = []
    for c in list(connected_clients):
        if c.closed or (now - getattr(c, "last_active_time", now) > PING_TIMEOUT):
            c.closed = True
            connected_clients.discard(c)
        else:
            valid_clients.append(c)
    # 按最近活跃排序
    valid_clients.sort(key=lambda c: getattr(c, "last_active_time", 0), reverse=True)
    return valid_clients


def _online_hostnames():
    return [c.client_info.get("hostname", "?") for c in list_active_clients()]


def pick_client(hostname=None):
    """按 hostname 选客户端。返回 (client, reason)，reason ∈ ok/none/not_found/multiple"""
    valid = list_active_clients()
    if not valid:
        return None, "none"
    if hostname:
        for c in valid:
            if c.client_info.get("hostname") == hostname:
                return c, "ok"
        for c in valid:  # 大小写不敏感兜底
            if (c.client_info.get("hostname") or "").lower() == hostname.lower():
                return c, "ok"
        return None, "not_found"
    if len(valid) == 1:
        return valid[0], "ok"
    return None, "multiple"


def get_active_client(hostname=None):
    # 薄包装：供老代码用，只返回 client
    client, _ = pick_client(hostname)
    return client


async def dispatch_request_to_pc(req_obj, timeout_sec=DEFAULT_CMD_TIMEOUT):
    target = req_obj.pop("target", None) or None
    client, reason = pick_client(target)
    if reason == "none":
        return {"ok": False, "error": "No PC connected to Muse Bridge"}
    if reason == "not_found":
        return {"ok": False, "error": f"Target '{target}' not online. Online: {_online_hostnames()}"}
    if reason == "multiple":
        return {"ok": False, "error": f"Multiple clients online {_online_hostnames()}, specify target hostname"}

    req_id = req_obj.get("id") or f"mb_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    req_obj["id"] = req_id

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut._mb_client = client  # so a disconnect can fail this request fast
    pending_futures[req_id] = fut

    try:
        await client.send_text(json.dumps(req_obj))
        if client.closed:
            return {"ok": False, "id": req_id, "error": "Target disconnected before result"}
        res = await asyncio.wait_for(fut, timeout=timeout_sec)
        return res
    except asyncio.TimeoutError:
        return {"ok": False, "id": req_id, "error": f"Command timed out after {timeout_sec}s"}
    except Exception as e:
        return {"ok": False, "id": req_id, "error": str(e)}
    finally:
        pending_futures.pop(req_id, None)


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        # Read request line & headers
        raw_headers = b""
        while b"\r\n\r\n" not in raw_headers:
            line = await reader.readline()
            if not line:
                break
            raw_headers += line
            if len(raw_headers) > 65536:
                writer.close()
                return

        if not raw_headers:
            writer.close()
            return

        header_text = raw_headers.decode("iso-8859-1")
        lines = header_text.split("\r\n")
        req_line = lines[0]
        parts = req_line.split(" ")
        if len(parts) < 2:
            writer.close()
            return

        method = parts[0].upper()
        url_path = parts[1]

        parsed_url = urllib.parse.urlparse(url_path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        headers = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        # Check WebSocket upgrade
        if headers.get("upgrade", "").lower() == "websocket":
            token = (
                headers.get("x-bridge-token")
                or query.get("token", [""])[0]
                or query.get("access_token", [""])[0]
            )
            if not check_auth(token):
                writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 12\r\n\r\nUnauthorized")
                await writer.drain()
                writer.close()
                return

            sec_key = headers.get("sec-websocket-key", "")
            if not sec_key:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 15\r\n\r\nMissing Sec-Key")
                await writer.drain()
                writer.close()
                return

            accept_val = base64.b64encode(
                hashlib.sha1(sec_key.encode("ascii") + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            ).decode("ascii")

            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
            )
            writer.write(resp.encode("ascii"))
            await writer.drain()

            ws = WSConnection(reader, writer)
            await handle_ws_session(ws)
            return

        # Otherwise HTTP API handler
        content_length = int(headers.get("content-length", 0))
        body_bytes = b""
        if content_length > 0:
            body_bytes = await reader.readexactly(content_length)

        body_json = {}
        if body_bytes:
            try:
                body_json = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                pass

        token = (
            headers.get("x-bridge-token")
            or headers.get("authorization", "").replace("Bearer ", "").strip()
            or body_json.get("token", "")
            or query.get("token", [""])[0]
        )

        async def send_json(data, status=200):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            status_text = "OK" if status == 200 else ("Unauthorized" if status == 401 else "Error")
            resp = (
                f"HTTP/1.1 {status} {status_text}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(resp.encode("ascii") + payload)
            await writer.drain()
            writer.close()

        # Authenticated endpoints
        if not check_auth(token):
            await send_json({"ok": False, "error": "Unauthorized: invalid bridge token"}, 401)
            return

        if path == "/status":
            clients_info = [c.client_info for c in list_active_clients()]
            await send_json({
                "ok": True,
                "version": BRIDGE_VERSION,
                "clients": len(clients_info),
                "client_list": clients_info,
                "pending": len(pending_futures),
                "completed_results": len(completed_results),
                "uptime": int(time.time()),
            })
            return

        if path == "/cmd" and method == "POST":
            cmd = body_json.get("command", "")
            timeout = int(body_json.get("timeout", DEFAULT_CMD_TIMEOUT))
            cwd = body_json.get("cwd", "")
            wait_for_res = body_json.get("wait", True)
            target = body_json.get("target", "") or None

            req = {"type": "cmd", "command": cmd, "timeout": timeout, "cwd": cwd, "target": target}
            if wait_for_res:
                res = await dispatch_request_to_pc(req, timeout_sec=timeout + 5)
                await send_json(res)
            else:
                client, reason = pick_client(target)
                if reason == "none":
                    await send_json({"ok": False, "error": "No PC connected to Muse Bridge"}, 503)
                    return
                if reason == "not_found":
                    await send_json({"ok": False, "error": f"Target '{target}' not online. Online: {_online_hostnames()}"}, 404)
                    return
                if reason == "multiple":
                    await send_json({"ok": False, "error": f"Multiple clients online {_online_hostnames()}, specify target hostname"}, 409)
                    return
                req_id = f"cmd_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
                req["id"] = req_id
                req.pop("target", None)
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                fut._mb_client = client
                fut._mb_async = True
                pending_futures[req_id] = fut
                await client.send_text(json.dumps(req))
                await send_json({"ok": True, "id": req_id})
            return

        if path == "/batch" and method == "POST":
            commands = body_json.get("commands", [])
            timeout = int(body_json.get("timeout", 180))
            wait_for_res = body_json.get("wait", True)
            target = body_json.get("target", "") or None
            req = {"type": "batch", "commands": commands, "target": target}
            if wait_for_res:
                res = await dispatch_request_to_pc(req, timeout_sec=timeout + 5)
                await send_json(res)
            else:
                req_id = f"bat_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
                req["id"] = req_id
                req.pop("target", None)
                client, reason = pick_client(target)
                if reason == "none":
                    await send_json({"ok": False, "error": "No PC connected"}, 503)
                    return
                if reason == "not_found":
                    await send_json({"ok": False, "error": f"Target '{target}' not online. Online: {_online_hostnames()}"}, 404)
                    return
                if reason == "multiple":
                    await send_json({"ok": False, "error": f"Multiple clients online {_online_hostnames()}, specify target hostname"}, 409)
                    return
                fut = asyncio.get_running_loop().create_future()
                fut._mb_client = client
                fut._mb_async = True
                pending_futures[req_id] = fut
                await client.send_text(json.dumps(req))
                await send_json({"ok": True, "id": req_id})
            return

        if path == "/file" and method == "POST":
            op = body_json.get("op", "read")
            timeout = int(body_json.get("timeout", 60))
            wait_for_res = body_json.get("wait", True)

            # Map op to protocol type
            type_map = {
                "read": "read_file",
                "write": "write_file",
                "edit": "edit_file",
                "glob": "glob_files",
                "grep": "grep_files",
                "info": "file_info",
                "list": "list_dir",
                "archive": "archive_7z",
                "extract": "extract_7z",
            }
            if op not in type_map:
                await send_json({"ok": False, "error": f"Unknown file op '{op}'. Supported: {sorted(type_map)}"}, 400)
                return
            proto_type = type_map[op]
            req = dict(body_json)
            req["type"] = proto_type
            target = body_json.get("target", "") or None
            if wait_for_res:
                res = await dispatch_request_to_pc(req, timeout_sec=timeout + 5)
                await send_json(res)
            else:
                req_id = f"fil_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
                req["id"] = req_id
                req.pop("target", None)
                client, reason = pick_client(target)
                if reason == "none":
                    await send_json({"ok": False, "error": "No PC connected"}, 503)
                    return
                if reason == "not_found":
                    await send_json({"ok": False, "error": f"Target '{target}' not online. Online: {_online_hostnames()}"}, 404)
                    return
                if reason == "multiple":
                    await send_json({"ok": False, "error": f"Multiple clients online {_online_hostnames()}, specify target hostname"}, 409)
                    return
                fut = asyncio.get_running_loop().create_future()
                fut._mb_client = client
                fut._mb_async = True
                pending_futures[req_id] = fut
                await client.send_text(json.dumps(req))
                await send_json({"ok": True, "id": req_id})
            return

        if path.startswith("/result/") and method == "GET":
            req_id = path[len("/result/"):]
            if not req_id:
                await send_json({"ok": False, "error": "Missing result ID"}, 400)
                return
            fut = pending_futures.get(req_id)
            if fut is None:
                # Result may already have arrived for a wait=false request.
                cached = completed_results.pop(req_id, None)
                if cached is not None:
                    await send_json(cached[0])
                    return
                await send_json({"ok": False, "error": "Result ID not found or already consumed"}, 404)
                return
            try:
                res = await asyncio.wait_for(fut, timeout=60)
                await send_json(res)
            except asyncio.TimeoutError:
                await send_json({"ok": False, "error": "Timed out waiting for result", "id": req_id}, 504)
            finally:
                pending_futures.pop(req_id, None)
            return

        # 404 for unknown endpoints
        await send_json({"ok": False, "error": "Endpoint not found"}, 404)

    except Exception as e:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    load_token()
    print(f"[bridge] Starting Muse Bridge on {LISTEN_HOST}:{LISTEN_PORT}...", flush=True)
    if not CONFIGURED_TOKEN:
        print(f"[bridge] Warning: Token not found in {TOKEN_PATH}. Authentication will fail.", flush=True)
    else:
        print("[bridge] Security token loaded successfully.", flush=True)

    server = await asyncio.start_server(handle_connection, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[bridge] Stopped by user.")
