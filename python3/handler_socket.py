"""Transparent TCP socket proxy (mixed into Handler).

The service holds a real TCP client socket per session and exposes a
byte-transparent tunnel over plain HTTP, so browser pages (which
cannot open raw sockets) can speak arbitrary TCP protocols through
the local service (e.g. SMTP/IMAP for a browser email client).

Sessions: POST /api/socket/open dials and returns an opaque id;
POST /api/socket/send writes bytes; GET /api/socket/recv long-polls
for bytes; POST /api/socket/close releases the session. Idle
sessions expire after SOCK_IDLE_TIMEOUT seconds; at most
SOCK_MAX_SESSIONS may be open at once.
"""

import base64
import binascii
import json
import secrets
import select
import socket
import ssl
import threading
import time
from tags import jdec, jenc


SOCK_DEFAULT_CONNECT_TIMEOUT = 5
SOCK_MAX_CONNECT_TIMEOUT = 30
SOCK_DEFAULT_RECV_TIMEOUT = 5
SOCK_MAX_RECV_TIMEOUT = 60
SOCK_DEFAULT_MAX_BYTES = 65536
SOCK_MAX_BYTES = 1024 * 1024
SOCK_MAX_SESSIONS = 128
SOCK_IDLE_TIMEOUT = 300

Sessions = {}
_sessions_lock = threading.Lock()
_reaper_started = False


def _sweep_expired():
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, sess in Sessions.items()
                   if not sess.get("ws_attached")
                   and now - sess["last"] > SOCK_IDLE_TIMEOUT]
        for sid in expired:
            sess = Sessions.pop(sid)
            try:
                sess["sock"].close()
            except OSError:
                pass


def _ensure_reaper():
    global _reaper_started
    with _sessions_lock:
        if _reaper_started:
            return
        _reaper_started = True

    def _loop():
        while True:
            time.sleep(60)
            try:
                _sweep_expired()
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()


def _parse_plain_timeout(text, default, maximum):
    """Parse a plain (query string) timeout in seconds. Returns float/None."""
    if text is None or text == "":
        return float(default)
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not (value > 0 and value <= maximum):
        return None
    return value


def _parse_tls_identity(obj):
    """Optional sni/ca_pem body fields shared by open and starttls.

    Returns (sni, ca_pem) or an error message string.
    """
    sni = ""
    if "sni" in obj and obj["sni"] is not None:
        value = obj["sni"]
        if not isinstance(value, str) or not value.strip():
            return None, "Field 'sni' must be a non-empty string."
        sni = value.strip()
    ca_pem = ""
    if "ca_pem" in obj and obj["ca_pem"] is not None:
        value = obj["ca_pem"]
        if not isinstance(value, str) or not value.strip():
            return None, "Field 'ca_pem' must be a PEM CA bundle string."
        ca_pem = value
    return (sni, ca_pem), ""


def _tls_context(ca_pem):
    """System roots plus an optional extra PEM bundle.

    Returns (context, error_message).
    """
    try:
        ctx = ssl.create_default_context()
    except ssl.SSLError as exc:
        return None, "TLS setup failed: %s" % exc
    # Match the reference (Go/Rust) verifier: plain OpenSSL defaults.
    # create_default_context enables VERIFY_X509_STRICT, which rejects
    # bare test CAs (e.g. no key-usage extension) that the reference
    # ports accept.
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        ctx.verify_flags &= ~strict
    if ca_pem:
        try:
            ctx.load_verify_locations(cadata=ca_pem)
        except (ssl.SSLError, ValueError):
            return None, "Field 'ca_pem' must be a valid PEM CA bundle."
    return ctx, ""


