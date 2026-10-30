#!/usr/bin/env python3
"""Regression tests: compress.py integration in mused.Upstream.request().

Covers P2 of CODE_REVIEW.md:
  - request bodies >= 2KB are zlib-compressed with
    "Content-Encoding: deflate" (Content-Length updated after compression)
  - small bodies pass through untouched
  - compress_enabled=false bypasses both directions
  - deflate responses are decompressed per Content-Encoding
  - corrupt deflate raises RuntimeError (never delivered silently)

Pure stdlib, no network: the connection pool is faked.
Run: python3 -m pytest test_mused_compress.py -v
"""
import json
import sys
import zlib

import pytest

sys.path.insert(0, ".")

import mused  # noqa: E402
from compress import compress as zlib_compress  # noqa: E402


class FakeResp:
    def __init__(self, status, data, headers, will_close=False):
        self.status = status
        self._data = data
        self.headers = headers
        self.will_close = will_close

    def read(self):
        return self._data


class FakeConn:
    def __init__(self, resp):
        self._resp = resp
        self.sent = None  # (method, url, body, headers)

    def request(self, method, url, body=None, headers=None):
        self.sent = (method, url, body, dict(headers))

    def getresponse(self):
        return self._resp


class FakePC:
    def __init__(self, conn):
        self.conn = conn


class FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.returned = []  # is_broken flags

    def get_connection(self):
        return FakePC(self._conn)

    def return_connection(self, pc, is_broken=False):
        self.returned.append(is_broken)


def make_upstream(status=200, resp_body=b"{}", resp_headers=None,
                  compress_enabled=True):
    up = mused.Upstream.__new__(mused.Upstream)
    up._cfg = {"sni": "ai.vps38.local",
               "compress_enabled": compress_enabled}
    resp = FakeResp(status, resp_body, resp_headers or {})
    up._pool = FakePool(FakeConn(resp))
    return up


def sent_of(up):
    return up._pool._conn.sent  # (method, url, body, headers)


def test_large_body_compressed():
    big = {"command": "x" * 5000}
    up = make_upstream()
    st, data = up.request("tok", "POST", "/cmd", big)
    method, url, body, hdrs = sent_of(up)
    assert hdrs.get("Content-Encoding") == "deflate"
    assert body == zlib_compress(json.dumps(big).encode("utf-8"))[0]
    assert int(hdrs["Content-Length"]) == len(body)
    assert (st, data) == (200, b"{}")


def test_small_body_passthrough():
    small = {"command": "echo hi"}
    up = make_upstream()
    up.request("tok", "POST", "/cmd", small)
    _, _, body, hdrs = sent_of(up)
    assert "Content-Encoding" not in hdrs
    assert body == json.dumps(small).encode("utf-8")


def test_disabled_bypasses_compression():
    big = {"command": "x" * 5000}
    up = make_upstream(compress_enabled=False)
    up.request("tok", "POST", "/cmd", big)
    _, _, body, hdrs = sent_of(up)
    assert "Content-Encoding" not in hdrs
    assert body == json.dumps(big).encode("utf-8")


def test_deflate_response_decompressed():
    raw = b'{"ok":true,"data":"' + b"y" * 3000 + b'"}'
    up = make_upstream(resp_body=zlib.compress(raw, 1),
                       resp_headers={"Content-Encoding": "deflate"})
    st, data = up.request("tok", "POST", "/cmd", {"command": "x"})
    assert st == 200
    assert data == raw


def test_corrupt_deflate_raises():
    up = make_upstream(resp_body=b"not-deflate-at-all!!!!",
                       resp_headers={"Content-Encoding": "deflate"})
    with pytest.raises(RuntimeError, match="decompress"):
        up.request("tok", "POST", "/cmd", {"command": "x"})


def test_body_none_path_unaffected():
    up = make_upstream(status=401, resp_body=b"unauthorized")
    st, data = up.request("", "GET", "/status", None)
    assert (st, data) == (401, b"unauthorized")
    _, _, body, hdrs = sent_of(up)
    assert body is None
    assert "Content-Encoding" not in hdrs
