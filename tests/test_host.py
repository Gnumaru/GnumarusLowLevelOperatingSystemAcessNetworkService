"""Tests for loopback-only bind + Host allowlist.

Every request (any method, any route, OPTIONS, WS upgrades) is gated
on the HTTP Host header: only loopback literals (127.0.0.0/8,
localhost, ::1) are served, anything else gets 403 JSON. Likewise
--host only accepts loopback addresses (anything else fails fast
with exit code 2 before listening).
"""

import http.client
import os
import socket
import subprocess
import time
import unittest

from tests.helpers import base_url, request, workspace, quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def raw_request(target, headers, method="GET"):
    """Minimal raw HTTP/1.1 exchange. Returns (status, headers, body)."""
    from urllib.parse import urlparse
    parsed = urlparse(base_url())
    sock = socket.create_connection(
        (parsed.hostname, parsed.port or 80), timeout=10)
    try:
        lines = ["%s %s HTTP/1.1" % (method, target)]
        lines += ["%s: %s" % kv for kv in headers]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n")[0]
        status = int(first.split(b" ")[1])
        return status, head, rest
    finally:
        sock.close()


def get_host(host_value):
    """GET / with an explicit Host header. Returns (status, headers)."""
    status, headers, _ = request(
        "GET", "/", headers={"Host": host_value})
    return status, headers


class TestHostAllow(unittest.TestCase):
    def test_loopback_hosts_ok(self):
        from urllib.parse import urlparse
        port = urlparse(base_url()).port
        for host in ("127.0.0.1", "127.0.0.1:%d" % port,
                     "localhost", "LOCALHOST", "localhost:%d" % port,
                     "127.0.0.2", "[::1]", "[::1]:%d" % port,
                     "127.0.0.1:9999"):
            status, _ = get_host(host)
            self.assertEqual(status, 200, host)

    def test_host_denied(self):
        for host in ("evil.com", "evil.com:1423", "localhost.",
                     "localhost:abc", "[::1", "[]", "192.168.1.1",
                     "0.0.0.0", "example.com.", "127.1", "0177.0.0.1",
                     "127.0.0.1:abc", "foo:bar", ":"):
            status, headers, data = request(
                "GET", "/", headers={"Host": host})
            self.assertEqual(status, 403, host)
            self.assertEqual(headers.get("access-control-allow-origin"),
                             "*")
            import json
            parsed = json.loads(data.decode("utf-8"))
            self.assertEqual(parsed["error"], "forbidden")

    def test_missing_host_rejected(self):
        # Go's own HTTP stack answers 400 here; the rest answer 403.
        # Either way it must never be served.
        status, _, _ = raw_request("/", [])
        self.assertIn(status, (400, 403), status)

    def test_empty_host_rejected(self):
        status, _, _ = raw_request("/", [("Host", "")])
        self.assertIn(status, (400, 403), status)


class TestHostEnforced(unittest.TestCase):
    def test_denied_before_auth(self):
        # No token and bad token both lose to a bad Host (403, not 401).
        status, _, _ = request(
            "GET", "/api/dir?path=.", headers={"Host": "evil.com"},
            auth=False)
        self.assertEqual(status, 403)
        status, _, _ = request(
            "GET", "/api/dir?path=.",
            headers={"Host": "evil.com",
                     "Authorization": "Bearer wrong"}, auth=False)
        self.assertEqual(status, 403)

    def test_denied_post_has_no_side_effect(self):
        work = workspace("host")
        target = "/api/file?path=" + quote(
            os.path.join(work, "evil.txt"))
        status, _, _ = request(
            "POST", target, body=b"evil",
            headers={"Host": "evil.com",
                     "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 403)
        status, _, _ = request("GET", target)
        self.assertEqual(status, 404)

    def test_denied_options(self):
        status, _, _ = request(
            "OPTIONS", "/api/file",
            headers={"Host": "evil.com", "Origin": "http://x"})
        self.assertEqual(status, 403)

    def test_denied_ws_upgrade(self):
        try:
            from tests.test_socket_ws import ws_capable
        except ImportError:
            self.skipTest("no ws helper module")
            return
        if not ws_capable():
            self.skipTest("no socket_ws route on this port")
        status, _, _ = raw_request(
            "/api/socket/ws?id=x&token=" +
            os.environ["GNATIVE_TEST_TOKEN"],
            [("Host", "evil.com"),
             ("Upgrade", "websocket"),
             ("Connection", "Upgrade"),
             ("Sec-WebSocket-Key", "eHl6"),
             ("Sec-WebSocket-Version", "13")])
        self.assertEqual(status, 403, status)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server_cmd():
    raw = os.environ.get("GNATIVE_TEST_SERVER_CMD")
    if raw:
        import shlex
        return shlex.split(raw)
    return [os.path.join(ROOT, "go", "gnumarus")]


def spawn_host(host):
    """Start a sub-server binding host; return (proc, output, port).

    Returns while the server is still running (or just after a fast
    config-error exit); the caller owns teardown.
    """
    import select
    port = free_port()
    cmd = server_cmd() + ["--host", host, "--port", str(port)]
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("GNUMARUS_")}
    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=child_env)
    out = b""
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            rest = proc.stdout.read()
            if rest:
                out += rest
            break
        ready, _, _ = select.select(
            [proc.stdout], [], [], max(0.0, deadline - time.time()))
        if not ready:
            break
        chunk = proc.stdout.readline()
        if not chunk:
            break
        out += chunk
        if b"config sources:" in chunk:
            break
    # If output hit EOF, the process is exiting: wait for the status
    # so callers never race poll() against a dying child.
    if proc.poll() is None and out and b"config sources:" not in out:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    return proc, out.decode("utf-8", errors="replace"), port


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


class TestBindLoopback(unittest.TestCase):
    def test_bind_rejects_wildcard(self):
        proc, out, _ = spawn_host("0.0.0.0")
        try:
            self.assertEqual(proc.poll(), 2, out)
        finally:
            stop(proc)
        self.assertIn("loopback", out)

    def test_bind_rejects_lan_ip(self):
        proc, out, _ = spawn_host("192.0.2.1")
        try:
            self.assertEqual(proc.poll(), 2, out)
        finally:
            stop(proc)
        self.assertIn("loopback", out)

    def test_bind_rejects_hostname(self):
        proc, out, _ = spawn_host("example.com")
        try:
            self.assertEqual(proc.poll(), 2, out)
        finally:
            stop(proc)
        self.assertIn("loopback", out)

    def test_bind_allows_loopback_variants(self):
        for host in ("127.0.0.2", "localhost"):
            proc, out, port = spawn_host(host)
            try:
                self.assertIsNone(proc.poll(), out)
                deadline = time.time() + 15
                ok = False
                while time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        # Connect to the bound name itself (resolves
                        # like any other client would).
                        conn = http.client.HTTPConnection(
                            host, port, timeout=2)
                        conn.request("GET", "/")
                        if conn.getresponse().status == 200:
                            ok = True
                            break
                    except OSError:
                        time.sleep(0.1)
                self.assertTrue(ok, out)
            finally:
                stop(proc)


if __name__ == "__main__":
    unittest.main()
