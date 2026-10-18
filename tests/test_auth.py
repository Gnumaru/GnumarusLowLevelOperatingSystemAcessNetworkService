"""Tests for the API token (auth required everywhere except help/index)."""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.parse import urlparse

from tests.helpers import (ROOT, assert_json_scalars, base_url, get_json,
                            json_request, request, tv)


def raw_request(method, target, body=None, headers=None):
    """Same as helpers.request but never attaches a token."""
    return request(method, target, body=body, headers=headers, auth=False)


class TestAuth(unittest.TestCase):
    def test_help_open_without_token(self):
        # raw: no token attached, proving /api/help is open
        parsed = urlparse(base_url())
        conn = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=10)
        conn.request("GET", "/api/help")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 200)
        assert_json_scalars(data)
        self.assertIn("auth", data)
        self.assertEqual(tv(data["auth"]["required"]), True)
        self.assertIn("/api/help", data["auth"]["open_routes"])

    def test_index_open_without_token(self):
        status, _, _ = raw_request("GET", "/")
        self.assertEqual(status, 200)

    def test_options_open_without_token(self):
        status, _, _ = raw_request("OPTIONS", "/api/shell")
        self.assertEqual(status, 204)

    def test_dir_without_token_401(self):
        status, headers, data = raw_request("GET", "/api/dir?path=/")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(data.decode("utf-8"))["error"],
                         "unauthorized")
        self.assertEqual(headers.get("access-control-allow-origin"), "*")

    def test_write_without_token_401(self):
        status, _, _ = raw_request(
            "POST", "/api/shell", body=b"{}",
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 401)

    def test_wrong_token_401(self):
        status, _, _ = request(
            "GET", "/api/dir?path=/",
            headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)

    def test_bearer_token_ok(self):
        status, _, _ = get_json("/api/dir?path=/")
        self.assertEqual(status, 200)

    def test_query_token_ok(self):
        status, _, data = raw_request("GET", "/api/dir?path=/&token=" +
                                      os.environ["GNATIVE_TEST_TOKEN"])
        self.assertEqual(status, 200)


class TestNoAuthFlag(unittest.TestCase):
    """A server started with --no-auth must not ask for tokens."""

    @classmethod
    def setUpClass(cls):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        log = os.path.join(ROOT, "tmp", "noauth_server.log")
        raw = os.environ.get("GNATIVE_TEST_SERVER_CMD")
        if raw:
            import shlex
            launch = shlex.split(raw)
        else:
            launch = [sys.executable, os.path.join(ROOT, "python3", "server.py")]
        cls.proc = subprocess.Popen(
            launch + ["--host", "127.0.0.1", "--port", str(cls.port),
                      "--no-auth"],
            cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=open(log, "w"), stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("--no-auth server exited early")
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", cls.port, timeout=2)
                conn.request("GET", "/")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("--no-auth server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def raw(self, method, target, **kwargs):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        conn.request(method, target, **kwargs)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_no_token_needed(self):
        status, _ = self.raw("GET", "/api/dir?path=/")
        self.assertEqual(status, 200)

    def test_help_reports_auth_off(self):
        status, body = self.raw("GET", "/api/help")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        assert_json_scalars(data)
        self.assertEqual(tv(data["auth"]["required"]), False)


if __name__ == "__main__":
    unittest.main()
