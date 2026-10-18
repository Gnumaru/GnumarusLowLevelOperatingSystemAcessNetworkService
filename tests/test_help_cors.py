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
        base = {
            "help", "read_directory", "read_file", "index",
            "create_file", "overwrite_file", "delete_file",
            "delete_directory", "exec_shell",
            "sqlite_create", "sqlite_query", "system_info",
            "socket_open", "socket_send", "socket_recv", "socket_close",
        }
        # socket_ws only exists on ports with a hijackable HTTP layer;
        # socket_starttls only where the port does TLS (Go for now);
        # the oauth2_* routes are the service-side Gmail OAuth2 helper
        # (Go reference only for now).
        optional = {"socket_ws", "socket_starttls",
                    "oauth2_start", "oauth2_callback", "oauth2_poll",
                    "oauth2_refresh", "oauth2_close"}
        self.assertTrue(base <= names <= base | optional, names)
        self.assertEqual(tv(data["count"]), len(data["routes"]))
        self.assertEqual(tv(data["auth"]["required"]), True)
        for route in data["routes"]:
            for key in ("method", "url", "name", "description", "params"):
                self.assertIn(key, route)

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
