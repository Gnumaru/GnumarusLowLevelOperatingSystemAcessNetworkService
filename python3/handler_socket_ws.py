"""WebSocket (RFC 6455) transport for socket sessions (mixed into Handler).

GET /api/socket/ws?id=<sid> with `Upgrade: websocket` attaches a
WebSocket to an existing session. After the 101 handshake, WS message
payloads flow transparently client->TCP (text frames as UTF-8 bytes)
and TCP bytes flow back as binary frames; ping gets an auto-pong. A
close from either side, or any TCP/protocol error, ends the whole
session (a WS close is a session close, exactly like HTTP close).
Stdlib only: manual 101 over the hijacked connection plus
hashlib/base64 framing. Assumes the upgrade request is not pipelined
(true for browsers and the test client): frame reads go straight to
the raw connection.
"""

import base64
import hashlib
import socket
import struct
import threading
import time
from handler_socket import Sessions, _sessions_lock


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE = 4 * 1024 * 1024
WS_WRITE_SECS = 30

WS_CONT, WS_TEXT, WS_BIN = 0x0, 0x1, 0x2
WS_CLOSE, WS_PING, WS_PONG = 0x8, 0x9, 0xA


class _WsProtoError(Exception):
    pass


def _ws_accept_key(key):
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _ws_has_token(header_value, token):
    return any(part.strip().lower() == token
               for part in header_value.split(","))


class _WsBridge:
    """Owns a hijacked client connection; wmu serializes all writes."""

    def __init__(self, conn):
        self.conn = conn
        self.wmu = threading.Lock()
        self.closed = False

    def _send_raw(self, opcode, payload):
        if len(payload) < 126:
            hdr = bytes([0x80 | opcode, len(payload)])
        elif len(payload) < 65536:
            hdr = bytes([0x80 | opcode, 126]) + struct.pack(
                "!H", len(payload))
        else:
            hdr = bytes([0x80 | opcode, 127]) + struct.pack(
                "!Q", len(payload))
        self.conn.sendall(hdr + payload)

    def send_frame(self, opcode, payload):
        with self.wmu:
            if self.closed:
                raise _WsProtoError("websocket already closed")
            self.conn.settimeout(WS_WRITE_SECS)
            try:
                self._send_raw(opcode, payload)
            finally:
                self.conn.settimeout(None)

    def send_close(self, code, reason=b""):
        with self.wmu:
            if self.closed:
                return
            self.closed = True
            payload = struct.pack("!H", code) + reason
            self.conn.settimeout(5)
            try:
                self._send_raw(WS_CLOSE, payload)
            except OSError:
                pass
            finally:
                try:
                    self.conn.settimeout(None)
                except OSError:
                    pass

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed mid-frame")
            buf += chunk
        return buf

    def _read_frame(self):
        """Return (fin, opcode, payload, is_control).

        Parsing runs to completion BEFORE validation (bounded by the
        cap), so a fail echo goes out on a clean FIN instead of an
        RST. Only an unbounded declaration aborts mid-stream.
        """
        def fail(code):
            self.send_close(code)
            raise _WsProtoError("protocol error")

        hdr = self._read_exact(2)
        fin = bool(hdr[0] & 0x80)
        opcode = hdr[0] & 0x0F
        rsv = bool(hdr[0] & 0x70)
        masked = bool(hdr[1] & 0x80)
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > WS_MAX_MESSAGE:
                fail(1009)  # abortive: cannot bound
        is_ctl = opcode >= 0x8
        if length > WS_MAX_MESSAGE:
            fail(1009)  # abortive: cannot bound
        # Unmasked client frames carry no key: consume payload only.
        if masked:
            key = self._read_exact(4)
            payload = self._read_exact(length)
            payload = bytes(
                b ^ key[i % 4] for i, b in enumerate(payload))
        else:
            payload = self._read_exact(length)
        if rsv:
            fail(1002)
        if not masked:
            fail(1002)  # clients MUST mask
        if is_ctl and (not fin or length > 125):
            fail(1002)
        return fin, opcode, payload, is_ctl

    def read_message(self):
        """Reassemble one data message; answer ping inline.

        Protocol violations are echoed inline (the session is doomed
        anyway). A clean peer close is NOT echoed: returns
        ("close", code, reason) so the caller ends the session
        BEFORE echoing — the echo then implies "gone". Returns
        ("msg", opcode, payload), or raises on transport error.
        """
        msg_op = None
        parts = []
        total = 0
        while True:
            fin, opcode, payload, is_ctl = self._read_frame()
            if is_ctl:
                if opcode == WS_CLOSE:
                    if len(payload) == 1:
                        self.send_close(1002)
                        raise _WsProtoError("bad close payload")
                    code = 1000
                    reason = b""
                    if len(payload) >= 2:
                        code = struct.unpack("!H", payload[:2])[0]
                        reason = payload[2:]
                    return ("close", code, reason)
                if opcode == WS_PING:
                    try:
                        self.send_frame(WS_PONG, payload)
                    except (OSError, _WsProtoError):
                        raise _WsProtoError("dead bridge")
                    continue
                if opcode == WS_PONG:
                    continue
                self.send_close(1002)
                raise _WsProtoError("bad control opcode")
            if opcode == WS_CONT:
                if msg_op is None:
                    self.send_close(1002)
                    raise _WsProtoError("orphan continuation")
            elif opcode in (WS_TEXT, WS_BIN):
                if msg_op is not None:
                    self.send_close(1002)
                    raise _WsProtoError("interleaved message")
                msg_op = opcode
            else:
                self.send_close(1003)
                raise _WsProtoError("bad data opcode")
            total += len(payload)
            if total > WS_MAX_MESSAGE:
                self.send_close(1009)
                raise _WsProtoError("message too big")
            parts.append(payload)
            if fin:
                msg = b"".join(parts)
                if msg_op == WS_TEXT:
                    try:
                        msg.decode("utf-8")
                    except UnicodeDecodeError:
                        self.send_close(1007)
                        raise _WsProtoError("bad utf-8")
                return ("msg", msg_op, msg)


