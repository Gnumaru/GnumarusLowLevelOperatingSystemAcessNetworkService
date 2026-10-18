"""Tests for GET /api/dir (read_directory)."""

import os
import subprocess
import sys
import time
import unittest

from tests.helpers import get_json, quote, tv, workspace


class TestReadDir(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = workspace("read_dir")
        with open(os.path.join(cls.ws, "a.txt"), "w") as fh:
            fh.write("alpha")
        with open(os.path.join(cls.ws, "b.bin"), "wb") as fh:
            fh.write(bytes([0, 255, 1]))
        os.makedirs(os.path.join(cls.ws, "sub"), exist_ok=True)

    def test_list_ok(self):
        status, _, data = get_json("/api/dir?path=" + quote(self.ws))
        self.assertEqual(status, 200)
        self.assertEqual(data["absolute_path"], self.ws)
        self.assertEqual(tv(data["count"]), 3)
        by_name = {e["name"]: e for e in data["entries"]}
        self.assertEqual(set(by_name), {"a.txt", "b.bin", "sub"})
        self.assertEqual(by_name["sub"]["type"], "dir")
        self.assertEqual(by_name["a.txt"]["type"], "file")
        self.assertEqual(by_name["a.txt"]["size"], "i:5")
        for entry in data["entries"]:
            for key in ("name", "path", "type", "size", "atime", "mtime",
                        "ctime", "crtime", "mode"):
                self.assertIn(key, entry)

    def test_missing_path_400(self):
        status, _, _ = get_json("/api/dir")
        self.assertEqual(status, 400)

    def test_not_found_404(self):
        status, _, _ = get_json(
            "/api/dir?path=" + quote(os.path.join(self.ws, "nope")))
        self.assertEqual(status, 404)

    def test_file_as_dir_400(self):
        status, _, _ = get_json(
            "/api/dir?path=" + quote(os.path.join(self.ws, "a.txt")))
        self.assertEqual(status, 400)

    def test_tilde_expands_to_home(self):
        status, _, data = get_json("/api/dir?path=" + quote("~"))
        self.assertEqual(status, 200)
        self.assertEqual(data["absolute_path"], os.path.expanduser("~"))
        self.assertGreaterEqual(tv(data["count"]), 1)

    def test_all_times_present(self):
        target = os.path.join(self.ws, "a.txt")
        past = time.time() - 10
        os.utime(target, (past, past))
        status, _, data = get_json("/api/dir?path=" + quote(self.ws))
        self.assertEqual(status, 200)
        by_name = {e["name"]: e for e in data["entries"]}
        entry = by_name["a.txt"]
        # Times are tagged numeric epoch seconds (float on CPython/JVM,
        # int on PHP where lstat has no sub-second precision).
        for key in ("atime", "mtime", "ctime"):
            self.assertIsInstance(tv(entry[key]), (int, float))
        self.assertAlmostEqual(tv(entry["atime"]), past, delta=2)
        self.assertAlmostEqual(tv(entry["mtime"]), past, delta=2)
        # crtime is "null" where neither the OS nor the filesystem
        # provides birth time, tagged float otherwise.
        self.assertTrue(entry["crtime"] == "null"
                        or isinstance(tv(entry["crtime"]), float))

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux-only statx birth time check")
    @unittest.skipIf("php" in os.environ.get("GNATIVE_TEST_SERVER_CMD", ""),
                     "PHP has no birth-time API (FFI blocked) and always "
                     "reports crtime null on Linux")
    def test_crtime_matches_stat(self):
        target = os.path.join(self.ws, "a.txt")
        status, _, data = get_json("/api/dir?path=" + quote(self.ws))
        self.assertEqual(status, 200)
        by_name = {e["name"]: e for e in data["entries"]}
        proc = subprocess.run(["stat", "-c", "%W", target],
                              capture_output=True, text=True, timeout=10)
        birth = int(proc.stdout.strip())
        if birth > 0:
            self.assertIsInstance(tv(by_name["a.txt"]["crtime"]), float)
            self.assertAlmostEqual(tv(by_name["a.txt"]["crtime"]), birth,
                                   delta=1)
        else:
            self.assertEqual(by_name["a.txt"]["crtime"], "null")


if __name__ == "__main__":
    unittest.main()
