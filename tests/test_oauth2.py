"""Tests for the service-side OAuth2 helper (`/api/oauth2/*`).

Capability-gated: ports without the routes skip everything (only the
Go reference implements them for now). A local fake Google stands in
for the provider, so the whole Authorization Code + PKCE flow runs
offline through the service: start (PKCE S256), the loopback callback
(open route, state-correlated, server-side code exchange), poll,
refresh and close, plus the validation/error paths.
"""

import base64
import hashlib
import http.server
import json
import threading
import unittest
import urllib.parse

from tests.helpers import get_json, json_request, request, tv


def oauth_capable():
    status, _, data = get_json("/api/help")
    assert status == 200, data
    return any(r["name"] == "oauth2_start" for r in data["routes"])


class FakeGoogle:
    """Minimal OAuth token endpoint (the test plays the browser)."""

    def __init__(self):
        self.expected_code = "code-123"
        self.expected_client_id = "client-abc"
        self.expected_redirect = None
        self.challenge = None
        self.token_calls = []
        self.access_seq = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output quiet
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                form = urllib.parse.parse_qs(
                    self.rfile.read(n).decode("utf-8"))
                outer.token_calls.append((self.path, form))
                if self.path != "/token":
                    self.send_response(404)
                    self.end_headers()
                    return
                grant = form.get("grant_type", [""])[0]
                if grant == "authorization_code":
                    verifier = form.get("code_verifier", [""])[0]
                    digest = hashlib.sha256(verifier.encode()).digest()
                    calc = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                    ok = (
                        form.get("code", [""])[0] == outer.expected_code
                        and form.get("client_id", [""])[0] == outer.expected_client_id
                        and form.get("redirect_uri", [""])[0] == outer.expected_redirect
                        and calc == outer.challenge
                    )
                    if not ok:
                        self._send(400, {
                            "error": "invalid_grant",
                            "error_description": "pkce/code mismatch"})
                        return
                    outer.access_seq += 1
                    self._send(200, {
                        "access_token": "access-%d" % outer.access_seq,
                        "refresh_token": "refresh-1",
                        "expires_in": 3599,
                        "token_type": "Bearer",
                        "scope": "https://mail.google.com/"})
                elif grant == "refresh_token":
                    outer.access_seq += 1
                    self._send(200, {
                        "access_token": "access-%d" % outer.access_seq,
                        "expires_in": 3599,
                        "token_type": "Bearer"})
                else:
                    self._send(400, {"error": "unsupported_grant_type"})

            def _send(self, status, obj):
                raw = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class OAuth2Base(unittest.TestCase):
    def setUp(self):
        if not oauth_capable():
            self.skipTest("no OAuth2 routes on this port")

    def start_session(self, fake, **extra):
        body = {
            "client_id": fake.expected_client_id,
            "client_secret": "secret-xyz",
            "auth_url": fake.base + "/auth",
            "token_url": fake.base + "/token",
        }
        body.update(extra)
        status, _, data = json_request("POST", "/api/oauth2/start", body)
        self.assertEqual(status, 201, data)
        return data

    def callback(self, data, *, code="code-123", error=None,
                 state=None, auth=False):
        params = {"state": state or auth_state(data)}
        if error is not None:
            params["error"] = error
        else:
            params["code"] = code
        target = "/api/oauth2/callback?" + urllib.parse.urlencode(params)
        return request("GET", target, auth=auth)

    def prime_fake(self, data, fake):
        q = urllib.parse.parse_qs(
            urllib.parse.urlparse(data["auth_url"]).query)
        fake.challenge = q["code_challenge"][0]
        fake.expected_redirect = data["redirect_uri"]
        return q


def auth_state(data):
    return urllib.parse.parse_qs(
        urllib.parse.urlparse(data["auth_url"]).query)["state"][0]


