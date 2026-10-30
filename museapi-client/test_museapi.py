#!/usr/bin/env python3
"""Unit tests for museapi.py — no network, all connections mocked."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import museapi
from museapi import (MuseAPIClient, MuseAPIError, AuthError, ForbiddenError,
                     NotFoundError, ConflictError, ServerError, NetworkError,
                     TimeoutError, load_config, read_token)


def make_token_file(content="muse_testtoken123"):
    f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".token")
    f.write(content)
    f.close()
    return f.name


def make_config(**over):
    tok = make_token_file()
    cfg = dict(museapi.DEFAULTS)
    cfg.update(token_file=tok, ca_cert_path="/nonexistent-ca.pem")
    cfg.update(over)
    return cfg, tok


class FakeResp:
    def __init__(self, status=200, reason="OK", body=None, ctype="application/json"):
        self.status = status
        self.reason = reason
        self._body = (json.dumps(body if body is not None else {"ok": True})
                      .encode())
        self._ctype = ctype

    def getheader(self, name, default=None):
        if name.lower() == "content-type":
            return self._ctype
        return default

    def read(self):
        return self._body


class FakeConn:
    """Stands in for _ProxyHTTPSConnection."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.sock = mock.Mock()  # non-None => pooled
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        if not self.responses:
            raise AssertionError("no more mocked responses")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class TestConfig(unittest.TestCase):
    def test_defaults_when_missing(self):
        cfg = load_config("/nonexistent-path-xyz/config.json")
        self.assertEqual(cfg["vps_host"], "38.207.167.51")
        self.assertEqual(cfg["vps_port"], 443)

    def test_partial_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"vps_port": 8443, "bogus_key": 1}, f)
            p = f.name
        cfg = load_config(p)
        self.assertEqual(cfg["vps_port"], 8443)
        self.assertNotIn("bogus_key", cfg)
        os.unlink(p)

    def test_read_token_ok(self):
        p = make_token_file("  muse_abc  \n")
        self.assertEqual(read_token(p), "muse_abc")
        os.unlink(p)

    def test_read_token_missing(self):
        with self.assertRaises(AuthError):
            read_token("/nonexistent-token-xyz")


class TestErrors(unittest.TestCase):
    def _client_with(self, responses):
        cfg, tok = make_config()
        c = MuseAPIClient.__new__(MuseAPIClient)
        c.config = cfg
        c._token = "muse_testtoken123"
        c._proxy = None
        c._ctx = mock.Mock()
        import threading
        c._pool_lock = threading.Lock()
        c._pool = {}
        c.stats = {"requests": 0, "reused": 0, "reconnects": 0}
        fake = FakeConn(responses)
        c._pool[("38.207.167.51", 443)] = fake
        c._fake = fake
        os.unlink(tok)
        return c

    def test_401(self):
        c = self._client_with([FakeResp(401, "Unauthorized",
                                        {"error": "bad token"})])
        with self.assertRaises(AuthError):
            c.status()

    def test_403(self):
        c = self._client_with([FakeResp(403, "Forbidden",
                                        {"error": "target not permitted"})])
        with self.assertRaises(ForbiddenError):
            c.cmd("OTHER-PC", "echo hi")

    def test_404(self):
        c = self._client_with([FakeResp(404, "Not Found",
                                        {"error": "no such pc"})])
        with self.assertRaises(NotFoundError):
            c.cmd("NOPE", "echo hi")

    def test_500(self):
        c = self._client_with([FakeResp(500, "Error", {"error": "boom"})])
        with self.assertRaises(ServerError):
            c.status()

    def test_bearer_header_injected(self):
        c = self._client_with([FakeResp(200, "OK", {"ok": True})])
        c.status()
        _, _, _, headers = c._fake.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer muse_testtoken123")
        self.assertEqual(headers["Host"], "ai.vps38.local")

    def test_cmd_body(self):
        c = self._client_with([FakeResp(200, "OK", {"ok": True,
                                                    "stdout": "hi\n"})])
        data = c.cmd("RD-928", "echo hi", cwd="C:/x", timeout=30)
        method, path, body, _ = c._fake.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/muse-api/cmd")
        b = json.loads(body.decode())
        self.assertEqual(b["target"], "RD-928")
        self.assertEqual(b["command"], "echo hi")
        self.assertEqual(b["cwd"], "C:/x")
        self.assertEqual(b["timeout"], 30)
        self.assertTrue(b["wait"])
        self.assertEqual(data["stdout"], "hi\n")

    def test_retry_idempotent_get(self):
        import http.client as hc
        c = self._client_with([])
        # First attempt: connection dies; second: succeeds.
        dying = FakeConn([])
        dying.request = mock.Mock(
            side_effect=hc.RemoteDisconnected("closed"))
        ok_conn = FakeConn([FakeResp(200, "OK", {"ok": True})])
        c._pool[("38.207.167.51", 443)] = dying
        orig_get = c._get_conn
        conns = [dying, ok_conn]
        c._get_conn = lambda: conns.pop(0)
        c._drop_conn = lambda: None
        data = c.request("GET", "/status")
        self.assertEqual(data[1], {"ok": True})

    def test_no_retry_post(self):
        import http.client as hc
        cfg, tok = make_config(max_retries=3)
        c = MuseAPIClient.__new__(MuseAPIClient)
        c.config = cfg
        c._token = "muse_testtoken123"
        c._proxy = None
        c._ctx = mock.Mock()
        import threading
        c._pool_lock = threading.Lock()
        dying = FakeConn([])
        dying.request = mock.Mock(
            side_effect=hc.RemoteDisconnected("closed"))
        c._pool = {("38.207.167.51", 443): dying}
        c.stats = {"requests": 0, "reused": 0, "reconnects": 0}
        c._drop_conn = lambda: None
        with self.assertRaises(NetworkError):
            c.request("POST", "/cmd", {"command": "x"})
        self.assertEqual(dying.request.call_count, 1)  # exactly once
        os.unlink(tok)


class TestCLIParsing(unittest.TestCase):
    def test_bare_command(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import cli
        g, op, op_args, cmd = cli.parse_musepc_args(["echo", "hello"])
        self.assertEqual(op, "exec")
        self.assertEqual(cmd, ["echo", "hello"])
        self.assertEqual(g["target"], "RD-928")

    def test_target_and_timeout(self):
        import cli
        g, op, op_args, cmd = cli.parse_musepc_args(
            ["--target", "homepc", "--timeout", "120", "echo", "hi"])
        self.assertEqual(g["target"], "homepc")
        self.assertEqual(g["timeout"], 120)
        self.assertEqual(cmd, ["echo", "hi"])

    def test_read_flag(self):
        import cli
        g, op, op_args, cmd = cli.parse_musepc_args(
            ["--target", "X", "--read", "C:/a.txt"])
        self.assertEqual(op, "read")
        self.assertEqual(op_args, ["C:/a.txt"])

    def test_json_flag(self):
        import cli
        g, op, op_args, cmd = cli.parse_musepc_args(
            ["--json", "--status"])
        self.assertTrue(g["json"])
        self.assertEqual(op, "status")

    def test_command_with_dashes(self):
        import cli
        g, op, op_args, cmd = cli.parse_musepc_args(
            ["grep", "-rn", "foo", "."])
        self.assertEqual(op, "exec")
        self.assertEqual(cmd, ["grep", "-rn", "foo", "."])


if __name__ == "__main__":
    unittest.main(verbosity=2)