def _touch_session(sid, sess):
    with _sessions_lock:
        if Sessions.get(sid) is sess:
            sess["last"] = time.time()


def _mark_session_eof(sid, sess):
    with _sessions_lock:
        if Sessions.get(sid) is sess:
            sess["eof"] = True
            sess["last"] = time.time()


class HandlerSocketWsMixin:
    """WebSocket attach endpoint (mixed into Handler)."""

    def _ws_header(self, name):
        return self.headers.get(name, "")

    def _handle_socket_ws(self, query):
        connection = self._ws_header("Connection")
        upgrade = self._ws_header("Upgrade")
        if (not _ws_has_token(connection, "upgrade")
                or upgrade.strip().lower() != "websocket"):
            self._send_error_json(
                400, "This route requires a WebSocket upgrade "
                     "(Upgrade: websocket).")
            return
        if self._ws_header("Sec-WebSocket-Version").strip() != "13":
            self._send_error_json(
                400, "Unsupported WebSocket version (need 13).")
            return
        key = self._ws_header("Sec-WebSocket-Key").strip()
        if not key:
            self._send_error_json(400, "Missing Sec-WebSocket-Key.")
            return
        found = self._socket_session(query)
        if found[0] is None:
            return
        sid, sess = found
        with _sessions_lock:
            if sess.get("ws_attached"):
                self._send_error_json(
                    400, "Socket session is already attached to a "
                         "websocket.")
                return
            if not sess["recv_lock"].acquire(blocking=False):
                self._send_error_json(
                    400, "Another recv is already pending on this "
                         "session.")
                return
            sess["recv_lock"].release()
            sess["ws_attached"] = True
            sess["last"] = time.time()
        conn = self.connection
        try:
            conn.sendall(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 "Sec-WebSocket-Accept: %s\r\n\r\n"
                 % _ws_accept_key(key)).encode("ascii"))
        except OSError:
            with _sessions_lock:
                if Sessions.get(sid) is sess:
                    sess["ws_attached"] = False
            return
        bridge = _WsBridge(conn)
        finished = threading.Event()
        pump = threading.Thread(target=self._ws_tcp_to_ws,
                                args=(bridge, sid, sess, finished),
                                daemon=True)
        pump.start()
        try:
            self._ws_to_tcp(bridge, sid, sess)
        finally:
            finished.set()
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            # A WS close is a session close (same as HTTP close).
            self._socket_close_session(sid, sess)

    def _ws_pump_grace(self, bridge, finished):
        # TCP ended: give the peer a grace period to echo close,
        # then force the WS reader out so nothing lingers.
        finished.wait(2)
        try:
            bridge.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _ws_tcp_to_ws(self, bridge, sid, sess, finished):
        buf_size = 32768
        sock = sess["sock"]
        while True:
            try:
                chunk = sock.recv(buf_size)
            except OSError:
                try:
                    bridge.send_close(1011)
                except (_WsProtoError, OSError):
                    pass
                self._ws_pump_grace(bridge, finished)
                return
            if chunk:
                try:
                    bridge.send_frame(WS_BIN, chunk)
                except (_WsProtoError, OSError):
                    return
                _touch_session(sid, sess)
                continue
            _mark_session_eof(sid, sess)
            try:
                bridge.send_close(1000)
            except (_WsProtoError, OSError):
                pass
            self._ws_pump_grace(bridge, finished)
            return

    def _ws_to_tcp(self, bridge, sid, sess):
        sock = sess["sock"]
        while True:
            try:
                res = bridge.read_message()
            except (_WsProtoError, ConnectionError, OSError):
                # Violation echoes already went out inline (or the
                # transport died: nothing to echo to).
                self._socket_close_session(sid, sess)
                return
            if res[0] == "close":
                # End the session BEFORE echoing, so the echo
                # deterministically implies "gone".
                _, code, reason = res
                self._socket_close_session(sid, sess)
                try:
                    bridge.send_close(code, reason)
                except (_WsProtoError, OSError):
                    pass
                return
            _, _, payload = res
            try:
                sock.settimeout(30)
                try:
                    sock.sendall(payload)
                finally:
                    sock.settimeout(None)
            except OSError:
                self._socket_close_session(sid, sess)
                return
            _touch_session(sid, sess)