class HandlerSocketMixin:
    """TCP socket proxy endpoints (mixed into Handler)."""

    def _socket_session(self, query):
        ids = query.get("id", [])
        if not ids or not ids[0]:
            self._send_error_json(
                400, "Missing required query parameter: id")
            return None, None
        sid = ids[0]
        with _sessions_lock:
            sess = Sessions.get(sid)
            if sess is not None:
                sess["last"] = time.time()
        if sess is None:
            self._send_error_json(
                404, "Unknown socket session: %s" % sid)
            return None, None
        return sid, sess

    def _handle_socket_open(self):
        _ensure_reaper()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            self._send_error_json(
                400, "Content-Type must be application/json with body "
                     "{ host, port, timeout? }.")
            return
        try:
            obj = json.loads(raw.decode("utf-8") if raw else "{}")
        except (UnicodeDecodeError, ValueError):
            self._send_error_json(400, "Invalid JSON body.")
            return
        if not isinstance(obj, dict):
            self._send_error_json(400, "JSON body must be an object.")
            return

        host = obj.get("host", "")
        if not isinstance(host, str) or not host.strip():
            self._send_error_json(
                400, "Missing or empty required field: host.")
            return
        if "port" not in obj or obj["port"] is None:
            self._send_error_json(
                400, "Missing required field: port (i:<1-65535>).")
            return
        try:
            port = jdec(obj["port"])
        except ValueError:
            self._send_error_json(
                400, "Field 'port' must be an integer 1-65535 (i:<int>).")
            return
        if (isinstance(port, bool) or not isinstance(port, int)
                or port < 1 or port > 65535):
            self._send_error_json(
                400, "Field 'port' must be an integer 1-65535 (i:<int>).")
            return
        if "timeout" in obj and obj["timeout"] is not None:
            try:
                timeout = jdec(obj["timeout"])
            except ValueError:
                timeout = None
            if (isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))):
                self._send_error_json(
                    400, "Field 'timeout' must be a tagged number "
                         "(i:<int> or f:<float>).")
                return
        else:
            timeout = SOCK_DEFAULT_CONNECT_TIMEOUT
        if not (timeout > 0 and timeout <= SOCK_MAX_CONNECT_TIMEOUT):
            self._send_error_json(
                400, "Field 'timeout' must be within (0, %d]."
                     % SOCK_MAX_CONNECT_TIMEOUT)
            return
        tls_mode = False
        if "tls" in obj and obj["tls"] is not None:
            try:
                tls_mode = jdec(obj["tls"])
            except ValueError:
                tls_mode = None
            if not isinstance(tls_mode, bool):
                self._send_error_json(
                    400, "Field 'tls' must be b:true or b:false.")
                return
        (identity, identity_err) = _parse_tls_identity(obj)
        if identity_err:
            self._send_error_json(400, identity_err)
            return
        sni, ca_pem = identity
        if not sni:
            sni = host
        ctx = None
        if tls_mode:
            ctx, ctx_err = _tls_context(ca_pem)
            if ctx_err:
                self._send_error_json(400, ctx_err)
                return

        _sweep_expired()
        with _sessions_lock:
            if len(Sessions) >= SOCK_MAX_SESSIONS:
                self._send_error_json(
                    400, "Too many open socket sessions (%d)."
                    % SOCK_MAX_SESSIONS)
                return
        try:
            sock = socket.create_connection(
                (host, port), timeout=float(timeout))
        except OSError as exc:
            self._send_error_json(
                400, "Cannot connect to %s:%d: %s" % (host, port, exc))
            return
        if tls_mode:
            sock.settimeout(float(timeout))
            try:
                sock = ctx.wrap_socket(sock, server_hostname=sni)
            except (ssl.SSLError, OSError, ValueError) as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                self._send_error_json(
                    400, "TLS handshake with %s:%d failed: %s"
                    % (host, port, exc))
                return
            try:
                sock.settimeout(None)
            except OSError:
                pass
        kind = "tls" if tls_mode else "tcp"
        sid = secrets.token_urlsafe(16)
        with _sessions_lock:
            Sessions[sid] = {"sock": sock, "host": host, "port": port,
                             "kind": kind,
                             "last": time.time(), "eof": False,
                             "ws_attached": False,
                             "recv_lock": threading.Lock()}
        self._send_json({
            "id": sid,
            "kind": kind,
            "host": host,
            "port": jenc(port),
        }, status=201)

    def _socket_close_session(self, sid, sess):
        """End a TCP session (delete + shutdown + close). Idempotent."""
        with _sessions_lock:
            if Sessions.get(sid) is not sess:
                current = None
            else:
                current = Sessions.pop(sid, None)
        target = current if current is not None else sess
        try:
            try:
                target["sock"].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            target["sock"].close()
        except OSError:
            pass

    def _handle_socket_send(self, query):
        found = self._socket_session(query)
        if found[0] is None:
            return
        sid, sess = found
        with _sessions_lock:
            attached = sess.get("ws_attached", False)
        if attached:
            self._send_error_json(
                400, "Socket session is attached to a websocket "
                     "(HTTP send refused while attached).")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                obj = json.loads(raw.decode("utf-8") if raw else "{}")
            except (UnicodeDecodeError, ValueError):
                self._send_error_json(400, "Invalid JSON body.")
                return
            if not isinstance(obj, dict):
                self._send_error_json(400, "JSON body must be an object.")
                return
            content = obj.get("content", "")
            if content is None:
                content = ""
            if not isinstance(content, str):
                self._send_error_json(
                    400, "'content' must be a string.")
                return
            encoding = obj.get("encoding", "text")
            if encoding is None:
                encoding = "text"
            if not isinstance(encoding, str):
                self._send_error_json(
                    400, "Invalid encoding: (use text|base64).")
                return
            encoding = encoding.lower()
            if encoding == "text":
                data = content.encode("utf-8")
            elif encoding == "base64":
                try:
                    data = base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError):
                    self._send_error_json(
                        400, "Invalid base64 'content'.")
                    return
            else:
                self._send_error_json(
                    400, "Invalid encoding: '%s' (use text|base64)."
                    % encoding)
                return
        else:
            data = raw
        sock = sess["sock"]
        try:
            sock.sendall(data)
        except OSError as exc:
            self._send_error_json(400, "Socket error: %s" % exc)
            return
        with _sessions_lock:
            if sid in Sessions:
                Sessions[sid]["last"] = time.time()
        self._send_json({"id": sid, "bytes": jenc(len(data))})

    def _handle_socket_recv(self, query):
        timeouts = query.get("timeout", [])
        timeout = _parse_plain_timeout(
            timeouts[0] if timeouts else None,
            SOCK_DEFAULT_RECV_TIMEOUT, SOCK_MAX_RECV_TIMEOUT)
        if timeout is None:
            self._send_error_json(
                400, "Invalid timeout param (seconds within (0, %d])."
                % SOCK_MAX_RECV_TIMEOUT)
            return
        max_raw = query.get("max_bytes", [])
        if max_raw:
            try:
                max_bytes = int(max_raw[0])
            except (TypeError, ValueError):
                max_bytes = -1
            if max_bytes < 1 or max_bytes > SOCK_MAX_BYTES:
                self._send_error_json(
                    400, "Invalid max_bytes param (1-%d)." % SOCK_MAX_BYTES)
                return
        else:
            max_bytes = SOCK_DEFAULT_MAX_BYTES
        found = self._socket_session(query)
        if found[0] is None:
            return
        sid, sess = found
        with _sessions_lock:
            attached = sess.get("ws_attached", False)
        if attached:
            self._send_error_json(
                400, "Socket session is attached to a websocket "
                     "(HTTP recv refused while attached).")
            return
        if not sess["recv_lock"].acquire(blocking=False):
            self._send_error_json(
                400, "Another recv is already pending on this session.")
            return
        try:
            self._socket_recv_locked(sid, sess, timeout, max_bytes)
        finally:
            sess["recv_lock"].release()

    def _socket_recv_locked(self, sid, sess, timeout, max_bytes):
        if sess.get("eof"):
            self._send_json({
                "id": sid, "encoding": "base64", "content": "",
                "bytes": jenc(0), "eof": jenc(True),
                "timed_out": jenc(False),
            })
            return
        sock = sess["sock"]
        deadline = time.time() + timeout
        data = b""
        eof = False
        failed = None
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([sock], [], [], remaining)
            except (OSError, ValueError) as exc:
                failed = str(exc)
                break
            if not ready:
                break
            try:
                chunk = sock.recv(min(max_bytes, SOCK_MAX_BYTES))
            except OSError as exc:
                failed = str(exc)
                break
            if chunk:
                data = chunk
                break
            eof = True
            break
        if failed is not None:
            self._send_error_json(400, "Socket error: %s" % failed)
            return
        with _sessions_lock:
            if sid in Sessions:
                Sessions[sid]["last"] = time.time()
                if eof:
                    Sessions[sid]["eof"] = True
        if eof:
            self._send_json({
                "id": sid, "encoding": "base64", "content": "",
                "bytes": jenc(0), "eof": jenc(True),
                "timed_out": jenc(False),
            })
            return
        if data:
            self._send_json({
                "id": sid, "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
                "bytes": jenc(len(data)), "eof": jenc(False),
                "timed_out": jenc(False),
            })
            return
        self._send_json({
            "id": sid, "encoding": "base64", "content": "",
            "bytes": jenc(0), "eof": jenc(False),
            "timed_out": jenc(True),
        })

    def _handle_socket_close(self, query):
        found = self._socket_session(query)
        if found[0] is None:
            return
        sid, sess = found
        with _sessions_lock:
            Sessions.pop(sid, None)
        try:
            try:
                sess["sock"].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sess["sock"].close()
        except OSError:
            pass
        self._send_json({"id": sid, "closed": jenc(True)})

    def _handle_socket_starttls(self, query):
        """Upgrade a plaintext session to TLS in place (STARTTLS-style).

        A failed handshake leaves the plaintext session untouched.
        """
        found = self._socket_session(query)
        if found[0] is None:
            return
        sid, sess = found
        with _sessions_lock:
            attached = sess.get("ws_attached", False)
            already_tls = sess.get("kind") == "tls"
            host, port = sess["host"], sess["port"]
        if attached:
            self._send_error_json(
                400, "Socket session is attached to a websocket "
                     "(STARTTLS refused while attached).")
            return
        if already_tls:
            self._send_error_json(
                400, "Socket session is already TLS.")
            return
        if not sess["recv_lock"].acquire(blocking=False):
            self._send_error_json(
                400, "Another recv is already pending on this session.")
            return
        try:
            return self._socket_starttls_locked(sid, sess, host, port)
        finally:
            sess["recv_lock"].release()

    def _socket_starttls_locked(self, sid, sess, host, port):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        obj = {}
        if raw.strip():
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                self._send_error_json(
                    400, "Content-Type must be application/json with body "
                         "{ sni?, ca_pem? }.")
                return
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send_error_json(400, "Invalid JSON body.")
                return
            if not isinstance(obj, dict):
                self._send_error_json(400, "JSON body must be an object.")
                return
        (identity, identity_err) = _parse_tls_identity(obj)
        if identity_err:
            self._send_error_json(400, identity_err)
            return
        sni, ca_pem = identity
        if not sni:
            sni = host
        ctx, ctx_err = _tls_context(ca_pem)
        if ctx_err:
            self._send_error_json(400, ctx_err)
            return
        old_sock = sess["sock"]
        old_sock.settimeout(SOCK_MAX_CONNECT_TIMEOUT)
        try:
            new_sock = ctx.wrap_socket(old_sock, server_hostname=sni)
        except (ssl.SSLError, OSError, ValueError) as exc:
            try:
                old_sock.settimeout(None)
            except OSError:
                pass
            self._send_error_json(
                400, "TLS handshake with %s:%d failed: %s"
                % (host, port, exc))
            return
        try:
            new_sock.settimeout(None)
        except OSError:
            pass
        with _sessions_lock:
            if Sessions.get(sid) is sess:
                sess["sock"] = new_sock
                sess["kind"] = "tls"
                sess["last"] = time.time()
        self._send_json({"id": sid, "kind": "tls"})
