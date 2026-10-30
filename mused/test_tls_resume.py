#!/usr/bin/env python3
"""test_tls_resume.py: mock tests for TLS session resumption (Gemini-4 §3.3).

No network access. Verifies:
  1. connect() injects the cached session into wrap_socket (getter called).
  2. connect() NEVER extracts/sets the session (TLS 1.3 ticket arrives later).
  3. getresponse() extracts sock.session AFTER the first HTTP response and
     calls the setter (the only valid extraction point under TLS 1.3).
  4. getresponse() does not call the setter when session is None.
  5. TLSSessionCache is thread-safe, latest-wins, ignores None.
  6. wrap_socket failure closes the raw socket (no leak).
  7. TCP_NODELAY is set on new connections.

Run: python3 -m pytest test_tls_resume.py -v   (or: python3 test_tls_resume.py)
"""
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))

import tls_resume
from tls_resume import ResumableHTTPSConnection, TLSSessionCache


def _make_conn(cached_session=None, sock_session_after_resp="sess-2",
               reused=False):
    """Build a ResumableHTTPSConnection with mocked transport.

    Returns (conn, getter, setter, ctx, raw_sock, wrapped_sock).
    """
    getter = mock.Mock(return_value=cached_session)
    setter = mock.Mock()
    ctx = mock.Mock()
    raw_sock = mock.Mock()
    wrapped = mock.Mock()
    wrapped.session = None          # TLS 1.3: nothing right after handshake
    wrapped.session_reused = reused
    ctx.wrap_socket.return_value = wrapped
    conn = ResumableHTTPSConnection(
        "example.com", 443,
        ssl_context=ctx,
        session_getter=getter,
        session_setter=setter)
    # the "post-response" session the fake server would deliver
    conn._fake_post_resp_session = sock_session_after_resp
    return conn, getter, setter, ctx, raw_sock, wrapped


class TestConnectInjectsSession(unittest.TestCase):
    def test_cached_session_injected_into_wrap_socket(self):
        conn, getter, setter, ctx, raw, wrapped = _make_conn(
            cached_session="sess-1")
        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw) as cc:
            conn.connect()
        cc.assert_called_once()
        getter.assert_called_once_with()
        # session= passed through to wrap_socket
        _, kwargs = ctx.wrap_socket.call_args
        self.assertEqual(kwargs["session"], "sess-1")
        self.assertEqual(kwargs["server_hostname"], "example.com")
        self.assertIs(conn.sock, wrapped)

    def test_no_cached_session_passes_none(self):
        conn, getter, setter, ctx, raw, wrapped = _make_conn(
            cached_session=None)
        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw):
            conn.connect()
        _, kwargs = ctx.wrap_socket.call_args
        self.assertIsNone(kwargs["session"])

    def test_connect_never_sets_session(self):
        """THE TLS 1.3 pitfall: sock.session must NOT be read in connect()."""
        conn, getter, setter, ctx, raw, wrapped = _make_conn()
        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw):
            conn.connect()
        setter.assert_not_called()

    def test_tcp_nodelay_set(self):
        conn, getter, setter, ctx, raw, wrapped = _make_conn()
        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw):
            conn.connect()
        raw.setsockopt.assert_called_with(
            socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def test_handshake_failure_closes_raw_socket(self):
        conn, getter, setter, ctx, raw, wrapped = _make_conn()
        ctx.wrap_socket.side_effect = OSError("handshake boom")
        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw):
            with self.assertRaises(OSError):
                conn.connect()
        raw.close.assert_called_once()

    def test_handshake_stats_hook(self):
        conn, getter, setter, ctx, raw, wrapped = _make_conn(reused=True)
        noted = []
        conn.attach_handshake_stats(noted.append)
        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw):
            conn.connect()
        self.assertEqual(noted, [True])


class TestGetresponseExtractsSession(unittest.TestCase):
    def _connected(self, **kw):
        conn, getter, setter, ctx, raw, wrapped = _make_conn(**kw)
        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw):
            conn.connect()
        # simulate the TLS 1.3 NewSessionTicket arriving with the response
        wrapped.session = conn._fake_post_resp_session
        return conn, getter, setter, wrapped

    def test_session_extracted_after_response(self):
        conn, getter, setter, wrapped = self._connected(
            sock_session_after_resp="sess-2")
        fake_resp = mock.Mock()
        with mock.patch(
                "http.client.HTTPConnection.getresponse",
                return_value=fake_resp) as gr:
            out = conn.getresponse()
        gr.assert_called_once_with()
        self.assertIs(out, fake_resp)
        setter.assert_called_once_with("sess-2")

    def test_setter_not_called_when_session_none(self):
        conn, getter, setter, wrapped = self._connected(
            sock_session_after_resp=None)
        with mock.patch("http.client.HTTPConnection.getresponse",
                        return_value=mock.Mock()):
            conn.getresponse()
        setter.assert_not_called()

    def test_full_cycle_getter_sees_fresh_session(self):
        """End-to-end ordering: connect#1 (no cache) -> response caches
        session -> connect#2 reuses it."""
        cache = TLSSessionCache()
        ctx = mock.Mock()
        wrapped1, wrapped2 = mock.Mock(), mock.Mock()
        wrapped1.session, wrapped1.session_reused = None, False
        wrapped2.session, wrapped2.session_reused = None, True
        raw = mock.Mock()
        ctx.wrap_socket.side_effect = [wrapped1, wrapped2]

        def new_conn():
            return ResumableHTTPSConnection(
                "example.com", 443, ssl_context=ctx,
                session_getter=cache.get, session_setter=cache.set)

        with mock.patch("tls_resume.socket.create_connection",
                        return_value=raw):
            c1 = new_conn()
            c1.connect()
            # TLS 1.3: ticket arrives with first response
            wrapped1.session = "ticket-A"
            with mock.patch("http.client.HTTPConnection.getresponse",
                            return_value=mock.Mock()):
                c1.getresponse()
            self.assertEqual(cache.get(), "ticket-A")

            c2 = new_conn()
            c2.connect()
        # second handshake injected the cached ticket
        self.assertEqual(ctx.wrap_socket.call_args_list[1][1]["session"],
                         "ticket-A")


class TestTLSSessionCache(unittest.TestCase):
    def test_set_get_roundtrip(self):
        c = TLSSessionCache()
        self.assertIsNone(c.get())
        c.set("s1")
        self.assertEqual(c.get(), "s1")

    def test_none_ignored(self):
        c = TLSSessionCache()
        c.set("s1")
        c.set(None)
        self.assertEqual(c.get(), "s1")

    def test_latest_wins(self):
        c = TLSSessionCache()
        c.set("s1")
        c.set("s2")
        self.assertEqual(c.get(), "s2")

    def test_thread_safety(self):
        c = TLSSessionCache()
        errors = []

        def worker(i):
            try:
                for j in range(200):
                    c.set(f"s-{i}-{j}")
                    c.get()
                    c.note_handshake(j % 2 == 0)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        st = c.stats()
        self.assertEqual(st["resumed"] + st["full"], 8 * 200)
        self.assertTrue(st["has_cached"])

    def test_stats(self):
        c = TLSSessionCache()
        c.note_handshake(True)
        c.note_handshake(False)
        c.note_handshake(True)
        self.assertEqual(c.stats(), {"resumed": 2, "full": 1,
                                     "has_cached": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