class TestOAuth2Catalog(OAuth2Base):
    def test_help_lists_oauth2_routes(self):
        status, _, data = get_json("/api/help")
        self.assertEqual(status, 200)
        names = {r["name"] for r in data["routes"]}
        for name in ("oauth2_start", "oauth2_callback", "oauth2_poll",
                     "oauth2_refresh", "oauth2_close"):
            self.assertIn(name, names)
        self.assertEqual(tv(data["count"]), len(data["routes"]))
        self.assertIn("/api/oauth2/callback", data["auth"]["open_routes"])

    def test_start_requires_json_content_type(self):
        status, _, _ = request("POST", "/api/oauth2/start", body=b"")
        self.assertEqual(status, 400)

    def test_start_requires_client_id(self):
        status, _, _ = json_request(
            "POST", "/api/oauth2/start", {"client_id": "  "})
        self.assertEqual(status, 400)

    def test_start_defaults_to_google(self):
        status, _, data = json_request(
            "POST", "/api/oauth2/start", {"client_id": "cid"})
        self.assertEqual(status, 201, data)
        self.assertTrue(data["auth_url"].startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertIn("code_challenge_method=S256", data["auth_url"])
        self.assertTrue(data["redirect_uri"].endswith("/api/oauth2/callback"))
        request("POST", "/api/oauth2/close?id=" + data["id"])

    def test_bad_urls_rejected(self):
        status, _, _ = json_request("POST", "/api/oauth2/start",
                                    {"client_id": "x", "token_url": "not a url"})
        self.assertEqual(status, 400)
        status, _, _ = json_request("POST", "/api/oauth2/start",
                                    {"client_id": "x", "auth_url": "ftp://nope"})
        self.assertEqual(status, 400)

    def test_poll_missing_and_unknown_id(self):
        status, _, _ = request("GET", "/api/oauth2/poll")
        self.assertEqual(status, 400)
        status, _, _ = request("GET", "/api/oauth2/poll?id=nope")
        self.assertEqual(status, 404)

    def test_wrong_methods(self):
        status, _, _ = request("GET", "/api/oauth2/start")
        self.assertEqual(status, 405)
        status, _, _ = request("PUT", "/api/oauth2/poll?id=x")
        self.assertEqual(status, 405)
        status, _, _ = request("DELETE", "/api/oauth2/close?id=x")
        self.assertEqual(status, 405)
        status, _, _ = request("POST", "/api/oauth2/callback?state=x")
        self.assertEqual(status, 405)


class TestOAuth2Flow(OAuth2Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeGoogle()
        self.addCleanup(self.fake.stop)

    def test_full_flow_pkce_refresh_close(self):
        data = self.start_session(self.fake)
        q = self.prime_fake(data, self.fake)

        # Authorization URL shape.
        self.assertEqual(q["client_id"], [self.fake.expected_client_id])
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["redirect_uri"], [data["redirect_uri"]])
        self.assertEqual(q["scope"], ["https://mail.google.com/"])
        self.assertEqual(q["access_type"], ["offline"])

        # The browser lands on the loopback callback without our token.
        status, headers, raw = self.callback(data)
        self.assertEqual(status, 200, raw)
        self.assertIn("text/html", headers.get("content-type", ""))

        # The fake verified the PKCE S256 verifier during the exchange.
        status, _, poll = json_request(
            "GET", "/api/oauth2/poll?id=" + data["id"])
        self.assertEqual(status, 200, poll)
        self.assertEqual(poll["status"], "authorized")
        self.assertEqual(poll["access_token"], "access-1")
        self.assertEqual(tv(poll["expires_in"]), 3599)
        self.assertNotIn("refresh_token", poll)

        # Refresh mints a new access token from the kept refresh token.
        status, _, ref = json_request(
            "POST", "/api/oauth2/refresh?id=" + data["id"])
        self.assertEqual(status, 200, ref)
        self.assertEqual(ref["access_token"], "access-2")
        self.assertEqual(self.fake.token_calls[-1][1]["grant_type"],
                         ["refresh_token"])

        # Close forgets the session.
        status, _, closed = json_request(
            "POST", "/api/oauth2/close?id=" + data["id"])
        self.assertEqual(status, 200)
        self.assertEqual(closed["closed"], "b:true")
        status, _, _ = request("GET", "/api/oauth2/poll?id=" + data["id"])
        self.assertEqual(status, 404)

    def test_client_secret_optional(self):
        data = self.start_session(self.fake, client_secret="")
        self.assertNotIn("secret-xyz", data["auth_url"])
        self.prime_fake(data, self.fake)
        status, _, _ = self.callback(data)
        self.assertEqual(status, 200)
        status, _, poll = json_request(
            "GET", "/api/oauth2/poll?id=" + data["id"])
        self.assertEqual(poll["status"], "authorized")

    def test_callback_unknown_state(self):
        status, _, _ = request(
            "GET", "/api/oauth2/callback?code=x&state=nope", auth=False)
        self.assertEqual(status, 400)

    def test_callback_provider_error(self):
        data = self.start_session(self.fake)
        self.prime_fake(data, self.fake)
        status, _, _ = self.callback(data, error="access_denied")
        self.assertEqual(status, 400)
        status, _, poll = json_request(
            "GET", "/api/oauth2/poll?id=" + data["id"])
        self.assertEqual(poll["status"], "error")
        self.assertIn("access_denied", poll["error"])

    def test_callback_repeated_is_idempotent(self):
        data = self.start_session(self.fake)
        self.prime_fake(data, self.fake)
        status, _, _ = self.callback(data)
        self.assertEqual(status, 200)
        status, _, _ = self.callback(data)
        self.assertEqual(status, 200)  # "already processed", not a re-exchange
        self.assertEqual(len(self.fake.token_calls), 1)

    def test_refresh_before_authorization(self):
        data = self.start_session(self.fake)
        status, _, _ = json_request(
            "POST", "/api/oauth2/refresh?id=" + data["id"])
        self.assertEqual(status, 400)

    def test_bad_pkce_is_reported(self):
        data = self.start_session(self.fake)
        self.prime_fake(data, self.fake)
        self.fake.challenge = "wrong-challenge"  # force the mismatch
        status, _, _ = self.callback(data)
        self.assertEqual(status, 400)
        status, _, poll = json_request(
            "GET", "/api/oauth2/poll?id=" + data["id"])
        self.assertEqual(poll["status"], "error")
        self.assertIn("pkce", poll["error"])


if __name__ == "__main__":
    unittest.main()
