#!/usr/bin/env python3
"""tls_resume: TLS session resumption for mused's upstream HTTPS connections.

Implements Gemini-4 review section 3.3 ("Daemon 到 VPS 的 TLS 会话复用").

Why: when a pooled upstream connection is dropped after 45s idle and rebuilt,
a full TLS handshake costs 2xRTT (TLS 1.2) plus ECDHE + cert-chain transfer.
With TLS Session Ticket / TLS 1.3 PSK resumption the handshake drops to 1xRTT
and skips the certificate chain entirely.

Critical pitfall (TLS 1.3): the server's NewSessionTicket message arrives
*after* the handshake completes, piggybacked on the first HTTP response flow.
Reading ``sock.session`` immediately after ``connect()`` returns None / a
stale empty session. The session MUST be extracted after the first
``getresponse()`` instead. This module enforces that ordering: ``connect()``
never touches ``sock.session``; ``getresponse()`` does.

Security: 0-RTT early data is NEVER enabled (replay attack would re-execute
non-idempotent /cmd). Resumption only shortens the handshake, it does not
send application data before the handshake completes.

Usage:
    cache = TLSSessionCache()
    conn = ResumableHTTPSConnection(host, port,
                                    ssl_context=ctx,
                                    session_getter=cache.get,
                                    session_setter=cache.set)
    conn.connect()          # injects cached session (if any)
    conn.request("GET", "/")
    resp = conn.getresponse()  # extracts + caches the fresh session
"""
import http.client
import socket
import ssl
import threading


class TLSSessionCache:
    """Thread-safe holder of the latest TLS session for resumption.

    Stores at most one session (the freshest). Also tracks handshake stats
    so operators can verify resumption is actually happening.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._session = None
        self._resumed = 0   # handshakes that reused a cached session
        self._full = 0      # full handshakes (no usable cached session)

    def get(self):
        """Return the cached SSLSession, or None."""
        with self._lock:
            return self._session

    def set(self, session):
        """Cache the freshest session. Ignores None (TLS 1.3 pre-ticket)."""
        if session is None:
            return
        with self._lock:
            self._session = session

    def note_handshake(self, reused):
        """Record whether the last handshake resumed (True) or not."""
        with self._lock:
            if reused:
                self._resumed += 1
            else:
                self._full += 1

    def stats(self):
        with self._lock:
            return {"resumed": self._resumed, "full": self._full,
                    "has_cached": self._session is not None}


class ResumableHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection with TLS session resumption (stdlib only).

    ``session_getter`` / ``session_setter`` are zero-arg / one-arg callables
    (typically bound methods of a shared :class:`TLSSessionCache`).
    """

    def __init__(self, host, port=None, ssl_context=None,
                 session_getter=None, session_setter=None, **kwargs):
        # http.client.HTTPSConnection takes the context as ``context=``.
        super().__init__(host, port, context=ssl_context, **kwargs)
        self._get_session = session_getter or (lambda: None)
        self._set_session = session_setter or (lambda s: None)
        self._note_handshake = lambda reused: None

    def attach_handshake_stats(self, note_fn):
        """Optional: note_fn(reused: bool) called after each handshake."""
        self._note_handshake = note_fn or (lambda reused: None)

    def _tls_wrap(self, sock, server_hostname):
        """Wrap a connected TCP socket in TLS, attempting session resumption.

        Never reads sock.session here -- under TLS 1.3 the ticket has not
        arrived yet at this point.
        """
        cached = self._get_session()
        try:
            self.sock = self._context.wrap_socket(
                sock,
                server_hostname=server_hostname,
                session=cached,          # None => normal full handshake
            )
        except Exception:
            # do not leak the raw socket if the handshake fails
            try:
                sock.close()
            except Exception:
                pass
            raise
        # do_handshake_on_connect=True (default), so session_reused is valid now
        try:
            reused = bool(getattr(self.sock, "session_reused", False))
        except Exception:
            reused = False
        self._note_handshake(reused)

    def connect(self):
        # 1. plain TCP (subclasses doing CONNECT tunnels override this but
        #    should reuse _tls_wrap for the TLS step)
        sock = socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 2. TLS with cached-session injection; NEVER extract session here
        self._tls_wrap(sock, self.host)

    def getresponse(self):
        resp = super().getresponse()
        # 3. KEY STEP: TLS 1.3's NewSessionTicket arrives with/after the first
        # HTTP response, so only now is sock.session valid. Extract + cache.
        try:
            sess = getattr(self.sock, "session", None)
        except Exception:
            sess = None
        if sess is not None:
            self._set_session(sess)
        return resp
