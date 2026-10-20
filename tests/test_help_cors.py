"""Tests for GET /, GET /api/help, CORS and routing contract."""

import unittest

from tests.helpers import get_json, request, tv


class TestHelpCors(unittest.TestCase):
    def test_index(self):
        status, _, data = get_json("/")
        self.assertEqual(status, 200)
        self.assertEqual(data["help"], "/api/help")
        self.assertIn("version", data)

    def test_help_lists_all_routes(self):
        status, _, data = get_json("/api/help")
        self.assertEqual(status, 200)
        names = {r["name"] for r in data["routes"]}
        # Every port implements the same 24 routes: the 12 filesystem /
        # shell / sqlite / sysinfo ones, the 6 socket ones (socket_ws +
        # socket_starttls), the 5 Gmail OAuth2 service routes and the
        # index. The contract is identical on all nine ports, so the
        # catalog is asserted exactly (not "at least").
        expected = {
            "help", "read_directory", "read_file", "index",
            "create_file", "overwrite_file", "delete_file",
            "delete_directory", "delete_directory_shallow", "exec_shell",
            "sqlite_create", "sqlite_query", "system_info",
            "socket_open", "socket_send", "socket_recv", "socket_close",
            "socket_starttls", "socket_ws",
            "oauth2_start", "oauth2_callback", "oauth2_poll",
            "oauth2_refresh", "oauth2_close",
        }
        self.assertEqual(names, expected)
        self.assertEqual(tv(data["count"]), len(data["routes"]))
        self.assertEqual(tv(data["count"]), 24)
        self.assertEqual(tv(data["auth"]["required"]), True)
        for route in data["routes"]:
            for key in ("method", "url", "name", "description", "params"):
                self.assertIn(key, route)

    def test_help_delete_directory_documents_shallow_alias(self):
        """DELETE /api/dir?shallow=1 is advertised in the catalog."""
        status, _, data = get_json("/api/help")
        self.assertEqual(status, 200)
        by_name = {r["name"]: r for r in data["routes"]}
        params = {p["name"]: p for p in by_name["delete_directory"]["params"]}
        self.assertIn("shallow", params)
        self.assertEqual(params["shallow"]["location"], "query")
        self.assertEqual(tv(params["shallow"]["required"]), False)
        self.assertEqual(params["shallow"]["default"], "0")
        self.assertEqual(params["shallow"]["allowed"], ["0", "1"])

    def test_cors_on_get(self):
        status, headers, _ = request(
            "GET", "/", headers={"Origin": "http://example.com"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")

    def test_cors_on_error(self):
        status, headers, _ = request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")

    def test_options_preflight(self):
        status, headers, _ = request(
            "OPTIONS", "/api/file",
            headers={"Origin": "http://example.com",
                     "Access-Control-Request-Method": "PUT"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")

    def test_unknown_route_404(self):
        status, _, _ = request("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_wrong_method_405(self):
        status, _, _ = request("POST", "/api/dir?path=.")
        self.assertEqual(status, 405)
        status, _, _ = request("PATCH", "/")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
