"""Tests for DELETE /api/file and DELETE /api/dir (rmdir)."""

import os
import unittest

from tests.helpers import get_json, json_request, quote, tv, workspace


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.ws = workspace("delete")

    # -- files ------------------------------------------------------------

    def test_delete_file(self):
        target = os.path.join(self.ws, "gone.txt")
        with open(target, "w") as fh:
            fh.write("bye")
        status, _, data = json_request(
            "DELETE", "/api/file?path=" + quote(target))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["deleted"]), True)
        self.assertFalse(os.path.exists(target))
        status, _, _ = get_json("/api/file?path=" + quote(target))
        self.assertEqual(status, 404)

    def test_delete_file_not_found_404(self):
        status, _, _ = json_request(
            "DELETE", "/api/file?path=" + quote(
                os.path.join(self.ws, "ghost.txt")))
        self.assertEqual(status, 404)

    def test_delete_file_missing_path_400(self):
        status, _, _ = json_request("DELETE", "/api/file")
        self.assertEqual(status, 400)

    def test_delete_directory_via_file_endpoint_400(self):
        os.makedirs(os.path.join(self.ws, "adir"), exist_ok=True)
        status, _, _ = json_request(
            "DELETE", "/api/file?path=" + quote(
                os.path.join(self.ws, "adir")))
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "adir")))

    # -- directories ------------------------------------------------------

    def test_delete_empty_dir(self):
        target = os.path.join(self.ws, "empty")
        os.makedirs(target, exist_ok=True)
        status, _, data = json_request(
            "DELETE", "/api/dir?path=" + quote(target))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["deleted"]), True)
        self.assertFalse(os.path.exists(target))

    def test_delete_nonempty_dir_400(self):
        target = os.path.join(self.ws, "full")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "f.txt"), "w") as fh:
            fh.write("x")
        status, _, _ = json_request(
            "DELETE", "/api/dir?path=" + quote(target))
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isdir(target))

    def test_delete_dir_not_found_404(self):
        status, _, _ = json_request(
            "DELETE", "/api/dir?path=" + quote(
                os.path.join(self.ws, "ghost")))
        self.assertEqual(status, 404)

    def test_delete_file_via_dir_endpoint_400(self):
        target = os.path.join(self.ws, "f.txt")
        with open(target, "w") as fh:
            fh.write("x")
        status, _, _ = json_request(
            "DELETE", "/api/dir?path=" + quote(target))
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isfile(target))

    def test_delete_dir_missing_path_400(self):
        status, _, _ = json_request("DELETE", "/api/dir")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
