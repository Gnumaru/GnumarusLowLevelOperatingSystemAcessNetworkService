"""Tests for GET /api/sysinfo (system_info, no parameters)."""

import platform
import unittest

from tests.helpers import get_json, request


class TestSysinfo(unittest.TestCase):
    def test_ok_structure(self):
        status, _, data = get_json("/api/sysinfo")
        self.assertEqual(status, 200)
        for key in ("system", "system_name", "release", "version",
                    "machine", "processor", "hostname", "python_version",
                    "cpu_count", "distro", "windows", "mac"):
            self.assertIn(key, data)
        self.assertTrue(data["system"])
        self.assertTrue(data["system_name"])
        self.assertTrue(data["machine"])
        # python_version is impl-defined: the CPython version string on
        # the Python server, null on ports without a Python runtime
        # (the Kotlin port reports jvm_version/kotlin_version instead).
        self.assertIn("python_version", data)
        expected_name = {"Darwin": "macOS"}.get(data["system"],
                                                data["system"])
        self.assertEqual(data["system_name"], expected_name)

    @unittest.skipUnless(platform.system() == "Linux",
                         "Linux-only distro assertions")
    def test_linux_distro(self):
        status, _, data = get_json("/api/sysinfo")
        self.assertEqual(status, 200)
        self.assertNotEqual(data["distro"], "null")
        for key in ("id", "name", "version", "version_id", "pretty_name"):
            self.assertIn(key, data["distro"])
        self.assertTrue(data["distro"]["id"])
        self.assertEqual(data["windows"], "null")
        self.assertEqual(data["mac"], "null")

    def test_no_token_401(self):
        status, _, _ = request("GET", "/api/sysinfo", auth=False)
        self.assertEqual(status, 401)

    def test_post_405(self):
        status, _, _ = request("POST", "/api/sysinfo")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
