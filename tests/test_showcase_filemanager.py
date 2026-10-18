"""Tests for showcase/filemanager (pure HTML/CSS/JS file manager).

The app uses ONLY the low-level webservice functions, so this module
checks both sides of that contract: the static side (the three files
exist, load locally with no external URLs) and the live side (the
exact API call sequence the app performs works against the server:
help caps -> dir listing with metadata -> create -> read -> overwrite
-> raw download -> delete file -> delete dir).
"""

import json
import os
import re
import shutil
import subprocess
import unittest

from tests.helpers import base_url, json_request, quote, request, tv, workspace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOW = os.path.join(ROOT, "showcase", "filemanager")


class TestShowcaseFiles(unittest.TestCase):
    def test_files_exist(self):
        for name in ("index.html", "styles.css", "app.js"):
            path = os.path.join(SHOW, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertGreater(os.path.getsize(path), 0, name)

    def test_html_wires_css_and_js(self):
        html = open(os.path.join(SHOW, "index.html"),
                    encoding="utf-8").read()
        self.assertIn('href="styles.css"', html)
        self.assertIn('src="app.js"', html)

    def test_offline_pure(self):
        """No external URLs: works from file:// with no network."""
        for name in ("index.html", "styles.css", "app.js"):
            text = open(os.path.join(SHOW, name), encoding="utf-8").read()
            for bad in ("https://", "http://cdn", "cdn.jsdelivr",
                        "unpkg.com", "googleapis", "@import",
                        "<script src=\"http"):
                self.assertNotIn(bad, text, "%s contains %s" % (name, bad))
        html = open(os.path.join(SHOW, "index.html"),
                    encoding="utf-8").read()
        # No remote *resources* (the serverUrl default value is just
        # a configurable API address, not something the page loads).
        for bad in ("src=\"http", "href=\"http"):
            self.assertNotIn(bad, html)

    def test_js_syntax(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not available")
        proc = subprocess.run(
            [node, "--check", os.path.join(SHOW, "app.js")],
            capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))

    def test_app_uses_only_file_api(self):
        """app.js talks to these routes and no others."""
        js = open(os.path.join(SHOW, "app.js"), encoding="utf-8").read()
        for route in ("/api/help", "/api/dir", "/api/file",
                      "/api/sysinfo"):
            self.assertIn(route, js)
        for route in ("/api/shell", "/api/sqlite", "/api/socket"):
            self.assertNotIn(route, js)


class TestShowcaseAppFlow(unittest.TestCase):
    """Mirror of the app's call sequence against the live server."""

    def test_browse_create_edit_delete(self):
        work = workspace("showcase")
        # connect(): GET /api/help caps.
        status, _, data = json_request("GET", "/api/help", auth=False)
        self.assertEqual(status, 200)
        names = {r["name"] for r in data["routes"]}
        for cap in ("read_directory", "read_file", "create_file",
                    "overwrite_file", "delete_file", "delete_directory"):
            self.assertIn(cap, names)
        # listDir(): GET /api/dir with entry metadata.
        status, _, data = json_request(
            "GET", "/api/dir?path=" + quote(work))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["count"]), 0)
        # createFile(): POST /api/file.
        target = "/api/file?path=" + quote(
            os.path.join(work, "hello.txt"))
        status, _, data = json_request(
            "POST", target, {"content": "hi", "encoding": "text"})
        self.assertEqual(status, 201, data)
        # listDir() again: one entry with full metadata.
        status, _, data = json_request(
            "GET", "/api/dir?path=" + quote(work))
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["count"]), 1)
        entry = data["entries"][0]
        for key in ("name", "path", "type", "size", "atime", "mtime",
                    "ctime", "crtime", "mode"):
            self.assertIn(key, entry)
        self.assertEqual(entry["name"], "hello.txt")
        # openFile(): GET /api/file JSON mode.
        status, _, data = json_request("GET", target)
        self.assertEqual(status, 200)
        self.assertEqual(data["content"], "hi")
        # saveFile(): PUT /api/file.
        status, _, data = json_request(
            "PUT", target, {"content": "hi v2", "encoding": "text"})
        self.assertEqual(status, 200, data)
        status, _, data = json_request("GET", target)
        self.assertEqual(data["content"], "hi v2")
        # downloadFile(): GET /api/file raw=1.
        status, headers, body = request("GET", target + "&raw=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hi v2")
        # deleteFile(): DELETE /api/file.
        status, _, _ = request("DELETE", target)
        self.assertEqual(status, 200)
        # deleteDir(): DELETE /api/dir on the now-empty workspace.
        status, _, _ = request(
            "DELETE", "/api/dir?path=" + quote(work))
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
