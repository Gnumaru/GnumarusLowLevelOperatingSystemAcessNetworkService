"""Tests for the transparent TCP socket proxy (/api/socket/*)."""

import base64
import socket
import threading
import unittest

from tests.helpers import json_request, quote, request, ti, tv


class EchoServer:
    """Threaded TCP echo server (echoes everything until EOF)."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,),
                             daemon=True).start()

    @staticmethod
    def _echo(conn):
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def goodbye_server():
    """Server that reads once, replies b'bye' and closes (for EOF tests)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
            try:
                conn.recv(4096)
                conn.sendall(b"bye")
            finally:
                conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def free_port():
    """A currently-closed localhost TCP port (for refused tests)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def sock_open(host, port, timeout=None):
    body = {"host": host, "port": port}
    if timeout is not None:
        body["timeout"] = timeout
    return json_request("POST", "/api/socket/open", body)


def sock_send(sid, data=None, encoding="text", raw=False, content_type=None):
    import json as _json
    from tests.helpers import assert_json_scalars
    target = "/api/socket/send?id=" + quote(sid)
    if raw:
        status, _, blob = request(
            "POST", target, body=data,
            headers={"Content-Type":
                     content_type or "application/octet-stream"})
        parsed = _json.loads(blob.decode("utf-8"))
        assert_json_scalars(parsed)
        return status, _, parsed
    body = {}
    if data is not None:
        body["content"] = data
    if encoding is not None:
        body["encoding"] = encoding
    return json_request("POST", target, body)


def sock_recv(sid, timeout="2", max_bytes=None):
    target = "/api/socket/recv?id=" + quote(sid) + "&timeout=" + timeout
    if max_bytes is not None:
        target += "&max_bytes=" + str(max_bytes)
    return json_request("GET", target)


def sock_close(sid):
    return json_request(
        "POST", "/api/socket/close?id=" + quote(sid), {})


_SOCKET_OK = None


def socket_available():
    """False when the server answers 503 (no session backend)."""
    global _SOCKET_OK
    if _SOCKET_OK is None:
        try:
            status, _, _ = sock_open("127.0.0.1", ti(free_port()))
        except OSError:
            status = 0
        _SOCKET_OK = status != 503
    return _SOCKET_OK


def require_socket(test):
    import unittest as _ut
    if not socket_available():
        raise _ut.SkipTest("socket proxy unavailable (503)")
    return test


def recv_all(sid, total, timeout="5"):
    """recv until exactly `total` bytes arrive; return the raw bytes."""
    out = b""
    empty = 0
    while len(out) < total:
        status, _, data = sock_recv(sid, timeout=timeout)
        assert status == 200, data
        assert tv(data["eof"]) is False, data
        chunk = base64.b64decode(data["content"])
        if not chunk:
            empty += 1
            assert empty <= 3, ("recv stalled: %d empty timeouts" % empty,
                                data)
        else:
            empty = 0
        out += chunk
    return out


class TestSocketProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_socket(cls)
        cls.echo = EchoServer()
        cls.echo.start()

    @classmethod
    def tearDownClass(cls):
        cls.echo.stop()

    def test_open_send_recv_close_text(self):
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        self.assertEqual(data["kind"], "tcp")
        self.assertEqual(data["host"], "127.0.0.1")
        self.assertEqual(tv(data["port"]), self.echo.port)
        try:
            status, _, sent = sock_send(sid, "hello-proxy")
            self.assertEqual(status, 200, sent)
            self.assertEqual(tv(sent["bytes"]), 11)
            status, _, got = sock_recv(sid)
            self.assertEqual(status, 200, got)
            self.assertEqual(got["encoding"], "base64")
            self.assertEqual(tv(got["bytes"]), 11)
            self.assertEqual(tv(got["eof"]), False)
            self.assertEqual(tv(got["timed_out"]), False)
            self.assertEqual(base64.b64decode(got["content"]),
                             b"hello-proxy")
        finally:
            status, _, closed = sock_close(sid)
            self.assertEqual(status, 200, closed)
            self.assertEqual(tv(closed["closed"]), True)

    def test_binary_base64(self):
        payload = bytes([0, 1, 2, 255, 254, 0, 65, 66, 67])
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            blob = base64.b64encode(payload).decode("ascii")
            status, _, sent = sock_send(sid, blob, encoding="base64")
            self.assertEqual(status, 200, sent)
            self.assertEqual(tv(sent["bytes"]), len(payload))
            got = recv_all(sid, len(payload))
            self.assertEqual(got, payload)
        finally:
            sock_close(sid)

    def test_raw_bytes_send(self):
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            status, _, sent = sock_send(
                sid, b"\x00\xffraw", raw=True)
            self.assertEqual(status, 200, sent)
            self.assertEqual(tv(sent["bytes"]), 5)
            got = recv_all(sid, 5)
            self.assertEqual(got, b"\x00\xffraw")
        finally:
            sock_close(sid)

    def test_large_256kb(self):
        payload = bytes((i % 251 for i in range(256 * 1024)))
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            blob = base64.b64encode(payload).decode("ascii")
            status, _, sent = sock_send(sid, blob, encoding="base64")
            self.assertEqual(status, 200, sent)
            self.assertEqual(tv(sent["bytes"]), len(payload))
            got = recv_all(sid, len(payload))
            self.assertEqual(got, payload)
        finally:
            sock_close(sid)

    def test_send_empty(self):
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            status, _, sent = sock_send(sid, "")
            self.assertEqual(status, 200, sent)
            self.assertEqual(tv(sent["bytes"]), 0)
        finally:
            sock_close(sid)

    def test_recv_timeout(self):
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            status, _, got = sock_recv(sid, timeout="1")
            self.assertEqual(status, 200, got)
            self.assertEqual(tv(got["timed_out"]), True)
            self.assertEqual(tv(got["bytes"]), 0)
            self.assertEqual(got["content"], "")
            self.assertEqual(tv(got["eof"]), False)
        finally:
            sock_close(sid)

    def test_eof(self):
        port = goodbye_server()
        status, _, data = sock_open("127.0.0.1", ti(port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            status, _, sent = sock_send(sid, "hi")
            self.assertEqual(status, 200, sent)
            got = recv_all(sid, 3)
            self.assertEqual(got, b"bye")
            status, _, done = sock_recv(sid, timeout="2")
            self.assertEqual(status, 200, done)
            self.assertEqual(tv(done["eof"]), True)
            self.assertEqual(tv(done["bytes"]), 0)
        finally:
            sock_close(sid)

    def test_two_sessions_interleaved(self):
        status, _, first = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, first)
        status, _, second = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, second)
        self.assertNotEqual(first["id"], second["id"])
        try:
            sock_send(first["id"], "AAA")
            sock_send(second["id"], "BBB")
            self.assertEqual(recv_all(second["id"], 3), b"BBB")
            self.assertEqual(recv_all(first["id"], 3), b"AAA")
        finally:
            sock_close(first["id"])
            sock_close(second["id"])

    def test_close_then_send_404(self):
        status, _, data = sock_open("127.0.0.1", ti(self.echo.port))
        self.assertEqual(status, 201, data)
        sock_close(data["id"])
        status, _, _ = sock_send(data["id"], "x")
        self.assertEqual(status, 404)


class TestSocketErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_socket(cls)

    def test_connect_refused_400(self):
        status, _, data = sock_open("127.0.0.1", ti(free_port()))
        self.assertEqual(status, 400, data)

    def test_missing_host_400(self):
        status, _, _ = json_request(
            "POST", "/api/socket/open", {"port": ti(80)})
        self.assertEqual(status, 400)

    def test_missing_port_400(self):
        status, _, _ = json_request(
            "POST", "/api/socket/open", {"host": "127.0.0.1"})
        self.assertEqual(status, 400)

    def test_bad_ports_400(self):
        for bad in (80, "80", "i:0", "i:99999", "i:-1", "b:true",
                    "null", "f:80.0"):
            status, _, _ = json_request(
                "POST", "/api/socket/open",
                {"host": "127.0.0.1", "port": bad})
            self.assertEqual(status, 400, bad)

    def test_bad_body_400(self):
        status, _, _ = request(
            "POST", "/api/socket/open", body=b"\x00\x01",
            headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 400)
        status, _, _ = json_request("POST", "/api/socket/open", [1, 2])
        self.assertEqual(status, 400)

    def test_bad_timeout_400(self):
        for bad in ("i:0", "i:31", "30", "b:true", "null"):
            status, _, _ = json_request(
                "POST", "/api/socket/open",
                {"host": "127.0.0.1", "port": ti(80), "timeout": bad})
            self.assertEqual(status, 400, bad)

    def test_unknown_id_404(self):
        status, _, _ = sock_send("nope", "x")
        self.assertEqual(status, 404)
        status, _, _ = sock_recv("nope", timeout="1")
        self.assertEqual(status, 404)
        status, _, _ = sock_close("nope")
        self.assertEqual(status, 404)

    def test_missing_id_400(self):
        status, _, _ = json_request("POST", "/api/socket/send", {})
        self.assertEqual(status, 400)
        status, _, _ = json_request("GET", "/api/socket/recv")
        self.assertEqual(status, 400)
        status, _, _ = json_request("POST", "/api/socket/close", {})
        self.assertEqual(status, 400)

    def test_bad_recv_params_400(self):
        for target in ("/api/socket/recv?id=x&timeout=abc",
                       "/api/socket/recv?id=x&timeout=0",
                       "/api/socket/recv?id=x&timeout=61",
                       "/api/socket/recv?id=x&max_bytes=0",
                       "/api/socket/recv?id=x&max_bytes=99999999",
                       "/api/socket/recv?id=x&max_bytes=abc"):
            status, _, _ = json_request("GET", target)
            self.assertEqual(status, 400, target)

    def test_bad_send_body_400(self):
        echo = EchoServer()
        echo.start()
        try:
            status, _, data = sock_open("127.0.0.1", ti(echo.port))
            self.assertEqual(status, 201, data)
            sid = data["id"]
            try:
                status, _, _ = json_request(
                    "POST", "/api/socket/send?id=" + quote(sid),
                    {"content": "!!!", "encoding": "base64"})
                self.assertEqual(status, 400)
                status, _, _ = json_request(
                    "POST", "/api/socket/send?id=" + quote(sid),
                    {"content": "x", "encoding": "rot13"})
                self.assertEqual(status, 400)
                status, _, _ = json_request(
                    "POST", "/api/socket/send?id=" + quote(sid),
                    {"content": 5})
                self.assertEqual(status, 400)
            finally:
                sock_close(sid)
        finally:
            echo.stop()

    def test_methods_405(self):
        for method, target in (
                ("GET", "/api/socket/open"),
                ("PUT", "/api/socket/open"),
                ("DELETE", "/api/socket/open"),
                ("GET", "/api/socket/send?id=x"),
                ("PUT", "/api/socket/send?id=x"),
                ("POST", "/api/socket/recv?id=x"),
                ("PUT", "/api/socket/recv?id=x"),
                ("DELETE", "/api/socket/recv?id=x"),
                ("GET", "/api/socket/close"),
                ("PUT", "/api/socket/close?id=x"),
                ("DELETE", "/api/socket/close?id=x")):
            status, _, _ = request(method, target)
            self.assertEqual(status, 405, (method, target))

    def test_auth_401(self):
        status, _, _ = json_request(
            "POST", "/api/socket/open",
            {"host": "127.0.0.1", "port": ti(80)}, auth=False)
        self.assertEqual(status, 401)
        status, _, _ = json_request(
            "POST", "/api/socket/send?id=x", {}, auth=False)
        self.assertEqual(status, 401)
        status, _, _ = json_request(
            "GET", "/api/socket/recv?id=x", auth=False)
        self.assertEqual(status, 401)
        status, _, _ = json_request(
            "POST", "/api/socket/close?id=x", {}, auth=False)
        self.assertEqual(status, 401)

    def test_help_lists_socket(self):
        status, _, data = json_request("GET", "/api/help", auth=False)
        self.assertEqual(status, 200)
        names = {r["name"] for r in data["routes"]}
        self.assertTrue(
            {"socket_open", "socket_send", "socket_recv",
             "socket_close"} <= names)
        self.assertEqual(tv(data["count"]), len(data["routes"]))


if __name__ == "__main__":
    unittest.main()
