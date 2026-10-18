"""Tests for the WebSocket transport (GET /api/socket/ws?id=).

A stdlib-only raw RFC 6455 client (no third-party deps): opens a TCP
session over HTTP, performs the upgrade handshake by hand, then speaks
masked client frames and parses server frames.

Ports without a hijackable HTTP layer (Kotlin/JDK HttpServer, PHP/php -S)
do not implement this route: the whole module skips when /api/help has
no socket_ws capability.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import unittest

from tests.helpers import base_url, json_request, quote, ti, tv
from tests.test_socket import (
    EchoServer, free_port, goodbye_server, recv_all,
    sock_close, sock_open, sock_recv, sock_send,
)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def expected_accept(key):
    """RFC 6455 accept for a client key (server must echo exactly this)."""
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()

OP_CONT, OP_TEXT, OP_BIN = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


def ws_capable():
    """True when the server advertises the socket_ws route."""
    status, _, data = json_request("GET", "/api/help", auth=False)
    assert status == 200
    return any(r["name"] == "socket_ws" for r in data["routes"])


class RawWS:
    """Minimal blocking RFC 6455 client over a raw socket."""

    def __init__(self, target, headers=None, timeout=10):
        from urllib.parse import urlparse
        parsed = urlparse(base_url())
        hostport = parsed.hostname + (":%d" % parsed.port
                                      if parsed.port else "")
        self.sock = socket.create_connection(
            (parsed.hostname, parsed.port or 80), timeout=timeout)
        self.sock.settimeout(timeout)
        self.key = base64.b64encode(os.urandom(16)).decode()
        lines = ["GET %s HTTP/1.1" % target, "Host: %s" % hostport,
                 "Connection: Upgrade", "Upgrade: websocket",
                 "Sec-WebSocket-Key: %s" % self.key,
                 "Sec-WebSocket-Version: 13"]
        for k, v in (headers or {}).items():
            lines.append("%s: %s" % (k, v))
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.status, self.headers, self.rest = self._read_head()

    def _read_head(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        hdrs = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                hdrs[k.decode().strip().lower()] = v.decode().strip()
        return status, hdrs, rest

    def _recv_exact(self, n):
        buf = getattr(self, "_buf", b"") + self.rest
        self.rest = b""
        while len(buf) < n:
            chunk = self.sock.recv(max(4096, n - len(buf)))
            if not chunk:
                raise AssertionError("connection closed mid-frame")
            buf += chunk
        self._buf = buf[n:]
        return buf[:n]

    def send_frame(self, opcode, data=b"", fin=True, masked=True):
        if isinstance(data, str):
            data = data.encode("utf-8")
        hdr = bytes([(0x80 if fin else 0) | opcode])
        if len(data) < 126:
            hdr += bytes([(0x80 if masked else 0) | len(data)])
        elif len(data) < 65536:
            hdr += bytes([(0x80 if masked else 0) | 126]) + struct.pack(
                "!H", len(data))
        else:
            hdr += bytes([(0x80 if masked else 0) | 127]) + struct.pack(
                "!Q", len(data))
        if masked:
            mask = os.urandom(4)
            hdr += mask
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(hdr + data)

    def recv_frame(self):
        """Return (fin, opcode, payload) for one server frame."""
        hdr = self._recv_exact(2)
        fin = bool(hdr[0] & 0x80)
        opcode = hdr[0] & 0x0F
        assert hdr[1] & 0x80 == 0, "server must not mask"
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        return fin, opcode, self._recv_exact(length)

    def recv_message(self):
        """Reassemble one data message; answer pings; surface closes."""
        parts, op = [], None
        while True:
            fin, opcode, payload = self.recv_frame()
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(
                    payload) >= 2 else None
                return ("close", code, payload[2:])
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BIN):
                assert op is None, "new message before previous finished"
                op = opcode
            elif opcode == OP_CONT:
                assert op is not None, "orphan continuation"
            else:
                raise AssertionError("unexpected opcode %r" % opcode)
            parts.append(payload)
            if fin:
                return ("msg", op, b"".join(parts))

    def close(self, code=1000):
        try:
            self.send_frame(OP_CLOSE, struct.pack("!H", code))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def close_clean(self, code=1000):
        """Full close handshake: send close, wait for the echo, then drop."""
        self.send_frame(OP_CLOSE, struct.pack("!H", code))
        fin, opcode, _ = self.recv_frame()
        assert opcode == OP_CLOSE, opcode
        self.sock.close()


def upgrade(sid, token=True, extra_headers=None, method="GET",
            omit_upgrade=False, assert101=True):
    """Upgrade helper returning a RawWS (asserts 101 + valid accept)."""
    from tests.helpers import base_url  # noqa
    target = "/api/socket/ws?id=" + quote(sid)
    if token:
        target += "&token=" + os.environ["GNATIVE_TEST_TOKEN"]
    headers = dict(extra_headers or {})
    if omit_upgrade:
        raw = RawWS.__new__(RawWS)
        from urllib.parse import urlparse
        parsed = urlparse(base_url())
        hostport = parsed.hostname + (":%d" % parsed.port
                                      if parsed.port else "")
        raw.sock = socket.create_connection(
            (parsed.hostname, parsed.port or 80), timeout=10)
        raw.sock.settimeout(10)
        raw.rest, raw._buf = b"", b""
        head = ["%s %s HTTP/1.1" % (method, target),
                "Host: %s" % hostport,
                "Connection: keep-alive"]
        if method != "GET":
            # HttpListener answers 411 to bodyless POST/PUT/DELETE
            # without one; harmless everywhere else.
            head.append("Content-Length: 0")
        for k, v in headers.items():
            head.append("%s: %s" % (k, v))
        raw.sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        raw.status, raw.headers, raw.rest = raw._read_head()
        return raw
    ws = RawWS(target, headers=headers)
    if assert101:
        assert ws.status == 101, (ws.status, ws.headers)
        accept = ws.headers.get("sec-websocket-accept")
        assert accept == expected_accept(ws.key), (accept, ws.key)
    return ws


class TestWsAcceptVector(unittest.TestCase):
    """RFC 6455 section 1.3 known-answer vector (no server needed).

    Guards the handshake GUID itself: a wrong GUID still yields 101
    against our own raw client, but real browsers reject the accept
    ("Incorrect 'Sec-WebSocket-Accept' header value") and the session
    dies. This test pins the wire value independently of any server.
    """

    def test_rfc_example_vector(self):
        self.assertEqual(
            expected_accept("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class TestSocketWs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ws_capable():
            raise unittest.SkipTest("no socket_ws route on this port")
        cls.echo = EchoServer()
        cls.echo.start()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "echo"):
            cls.echo.stop()

    def open_session(self):
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        return data["id"]

    def test_ws_echo_binary(self):
        sid = self.open_session()
        try:
            ws = upgrade(sid)
            try:
                ws.send_frame(OP_BIN, b"hello-ws")
                kind, op, payload = ws.recv_message()
                self.assertEqual((kind, op), ("msg", OP_BIN))
                self.assertEqual(payload, b"hello-ws")
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_echo_text(self):
        sid = self.open_session()
        try:
            ws = upgrade(sid)
            try:
                ws.send_frame(OP_TEXT, "héllo-ws")
                kind, op, payload = ws.recv_message()
                self.assertEqual(kind, "msg")
                self.assertEqual(op, OP_BIN)  # TCP bytes come back binary
                self.assertEqual(payload, "héllo-ws".encode("utf-8"))
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_fragmented(self):
        sid = self.open_session()
        try:
            ws = upgrade(sid)
            try:
                ws.send_frame(OP_BIN, b"hell", fin=False)
                ws.send_frame(OP_CONT, b"o-ws", fin=True)
                kind, op, payload = ws.recv_message()
                self.assertEqual((kind, op), ("msg", OP_BIN))
                self.assertEqual(payload, b"hello-ws")
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_large_256k(self):
        sid = self.open_session()
        blob = bytes((i % 251 for i in range(256 * 1024)))
        try:
            ws = upgrade(sid)
            try:
                ws.send_frame(OP_BIN, blob)
                got = b""
                while len(got) < len(blob):
                    kind, op, payload = ws.recv_message()
                    self.assertEqual(kind, "msg")
                    got += payload
                self.assertEqual(got, blob)
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_ping_pong(self):
        sid = self.open_session()
        try:
            ws = upgrade(sid)
            try:
                ws.send_frame(OP_PING, b"abc")
                fin, opcode, payload = ws.recv_frame()
                self.assertTrue(fin)
                self.assertEqual(opcode, OP_PONG)
                self.assertEqual(payload, b"abc")
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_tcp_eof_closes(self):
        port = goodbye_server()
        status, _, data = sock_open("127.0.0.1", ti(port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            ws = upgrade(sid)
            try:
                ws.send_frame(OP_BIN, b"hi")
                kind, op, payload = ws.recv_message()
                self.assertEqual((kind, op), ("msg", OP_BIN))
                self.assertEqual(payload, b"bye")
                kind, code, _ = ws.recv_message()
                self.assertEqual(kind, "close")
                self.assertEqual(code, 1000)
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_close_ends_session(self):
        sid = self.open_session()
        try:
            ws = upgrade(sid)
            ws.close_clean()  # handshake first: server ended it by now
            # A WS close is a session close: HTTP sees 404 afterwards.
            status, _, _ = sock_send(sid, "again")
            self.assertEqual(status, 404)
            status, _, _ = sock_recv(sid, timeout="1")
            self.assertEqual(status, 404)
        finally:
            sock_close(sid)  # already gone -> 404, harmless

    def test_ws_http_refused_while_attached(self):
        sid = self.open_session()
        try:
            ws = upgrade(sid)
            try:
                status, _, _ = sock_send(sid, "x")
                self.assertEqual(status, 400)
                status, _, _ = sock_recv(sid, timeout="1")
                self.assertEqual(status, 400)
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_http_close_kills_ws(self):
        sid = self.open_session()
        ws = upgrade(sid)
        try:
            status, _, _ = sock_close(sid)
            self.assertEqual(status, 200)
            kind, _, _ = ws.recv_message()
            self.assertEqual(kind, "close")
        finally:
            ws.close()

    def test_ws_unmasked_rejected(self):
        sid = self.open_session()
        try:
            ws = upgrade(sid)
            try:
                ws.send_frame(OP_BIN, b"nope", masked=False)
                kind, code, _ = ws.recv_message()
                self.assertEqual(kind, "close")
                self.assertEqual(code, 1002)
            finally:
                ws.close()
        finally:
            sock_close(sid)

    def test_ws_unknown_id_404(self):
        ws = upgrade("nope", assert101=False)
        self.assertEqual(ws.status, 404)
        ws.sock.close()

    def test_ws_missing_id_400(self):
        from urllib.parse import urlparse
        parsed = urlparse(base_url())
        hostport = parsed.hostname + (":%d" % parsed.port
                                      if parsed.port else "")
        s = socket.create_connection((parsed.hostname, parsed.port or 80),
                                     timeout=10)
        try:
            s.sendall(
                ("GET /api/socket/ws?token=" +
                 os.environ["GNATIVE_TEST_TOKEN"] +
                 " HTTP/1.1\r\nHost: " + hostport + "\r\n"
                 "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 "Sec-WebSocket-Key: eHl6\r\nSec-WebSocket-Version: 13\r\n\r\n"
                 ).encode())
            head = b""
            while b"\r\n\r\n" not in head:
                head += s.recv(4096)
            # Any HTTP/1.x 400 (ports differ: stdlib servers answer 1.0).
            self.assertIn(b" 400 ", head.split(b"\r\n")[0])
        finally:
            s.close()

    def test_ws_no_token_401(self):
        ws = upgrade("whatever", token=False, assert101=False)
        self.assertEqual(ws.status, 401)
        ws.sock.close()

    def test_ws_plain_get_400(self):
        sid = self.open_session()
        try:
            raw = upgrade(sid, omit_upgrade=True)
            self.assertEqual(raw.status, 400)
            raw.sock.close()
        finally:
            sock_close(sid)

    def test_ws_wrong_method_405(self):
        sid = self.open_session()
        try:
            raw = upgrade(sid, omit_upgrade=True, method="POST")
            self.assertEqual(raw.status, 405)
            raw.sock.close()
        finally:
            sock_close(sid)

    def test_help_has_socket_ws(self):
        status, _, data = json_request("GET", "/api/help", auth=False)
        self.assertEqual(status, 200)
        names = {r["name"] for r in data["routes"]}
        self.assertIn("socket_ws", names)
        self.assertEqual(tv(data["count"]), len(data["routes"]))


if __name__ == "__main__":
    unittest.main()
