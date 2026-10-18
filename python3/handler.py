"""Request handler: response plumbing, auth, dispatch.
Endpoint implementations live in the handler_* mixins."""

import hmac
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from handler_fs import HandlerFsMixin
from handler_shell import HandlerShellMixin
from handler_socket import HandlerSocketMixin
from handler_socket_ws import HandlerSocketWsMixin
from handler_sqlite import HandlerSqliteMixin
from handler_sysinfo import HandlerSysinfoMixin
from handler_oauth2 import HandlerOAuth2Mixin
from hosts import HOST_DENIED_MSG, host_allowed
from routes import OPEN_ROUTES, ROUTES
from tags import jenc
from version import VERSION



class Handler(HandlerFsMixin, HandlerShellMixin,
                HandlerSocketMixin, HandlerSocketWsMixin,
                HandlerSqliteMixin, HandlerSysinfoMixin,
                HandlerOAuth2Mixin,
                BaseHTTPRequestHandler):
    """Dispatching handler (see handler_* mixins for endpoints)."""
    server_version = "GnumarusNativeOps/" + VERSION

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message, path=None):
        payload = {"error": self._status_label(status), "message": message}
        if path is not None:
            payload["path"] = path
        self._send_json(payload, status=status)

    @staticmethod

    def _status_label(status):
        return {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
        }.get(status, "error")

    def log_message(self, fmt, *args):  # keep default logging to stderr
        super().log_message(fmt, *args)

    def _require_auth(self):
        """Enforce the API token. Returns True when the request may proceed.

        The Host header is gated first (loopback literals only),
        uniformly for every method, route and upgrade. GET / and
        GET /api/help plus all OPTIONS preflight requests are always
        open. Everything else needs `Authorization: Bearer <token>`
        (preferred) or `?token=`.
        """
        if not host_allowed(self.headers.get("Host", "")):
            self._send_error_json(403, HOST_DENIED_MSG)
            return False
        if not getattr(self.server, "auth_required", True):
            return True
        parsed = urlparse(self.path)
        if self.command == "GET" and parsed.path in OPEN_ROUTES:
            return True
        supplied = ""
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            supplied = auth_header[len("Bearer "):].strip()
        if not supplied:
            tokens = parse_qs(parsed.query).get("token", [])
            if tokens:
                supplied = tokens[0]
        expected = getattr(self.server, "auth_token", "")
        if expected and hmac.compare_digest(supplied, expected):
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("WWW-Authenticate", "Bearer")
        self._send_cors_headers()
        body = json.dumps({
            "error": "unauthorized",
            "message": "Valid API token required: send "
                       "Authorization: Bearer <token> or ?token=.",
        }).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_OPTIONS(self):
        if not host_allowed(self.headers.get("Host", "")):
            self._send_error_json(403, HOST_DENIED_MSG)
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/api/help":
            self._handle_help()
        elif route == "/api/dir":
            self._handle_read_dir(query)
        elif route == "/api/file":
            self._handle_read_file(query)
        elif route == "/api/socket/recv":
            self._handle_socket_recv(query)
        elif route == "/api/socket/ws":
            self._handle_socket_ws(query)
        elif route == "/":
            self._send_json({
                "service": "GnumarusPythonNativeOperationsHttpService",
                "version": VERSION,
                "help": "/api/help",
            })
        elif route == "/api/shell":
            self._send_error_json(405, "Method GET not supported for this route (use POST).")
        elif route in ("/api/sqlite/create", "/api/sqlite/query"):
            self._send_error_json(405, "Method GET not supported for this route (use POST).")
        elif route in ("/api/socket/open", "/api/socket/send",
                       "/api/socket/close", "/api/socket/starttls"):
            self._send_error_json(405, "Method GET not supported for this route (use POST).")
        elif route == "/api/sysinfo":
            self._handle_sysinfo()
        elif route == "/api/oauth2/callback":
            self._handle_oauth2_callback(query)
        elif route == "/api/oauth2/poll":
            self._handle_oauth2_poll(query)
        elif route in ("/api/oauth2/start", "/api/oauth2/refresh",
                       "/api/oauth2/close"):
            self._send_error_json(405, "Method GET not supported for this route.")
        else:
            self._send_error_json(404, "Unknown route: %s" % route)

    def do_POST(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == "/api/file":
            self._handle_create_file(query)
        elif route == "/api/shell":
            self._handle_exec_shell()
        elif route == "/api/sqlite/create":
            self._handle_sqlite_create(query)
        elif route == "/api/sqlite/query":
            self._handle_sqlite_query(query)
        elif route == "/api/socket/open":
            self._handle_socket_open()
        elif route == "/api/socket/send":
            self._handle_socket_send(query)
        elif route == "/api/socket/close":
            self._handle_socket_close(query)
        elif route == "/api/socket/starttls":
            self._handle_socket_starttls(query)
        elif route == "/api/oauth2/start":
            self._handle_oauth2_start()
        elif route == "/api/oauth2/refresh":
            self._handle_oauth2_refresh(query)
        elif route == "/api/oauth2/close":
            self._handle_oauth2_close(query)
        elif route in ("/api/oauth2/callback", "/api/oauth2/poll"):
            self._send_error_json(405, "Method POST not supported for this route.")
        elif route in ("/", "/api/help", "/api/dir", "/api/dir/shallow",
                       "/api/shallow_dir", "/api/dir_shallow", "/api/sysinfo",
                       "/api/socket/recv", "/api/socket/ws"):
            self._send_error_json(405, "Method POST not supported for this route.")
        else:
            self._send_error_json(404, "Unknown route: %s" % route)

    def do_PUT(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == "/api/file":
            self._handle_overwrite_file(query)
        elif route in ("/", "/api/help", "/api/dir", "/api/dir/shallow",
                       "/api/shallow_dir", "/api/dir_shallow", "/api/shell",
                       "/api/oauth2/start", "/api/oauth2/callback",
                       "/api/oauth2/poll", "/api/oauth2/refresh",
                       "/api/oauth2/close",
                       "/api/sqlite/create", "/api/sqlite/query",
                       "/api/sysinfo", "/api/socket/open",
                       "/api/socket/send", "/api/socket/recv",
                       "/api/socket/close", "/api/socket/starttls",
                       "/api/socket/ws"):
            self._send_error_json(405, "Method PUT not supported for this route.")
        else:
            self._send_error_json(404, "Unknown route: %s" % route)

    def do_DELETE(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == "/api/file":
            self._handle_delete_file(query)
        elif route == "/api/dir":
            self._handle_delete_dir(query)
        elif route in ("/api/dir/shallow", "/api/shallow_dir", "/api/dir_shallow"):
            self._handle_delete_dir_shallow(query)
        elif route in ("/", "/api/help", "/api/shell",
                       "/api/sqlite/create", "/api/sqlite/query",
                       "/api/sysinfo", "/api/socket/open",
                       "/api/socket/send", "/api/socket/recv",
                       "/api/socket/close", "/api/socket/starttls",
                       "/api/socket/ws",
                       "/api/oauth2/start", "/api/oauth2/callback",
                       "/api/oauth2/poll", "/api/oauth2/refresh",
                       "/api/oauth2/close"):
            self._send_error_json(405, "Method DELETE not supported for this route.")
        else:
            self._send_error_json(404, "Unknown route: %s" % route)

    def do_PATCH(self):
        if not self._require_auth():
            return
        self._send_error_json(405, "Method PATCH not supported.")

    def _handle_help(self):
        self._send_json({
            "version": VERSION,
            "count": jenc(len(ROUTES)),
            "routes": ROUTES,
            "auth": {
                "required": jenc(bool(getattr(self.server, "auth_required", True))),
                "open_routes": list(OPEN_ROUTES) + ["OPTIONS *"],
                "how": "Authorization: Bearer <token> header (preferred) "
                       "or ?token= query parameter.",
            },
        })
