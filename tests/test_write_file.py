"""Tests for POST /api/file (create) and PUT /api/file (overwrite)."""

import base64
import json
import os
import unittest

from tests.helpers import (assert_json_scalars, get_json, json_request,
                            quote, request, tv, workspace)

BINARY = bytes([0, 7, 255, 16, 32, 99])


class TestWriteFile(unittest.TestCase):
    def setUp(self):
        self.ws = workspace("write_file")

    def _path(self, name):
        return os.path.join(self.ws, name)

    def _target(self, name):
        return "/api/file?path=" + quote(self._path(name))

    # -- create -----------------------------------------------------------

    def test_create_empty(self):
        status, _, data = json_request("POST", self._target("empty.txt"))
        self.assertEqual(status, 201)
        self.assertEqual(tv(data["size"]), 0)
        with open(self._path("empty.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"")
        status, _, data = get_json(self._target("empty.txt"))
        self.assertEqual(data["content"], "")

    def test_create_text_json(self):
        status, _, data = json_request(
            "POST", self._target("note.txt"),
            {"content": "hello utf8: café", "encoding": "text"})
        self.assertEqual(status, 201)
        with open(self._path("note.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello utf8: café")

    def test_create_binary_base64(self):
        payload = base64.b64encode(BINARY).decode("ascii")
        status, _, data = json_request(
            "POST", self._target("blob.bin"),
            {"content": payload, "encoding": "base64"})
        self.assertEqual(status, 201)
        self.assertEqual(tv(data["size"]), len(BINARY))
        with open(self._path("blob.bin"), "rb") as fh:
            self.assertEqual(fh.read(), BINARY)
        # read back through the API (auto mode -> base64)
        status, _, data = get_json(self._target("blob.bin"))
        self.assertEqual(tv(data["is_binary"]), True)
        self.assertEqual(base64.b64decode(data["content"]), BINARY)

    def test_create_raw_bytes(self):
        status, _, data = request(
            "POST", self._target("raw.bin"), body=BINARY,
            headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 201)
        with open(self._path("raw.bin"), "rb") as fh:
            self.assertEqual(fh.read(), BINARY)

    def test_create_existing_conflict(self):
        with open(self._path("taken.txt"), "w") as fh:
            fh.write("original")
        status, _, _ = json_request(
            "POST", self._target("taken.txt"), {"content": "new"})
        self.assertEqual(status, 409)
        with open(self._path("taken.txt")) as fh:
            self.assertEqual(fh.read(), "original")

    def test_create_missing_parent_404(self):
        status, _, _ = json_request(
            "POST", "/api/file?path=" + quote(
                os.path.join(self.ws, "nodir", "f.txt")),
            {"content": "x"})
        self.assertEqual(status, 404)

    def test_create_missing_path_400(self):
        status, _, _ = json_request("POST", "/api/file", {"content": "x"})
        self.assertEqual(status, 400)

    def test_create_bad_json_400(self):
        status, _, _ = request(
            "POST", self._target("bad.txt"), body=b"{not json",
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_create_invalid_base64_400(self):
        status, _, _ = json_request(
            "POST", self._target("bad.txt"),
            {"content": "!!!not-base64!!!", "encoding": "base64"})
        self.assertEqual(status, 400)

    def test_create_invalid_encoding_400(self):
        status, _, _ = json_request(
            "POST", self._target("bad.txt"),
            {"content": "x", "encoding": "rot13"})
        self.assertEqual(status, 400)

    # -- overwrite --------------------------------------------------------

    def test_overwrite_text(self):
        with open(self._path("doc.txt"), "w") as fh:
            fh.write("old")
        status, _, data = json_request(
            "PUT", self._target("doc.txt"),
            {"content": "new content", "encoding": "text"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["size"]), len("new content"))
        with open(self._path("doc.txt")) as fh:
            self.assertEqual(fh.read(), "new content")

    def test_overwrite_binary_base64(self):
        with open(self._path("blob.bin"), "w") as fh:
            fh.write("old")
        payload = base64.b64encode(BINARY).decode("ascii")
        status, _, _ = json_request(
            "PUT", self._target("blob.bin"),
            {"content": payload, "encoding": "base64"})
        self.assertEqual(status, 200)
        with open(self._path("blob.bin"), "rb") as fh:
            self.assertEqual(fh.read(), BINARY)

    def test_overwrite_raw(self):
        with open(self._path("raw.bin"), "w") as fh:
            fh.write("old")
        status, _, body = request(
            "PUT", self._target("raw.bin"), body=BINARY,
            headers={"Content-Type": "application/octet-stream"})
        data = json.loads(body.decode("utf-8"))
        assert_json_scalars(data)
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["size"]), len(BINARY))
        with open(self._path("raw.bin"), "rb") as fh:
            self.assertEqual(fh.read(), BINARY)

    def test_overwrite_truncate_empty(self):
        with open(self._path("doc.txt"), "w") as fh:
            fh.write("something")
        status, _, data = json_request("PUT", self._target("doc.txt"))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["size"]), 0)
        with open(self._path("doc.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"")

    def test_overwrite_missing_404(self):
        status, _, _ = json_request(
            "PUT", self._target("ghost.txt"), {"content": "x"})
        self.assertEqual(status, 404)

    def test_overwrite_directory_400(self):
        os.makedirs(self._path("adir"), exist_ok=True)
        status, _, _ = json_request(
            "PUT", self._target("adir"), {"content": "x"})
        self.assertEqual(status, 400)

    def test_overwrite_missing_path_400(self):
        status, _, _ = json_request("PUT", "/api/file", {"content": "x"})
        self.assertEqual(status, 400)


class TestTildePaths(unittest.TestCase):
    """~/ paths expand to the server user's home on write endpoints too."""

    MARKER = os.path.join(os.path.expanduser("~"), ".gnumarus_tilde_test")

    def setUp(self):
        import shutil
        shutil.rmtree(self.MARKER, ignore_errors=True)
        os.makedirs(self.MARKER, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.MARKER, ignore_errors=True)

    def test_create_read_delete_under_home(self):
        rel = "~/.gnumarus_tilde_test/note.txt"
        status, _, data = json_request(
            "POST", "/api/file?path=" + quote(rel),
            {"content": "home sweet home", "encoding": "text"})
        self.assertEqual(status, 201)
        self.assertEqual(data["absolute_path"],
                         os.path.join(self.MARKER, "note.txt"))
        status, _, data = get_json("/api/file?path=" + quote(rel))
        self.assertEqual(status, 200)
        self.assertEqual(data["content"], "home sweet home")
        status, _, data = json_request(
            "DELETE", "/api/file?path=" + quote(rel))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["deleted"]), True)
        self.assertFalse(os.path.exists(os.path.join(self.MARKER,
                                                      "note.txt")))


if __name__ == "__main__":
    unittest.main()
