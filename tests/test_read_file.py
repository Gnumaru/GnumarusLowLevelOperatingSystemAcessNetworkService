"""Tests for GET /api/file (read_file), text and binary."""

import base64
import os
import unittest

from tests.helpers import get_json, quote, request, tv, workspace

BINARY = bytes([0, 1, 2, 255, 254, 0, 65, 66, 67])
TEXT = "hello world\nsecond line\n"


class TestReadFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = workspace("read_file")
        with open(os.path.join(cls.ws, "hello.txt"), "w") as fh:
            fh.write(TEXT)
        with open(os.path.join(cls.ws, "blob.bin"), "wb") as fh:
            fh.write(BINARY)
        os.makedirs(os.path.join(cls.ws, "sub"), exist_ok=True)

    def test_text_auto(self):
        status, _, data = get_json(
            "/api/file?path=" + quote(os.path.join(self.ws, "hello.txt")))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["is_binary"]), False)
        self.assertEqual(data["encoding"], "utf-8")
        self.assertEqual(data["content"], TEXT)
        self.assertEqual(tv(data["size"]), len(TEXT.encode("utf-8")))

    def test_binary_auto_base64(self):
        status, _, data = get_json(
            "/api/file?path=" + quote(os.path.join(self.ws, "blob.bin")))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["is_binary"]), True)
        self.assertEqual(data["encoding"], "base64")
        self.assertEqual(base64.b64decode(data["content"]), BINARY)

    def test_forced_base64_on_text(self):
        status, _, data = get_json(
            "/api/file?path=" + quote(os.path.join(self.ws, "hello.txt"))
            + "&encoding=base64")
        self.assertEqual(status, 200)
        self.assertEqual(base64.b64decode(data["content"]),
                         TEXT.encode("utf-8"))

    def test_forced_text_on_binary(self):
        status, _, data = get_json(
            "/api/file?path=" + quote(os.path.join(self.ws, "blob.bin"))
            + "&encoding=text")
        self.assertEqual(status, 200)
        self.assertEqual(data["encoding"], "utf-8")
        self.assertIsInstance(data["content"], str)

    def test_raw_mode(self):
        target = ("/api/file?path="
                  + quote(os.path.join(self.ws, "blob.bin")) + "&raw=1")
        status, headers, body = request("GET", target)
        self.assertEqual(status, 200)
        self.assertEqual(body, BINARY)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")
        self.assertIn("content-type", headers)

    def test_missing_path_400(self):
        status, _, _ = get_json("/api/file")
        self.assertEqual(status, 400)

    def test_not_found_404(self):
        status, _, _ = get_json(
            "/api/file?path=" + quote(os.path.join(self.ws, "nope.txt")))
        self.assertEqual(status, 404)

    def test_directory_400(self):
        status, _, _ = get_json(
            "/api/file?path=" + quote(os.path.join(self.ws, "sub")))
        self.assertEqual(status, 400)

    def test_bad_encoding_400(self):
        status, _, _ = get_json(
            "/api/file?path=" + quote(os.path.join(self.ws, "hello.txt"))
            + "&encoding=rot13")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
