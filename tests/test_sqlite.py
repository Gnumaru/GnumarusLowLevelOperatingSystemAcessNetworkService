"""Tests for POST /api/sqlite/create and POST /api/sqlite/query."""

import base64
import os
import unittest

from tests.helpers import (json_request, quote, request, ti, tv,
                            workspace)

SQLITE_REASON = "server has no SQLite support (GNATIVE_TEST_SQLITE=0)"


def create_db(path):
    return json_request("POST", "/api/sqlite/create?path=" + quote(path))


def query_db(path, obj):
    return json_request("POST", "/api/sqlite/query?path=" + quote(path), obj)


@unittest.skipIf(os.environ.get("GNATIVE_TEST_SQLITE") == "0", SQLITE_REASON)
class TestSqliteCreate(unittest.TestCase):
    def setUp(self):
        self.ws = workspace("sqlite_create")

    def test_create_ok(self):
        target = os.path.join(self.ws, "app.db")
        status, _, data = create_db(target)
        self.assertEqual(status, 201)
        self.assertEqual(data["absolute_path"], target)
        self.assertIn("sqlite_version", data)
        self.assertTrue(os.path.isfile(target))
        # the file is a working database right away
        status, _, data = query_db(target, {"sql": "SELECT 1 + 1 AS two"})
        self.assertEqual(status, 200)
        self.assertEqual(data["columns"], ["two"])
        self.assertEqual(data["rows"], [["i:2"]])

    def test_create_existing_409(self):
        target = os.path.join(self.ws, "taken.db")
        status, _, _ = create_db(target)
        self.assertEqual(status, 201)
        status, _, _ = create_db(target)
        self.assertEqual(status, 409)

    def test_create_missing_parent_404(self):
        status, _, _ = create_db(os.path.join(self.ws, "nodir", "x.db"))
        self.assertEqual(status, 404)

    def test_create_missing_path_400(self):
        status, _, _ = json_request("POST", "/api/sqlite/create")
        self.assertEqual(status, 400)


@unittest.skipIf(os.environ.get("GNATIVE_TEST_SQLITE") == "0", SQLITE_REASON)
class TestSqliteQuery(unittest.TestCase):
    def setUp(self):
        self.ws = workspace("sqlite_query")
        self.db = os.path.join(self.ws, "t.db")
        status, _, _ = create_db(self.db)
        self.assertEqual(status, 201)

    def test_stateless_roundtrip(self):
        # separate requests share nothing but the file: proves statelessness
        status, _, data = query_db(
            self.db, {"sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"})
        self.assertEqual(status, 200)
        status, _, data = query_db(
            self.db, {"sql": "INSERT INTO users (name) VALUES (?)", "params": ["s:ada"]})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["rowcount"]), 1)
        self.assertEqual(tv(data["lastrowid"]), 1)
        status, _, data = query_db(
            self.db, {"sql": "SELECT id, name FROM users"})
        self.assertEqual(status, 200)
        self.assertEqual(data["columns"], ["id", "name"])
        self.assertEqual(data["rows"], [["i:1", "s:ada"]])
        self.assertEqual(tv(data["rowcount"]), 1)

    def test_named_params(self):
        query_db(self.db, {"sql": "CREATE TABLE k (a TEXT, b INTEGER)"})
        status, _, _ = query_db(
            self.db, {"sql": "INSERT INTO k (a, b) VALUES (:a, :b)",
                      "params": {"a": "s:x", "b": "i:7"}})
        self.assertEqual(status, 200)
        status, _, data = query_db(self.db, {"sql": "SELECT a, b FROM k"})
        self.assertEqual(data["rows"], [["s:x", "i:7"]])

    def test_null_and_types(self):
        status, _, data = query_db(
            self.db, {"sql": "SELECT NULL AS n, 1.5 AS f, 'txt' AS s"})
        self.assertEqual(status, 200)
        self.assertEqual(data["columns"], ["n", "f", "s"])
        self.assertEqual(data["rows"], [["null", "f:1.5", "s:txt"]])

    def test_blob_as_base64(self):
        status, _, data = query_db(
            self.db, {"sql": "SELECT randomblob(4) AS b"})
        self.assertEqual(status, 200)
        cell = data["rows"][0][0]
        self.assertTrue(cell.startswith("s:"))
        raw = base64.b64decode(cell[2:])
        self.assertEqual(len(raw), 4)

    def test_update_delete_counters(self):
        query_db(self.db, {"sql": "CREATE TABLE n (v INTEGER)"})
        query_db(self.db, {"sql": "INSERT INTO n (v) VALUES (1), (2), (3)"})
        status, _, data = query_db(
            self.db, {"sql": "UPDATE n SET v = v + 10 WHERE v > 1"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["rowcount"]), 2)
        status, _, data = query_db(self.db, {"sql": "SELECT v FROM n ORDER BY v"})
        self.assertEqual(data["rows"], [["i:1"], ["i:12"], ["i:13"]])

    def test_readonly(self):
        status, _, data = query_db(
            self.db, {"sql": "SELECT name FROM sqlite_master", "readonly": "b:true"})
        self.assertEqual(status, 200)
        status, _, _ = query_db(
            self.db, {"sql": "CREATE TABLE nope (x)", "readonly": "b:true"})
        self.assertEqual(status, 400)

    def test_missing_db_404(self):
        status, _, _ = query_db(
            os.path.join(self.ws, "ghost.db"), {"sql": "SELECT 1"})
        self.assertEqual(status, 404)

    def test_bad_sql_400(self):
        status, _, data = query_db(
            self.db, {"sql": "SELECT * FROM no_such_table"})
        self.assertEqual(status, 400)
        self.assertIn("no_such_table", data["message"])

    def test_multi_statement_400(self):
        status, _, _ = query_db(
            self.db, {"sql": "SELECT 1; SELECT 2"})
        self.assertEqual(status, 400)

    def test_missing_sql_400(self):
        status, _, _ = query_db(self.db, {})
        self.assertEqual(status, 400)

    def test_missing_path_400(self):
        status, _, _ = json_request(
            "POST", "/api/sqlite/query", {"sql": "SELECT 1"})
        self.assertEqual(status, 400)

    def test_bad_params_400(self):
        status, _, _ = query_db(self.db, {"sql": "SELECT ?", "params": "x"})
        self.assertEqual(status, 400)
        status, _, _ = query_db(
            self.db, {"sql": "SELECT 1", "params": [{"nested": 1}]})
        self.assertEqual(status, 400)
        status, _, _ = query_db(
            self.db, {"sql": "SELECT ?", "params": ["z:5"]})
        self.assertEqual(status, 400)
        status, _, _ = query_db(
            self.db, {"sql": "SELECT ?", "params": [5]})
        self.assertEqual(status, 400)

    def test_bad_timeout_400(self):
        status, _, _ = query_db(
            self.db, {"sql": "SELECT 1", "timeout": "long"})
        self.assertEqual(status, 400)

    def test_bad_readonly_400(self):
        status, _, _ = query_db(
            self.db, {"sql": "SELECT 1", "readonly": "yes"})
        self.assertEqual(status, 400)

    def test_non_json_body_400(self):
        status, _, _ = request(
            "POST", "/api/sqlite/query?path=" + quote(self.db),
            body=b"SELECT 1", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)

    def test_wrong_methods_405(self):
        status, _, _ = request(
            "GET", "/api/sqlite/query?path=" + quote(self.db))
        self.assertEqual(status, 405)
        status, _, _ = request(
            "GET", "/api/sqlite/create?path=" + quote(self.db))
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
