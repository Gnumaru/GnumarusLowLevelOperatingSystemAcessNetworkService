"""Tests for TLS socket sessions.

POST /api/socket/open accepts tls (b:), sni and ca_pem; POST
/api/socket/starttls?id= upgrades a plaintext session in place.
Both verify against the system roots (+ optional ca_pem) with
hostname check and SNI; failures are 400 with the engine text.

The module skips when the server does not advertise the
socket_starttls route or when the openssl CLI is unavailable
(local PKI fixture).
"""

import base64
import os
import shutil
import socket
import ssl
import subprocess
import threading
import unittest

from tests.helpers import base_url, json_request, quote, request, ti, tv
from tests.test_socket import EchoServer, sock_close, sock_open, sock_recv


def tls_capable():
    """True when the server advertises the socket_starttls route."""
    status, _, data = json_request("GET", "/api/help", auth=False)
    assert status == 200
    return any(r["name"] == "socket_starttls" for r in data["routes"])


def make_pki(work):
    """Mint a test CA + server cert (SAN localhost/127.0.0.1)."""
    ca_key = os.path.join(work, "ca.key")
    ca_pem = os.path.join(work, "ca.pem")
    srv_key = os.path.join(work, "srv.key")
    srv_csr = os.path.join(work, "srv.csr")
    srv_pem = os.path.join(work, "srv.pem")
    san = os.path.join(work, "san.cnf")
    with open(san, "w") as fh:
        fh.write("[v3]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", ca_key, "-out", ca_pem, "-days", "2",
         "-nodes", "-subj", "/CN=test-ca"],
        check=True, capture_output=True, timeout=60)
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048",
         "-keyout", srv_key, "-out", srv_csr,
         "-nodes", "-subj", "/CN=localhost"],
        check=True, capture_output=True, timeout=60)
    subprocess.run(
        ["openssl", "x509", "-req", "-in", srv_csr,
         "-CA", ca_pem, "-CAkey", ca_key, "-CAcreateserial",
         "-out", srv_pem, "-days", "2",
         "-extensions", "v3", "-extfile", san],
        check=True, capture_output=True, timeout=60)
    with open(ca_pem) as fh:
        ca_text = fh.read()
    return {"ca": ca_text, "cert": srv_pem, "key": srv_key}


class TlsEchoServer:
    """Single-connection TLS echo server (implicit TLS)."""

    def __init__(self, certfile, keyfile):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        self.ctx = ctx
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _serve(self):
        raw, _ = self.sock.accept()
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except (ssl.SSLError, OSError):
                    return
                if not data:
                    return
                try:
                    conn.sendall(data)
                except (ssl.SSLError, OSError):
                    return


class StarttlsServer:
    """Plaintext prelude, then TLS on demand (STARTTLS-style)."""

    def __init__(self, certfile, keyfile):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        self.ctx = ctx
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _readline(self, conn):
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = conn.recv(1)
            if not chunk:
                raise ConnectionError("eof")
            buf += chunk
        return buf

    def _serve(self):
        raw, _ = self.sock.accept()
        with raw:
            raw.sendall(b"* OK starttls ready\r\n")
            try:
                line = self._readline(raw)
            except ConnectionError:
                return
            tag = line.decode("utf-8", "replace").split(" ", 1)[0]
            raw.sendall(tag.encode() + b" OK begin TLS now\r\n")
            try:
                conn = self.ctx.wrap_socket(raw, server_side=True)
            except (ssl.SSLError, OSError):
                return
            while True:
                try:
                    data = conn.recv(65536)
                except (ssl.SSLError, OSError):
                    return
                if not data:
                    return
                try:
                    conn.sendall(data)
                except (ssl.SSLError, OSError):
                    return


def tls_open(host, port, extra=None):
    body = {"host": host, "port": port}
    body.update(extra or {})
    return json_request("POST", "/api/socket/open", body)


def starttls(sid, body=None):
    """POST /api/socket/starttls?id=. Returns (status, parsed_json)."""
    import json as _json
    from tests.helpers import assert_json_scalars
    target = "/api/socket/starttls?id=" + quote(sid)
    if body is None:
        status, _, raw = request("POST", target)
        parsed = _json.loads(raw.decode("utf-8"))
        assert_json_scalars(parsed)
        return status, parsed
    status, _, parsed = json_request("POST", target, body)
    return status, parsed


def recv_bytes(sid, timeout="5"):
    status, _, data = sock_recv(sid, timeout=timeout)
    assert status == 200, data
    assert tv(data["eof"]) is False, data
    return base64.b64decode(data["content"] or "")


class TestSocketTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not tls_capable():
            raise unittest.SkipTest("no socket_starttls route on this port")
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("openssl CLI not available")
        from tests.helpers import workspace
        cls.pki = make_pki(workspace("tls_pki"))

    def test_help_advertises_tls(self):
        status, _, data = json_request("GET", "/api/help", auth=False)
        self.assertEqual(status, 200)
        names = {r["name"] for r in data["routes"]}
        self.assertIn("socket_starttls", names)
        by_name = {r["name"]: r for r in data["routes"]}
        params = {p["name"] for p in by_name["socket_open"]["params"]}
        for name in ("tls", "sni", "ca_pem"):
            self.assertIn(name, params)
        params = {p["name"] for p in by_name["socket_starttls"]["params"]}
        for name in ("id", "sni", "ca_pem"):
            self.assertIn(name, params)

    def test_direct_tls_echo(self):
        srv = TlsEchoServer(self.pki["cert"], self.pki["key"]).start()
        status, _, data = tls_open("127.0.0.1", ti(srv.port),
                                   {"tls": "b:true",
                                    "ca_pem": self.pki["ca"]})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["kind"], "tls")
        sid = data["id"]
        try:
            from tests.test_socket import sock_send
            status, _, _ = sock_send(sid, "hello-tls")
            self.assertEqual(status, 200)
            self.assertEqual(recv_bytes(sid), b"hello-tls")
        finally:
            sock_close(sid)

    def test_direct_tls_needs_ca(self):
        srv = TlsEchoServer(self.pki["cert"], self.pki["key"]).start()
        status, _, data = tls_open("127.0.0.1", ti(srv.port),
                                   {"tls": "b:true"})
        self.assertEqual(status, 400, data)
        self.assertIn("TLS handshake", data["message"])

    def test_direct_tls_sni_mismatch(self):
        srv = TlsEchoServer(self.pki["cert"], self.pki["key"]).start()
        status, _, data = tls_open("127.0.0.1", ti(srv.port),
                                   {"tls": "b:true",
                                    "ca_pem": self.pki["ca"],
                                    "sni": "wrong.invalid"})
        self.assertEqual(status, 400, data)
        self.assertIn("TLS handshake", data["message"])

    def test_direct_tls_against_plaintext(self):
        srv = EchoServer()
        srv.start()
        try:
            status, _, data = tls_open("127.0.0.1", ti(srv.port),
                                       {"tls": "b:true",
                                        "ca_pem": self.pki["ca"]})
            self.assertEqual(status, 400, data)
            self.assertIn("TLS handshake", data["message"])
        finally:
            srv.stop()

    def test_open_tls_validation(self):
        srv = TlsEchoServer(self.pki["cert"], self.pki["key"]).start()
        for extra, hint in (
                ({"tls": "yes"}, "b:true"),
                ({"tls": "i:1"}, "b:true"),
                ({"tls": "b:true", "ca_pem": "not a pem"}, "PEM"),
                ({"tls": "b:true", "ca_pem": self.pki["ca"],
                  "sni": ""}, "sni")):
            status, _, data = tls_open("127.0.0.1", ti(srv.port), extra)
            self.assertEqual(status, 400, (extra, data))
            self.assertIn(hint, data["message"], (extra, data))

    def test_starttls_happy_path(self):
        srv = StarttlsServer(self.pki["cert"], self.pki["key"]).start()
        status, _, data = sock_open("127.0.0.1", ti(srv.port))
        self.assertEqual(status, 201, data)
        self.assertEqual(data["kind"], "tcp")
        sid = data["id"]
        try:
            from tests.test_socket import sock_send
            self.assertEqual(recv_bytes(sid), b"* OK starttls ready\r\n")
            status, _, _ = sock_send(sid, "a001 STARTTLS\r\n")
            self.assertEqual(status, 200)
            self.assertEqual(recv_bytes(sid), b"a001 OK begin TLS now\r\n")
            status, data = starttls(sid, {"ca_pem": self.pki["ca"]})
            self.assertEqual(status, 200, data)
            self.assertEqual(data["kind"], "tls")
            status, _, _ = sock_send(sid, "secret-bytes")
            self.assertEqual(status, 200)
            self.assertEqual(recv_bytes(sid), b"secret-bytes")
        finally:
            sock_close(sid)

    def test_starttls_already_tls(self):
        srv = TlsEchoServer(self.pki["cert"], self.pki["key"]).start()
        status, _, data = tls_open("127.0.0.1", ti(srv.port),
                                   {"tls": "b:true",
                                    "ca_pem": self.pki["ca"]})
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            status, data = starttls(sid, {})
            self.assertEqual(status, 400, data)
            self.assertIn("already TLS", data["message"])
        finally:
            sock_close(sid)

    def test_starttls_against_plaintext_keeps_session(self):
        srv = EchoServer()
        srv.start()
        try:
            status, _, data = sock_open("127.0.0.1", ti(srv.port))
            self.assertEqual(status, 201, data)
            sid = data["id"]
            try:
                status, data = starttls(
                    sid, {"ca_pem": self.pki["ca"]})
                self.assertEqual(status, 400, data)
                self.assertIn("TLS handshake", data["message"])
                # The plaintext session survives the failed upgrade.
                status, _, _ = sock_close(sid)
                self.assertEqual(status, 200)
            except Exception:
                sock_close(sid)
                raise
        finally:
            srv.stop()

    def test_starttls_unknown_and_missing_id(self):
        status, _, data = json_request(
            "POST", "/api/socket/starttls?id=nope", {})
        self.assertEqual(status, 404, data)
        status, _, data = json_request("POST", "/api/socket/starttls", {})
        self.assertEqual(status, 400, data)

    def test_starttls_wrong_method(self):
        status, _, data = json_request(
            "GET", "/api/socket/starttls?id=x")
        self.assertEqual(status, 405, data)

    def test_starttls_refused_while_ws_attached(self):
        try:
            from tests.test_socket_ws import upgrade, ws_capable
        except ImportError:
            self.skipTest("no ws helper module")
            return
        if not ws_capable():
            self.skipTest("no socket_ws route on this port")
        srv = EchoServer()
        srv.start()
        try:
            status, _, data = sock_open("127.0.0.1", ti(srv.port))
            self.assertEqual(status, 201, data)
            sid = data["id"]
            try:
                ws = upgrade(sid)
                try:
                    status, data = starttls(sid, {})
                    self.assertEqual(status, 400, data)
                    self.assertIn("attached", data["message"])
                finally:
                    ws.close()
            finally:
                sock_close(sid)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
