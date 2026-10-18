"""OAuth2 helper (Authorization Code + PKCE) mixin."""

import base64
from tags import jenc
import hashlib
import html
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

OAUTH_DEFAULT_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_DEFAULT_SCOPE = "https://mail.google.com/"
OAUTH_CALLBACK_PATH = "/api/oauth2/callback"
OAUTH_IDLE_TIMEOUT = 600
OAUTH_MAX_SESSIONS = 64
OAUTH_EXCHANGE_TIMEOUT = 20

_oauth_sessions = {}
_oauth_lock = threading.Lock()
_oauth_sweeper_started = False


def _oauth_sweep():
    global _oauth_sweeper_started
    if _oauth_sweeper_started:
        return
    _oauth_sweeper_started = True
    def loop():
        while True:
            time.sleep(60)
            now = time.time()
            with _oauth_lock:
                for sid in [k for k, s in _oauth_sessions.items()
                            if now - s["last"] > OAUTH_IDLE_TIMEOUT]:
                    del _oauth_sessions[sid]
    t = threading.Thread(target=loop, daemon=True)
    t.start()


def _oauth_random(n):
    return secrets.token_urlsafe(n)


def _oauth_valid_url(raw):
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


def _oauth_post_form(token_url, form):
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(token_url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=OAUTH_EXCHANGE_TIMEOUT) as resp:
            body = resp.read(1 << 20)
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read(1 << 20)
        status = e.code
    except Exception as e:
        return None, str(e)
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        payload = {}
    def get(k):
        v = payload.get(k)
        return v if isinstance(v, str) else ""
    if status != 200:
        msg = get("error_description") or get("error")
        if not msg:
            msg = "HTTP %d" % status
        return None, msg
    tok = {
        "access": get("access_token"),
        "refresh": get("refresh_token"),
        "token_type": get("token_type"),
        "scope": get("scope"),
    }
    ei = payload.get("expires_in")
    if isinstance(ei, (int, float)):
        tok["expires"] = int(ei)
    else:
        tok["expires"] = 0
    if not tok["access"]:
        return None, "token response without an access_token"
    if not tok["token_type"]:
        tok["token_type"] = "Bearer"
    if not tok["expires"]:
        tok["expires"] = 3600
    return tok, None


class HandlerOAuth2Mixin:
    def _handle_oauth2_start(self):
        _oauth_sweep()
        ctype = self.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            self._send_error_json(400, "Content-Type must be application/json with body { client_id, client_secret?, auth_url?, token_url?, scope? }.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            obj = {}
        else:
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_error_json(400, "Invalid JSON body.")
                return
        if not isinstance(obj, dict):
            self._send_error_json(400, "JSON body must be an object.")
            return

        def get_str(key):
            if key not in obj or obj[key] is None:
                return "", True
            v = obj[key]
            if not isinstance(v, str):
                return "", False
            return v, True

        client_id, ok = get_str("client_id")
        if not ok:
            self._send_error_json(400, "Field 'client_id' must be a string.")
            return
        client_id = client_id.strip()
        if not client_id:
            self._send_error_json(400, "Missing or empty required field: client_id.")
            return
        client_secret, ok = get_str("client_secret")
        if not ok:
            self._send_error_json(400, "Field 'client_secret' must be a string.")
            return
        auth_url, ok = get_str("auth_url")
        if not ok:
            self._send_error_json(400, "Field 'auth_url' must be a string.")
            return
        token_url, ok = get_str("token_url")
        if not ok:
            self._send_error_json(400, "Field 'token_url' must be a string.")
            return
        scope, ok = get_str("scope")
        if not ok:
            self._send_error_json(400, "Field 'scope' must be a string.")
            return
        auth_url = auth_url.strip()
        token_url = token_url.strip()
        scope = scope.strip()
        if not auth_url:
            auth_url = OAUTH_DEFAULT_AUTH_URL
        if not token_url:
            token_url = OAUTH_DEFAULT_TOKEN_URL
        if not scope:
            scope = OAUTH_DEFAULT_SCOPE
        if not _oauth_valid_url(auth_url):
            self._send_error_json(400, "Field 'auth_url' must be an absolute http(s) URL.")
            return
        if not _oauth_valid_url(token_url):
            self._send_error_json(400, "Field 'token_url' must be an absolute http(s) URL.")
            return

        host = self.headers.get("Host", "")
        redirect_uri = "http://" + host + OAUTH_CALLBACK_PATH
        state = _oauth_random(24)
        verifier = _oauth_random(32)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

        import urllib.parse as _up
        vals = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        sep = "&" if "?" in auth_url else "?"
        full = auth_url + sep + _up.urlencode(vals)

        with _oauth_lock:
            if len(_oauth_sessions) >= OAUTH_MAX_SESSIONS:
                self._send_error_json(400, "Too many open OAuth sessions (%d)." % OAUTH_MAX_SESSIONS)
                return
        sid = secrets.token_urlsafe(16)
        sess = {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_url": auth_url,
            "token_url": token_url,
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_verifier": verifier,
            "status": "pending",
            "access_token": "",
            "refresh_token": "",
            "token_type": "",
            "expires_in": 0,
            "scope_out": "",
            "err_text": "",
            "last": time.time(),
        }
        with _oauth_lock:
            _oauth_sessions[sid] = sess
        self._send_json({
            "id": sid, "auth_url": full, "redirect_uri": redirect_uri, "scope": scope,
        }, status=201)

    def _handle_oauth2_poll(self, query):
        sid_vals = query.get("id")
        if not sid_vals or not sid_vals[0]:
            self._send_error_json(400, "Missing required query parameter: id")
            return
        sid = sid_vals[0]
        with _oauth_lock:
            sess = _oauth_sessions.get(sid)
            if sess is None:
                self._send_error_json(404, "Unknown OAuth session: " + sid)
                return
            sess["last"] = time.time()
            out = {"id": sid, "status": sess["status"]}
            if sess["status"] == "authorized":
                out["access_token"] = sess["access_token"]
                out["token_type"] = sess["token_type"]
                out["expires_in"] = jenc(sess["expires_in"])
                out["scope"] = sess["scope_out"]
            elif sess["status"] == "error":
                out["error"] = sess["err_text"]
        self._send_json(out)

    def _handle_oauth2_refresh(self, query):
        sid_vals = query.get("id")
        if not sid_vals or not sid_vals[0]:
            self._send_error_json(400, "Missing required query parameter: id")
            return
        sid = sid_vals[0]
        with _oauth_lock:
            sess = _oauth_sessions.get(sid)
            if sess is None:
                self._send_error_json(404, "Unknown OAuth session: " + sid)
                return
            sess["last"] = time.time()
            rt = sess["refresh_token"]
            status = sess["status"]
            client_id = sess["client_id"]
            client_secret = sess["client_secret"]
            token_url = sess["token_url"]
        if not rt or status in ("pending", "exchanging"):
            self._send_error_json(400, "No refresh token for this session; authorize again.")
            return
        form = {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": client_id,
        }
        if client_secret:
            form["client_secret"] = client_secret
        tok, err = _oauth_post_form(token_url, form)
        if err is not None:
            self._send_error_json(400, "Token refresh failed: " + err)
            return
        with _oauth_lock:
            sess["access_token"] = tok["access"]
            sess["token_type"] = tok["token_type"]
            sess["expires_in"] = tok["expires"]
            if tok["scope"]:
                sess["scope_out"] = tok["scope"]
            sess["status"] = "authorized"
            sess["err_text"] = ""
            sess["last"] = time.time()
            access = sess["access_token"]
            ttype = sess["token_type"]
            expires = sess["expires_in"]
            scope_out = sess["scope_out"]
        self._send_json({
            "id": sid, "access_token": access, "token_type": ttype,
            "expires_in": jenc(expires), "scope": scope_out,
        })

    def _handle_oauth2_close(self, query):
        sid_vals = query.get("id")
        if not sid_vals or not sid_vals[0]:
            self._send_error_json(400, "Missing required query parameter: id")
            return
        sid = sid_vals[0]
        with _oauth_lock:
            sess = _oauth_sessions.get(sid)
            if sess is None:
                self._send_error_json(404, "Unknown OAuth session: " + sid)
                return
            if _oauth_sessions.get(sid) is sess:
                del _oauth_sessions[sid]
        self._send_json({"id": sid, "closed": jenc(True)})

    def _handle_oauth2_callback(self, query):
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        err_param = query.get("error", [""])[0]
        err_desc = query.get("error_description", [""])[0]

        with _oauth_lock:
            sess = None
            for s in _oauth_sessions.values():
                if state and s["state"] == state:
                    sess = s
                    break
            if sess is not None:
                sess["last"] = time.time()
        if sess is None:
            self._oauth_write_page(400, "Authorization failed",
                "Unknown or expired authorization request. Start the sign-in again from the app.")
            return
        if err_param:
            msg = "The provider reported: " + err_param
            if err_desc:
                msg += " (" + err_desc + ")"
            with _oauth_lock:
                sess["status"] = "error"
                sess["err_text"] = msg
            self._oauth_write_page(400, "Authorization denied", msg)
            return
        if not code:
            with _oauth_lock:
                sess["status"] = "error"
                sess["err_text"] = "authorization response without a code"
            self._oauth_write_page(400, "Authorization failed",
                "The provider did not return an authorization code.")
            return
        with _oauth_lock:
            already = sess["status"] != "pending"
            if not already:
                sess["status"] = "exchanging"
            client_id = sess["client_id"]
            client_secret = sess["client_secret"]
            token_url = sess["token_url"]
            redirect_uri = sess["redirect_uri"]
            verifier = sess["code_verifier"]
        if already:
            self._oauth_write_page(200, "Already processed",
                "This authorization request was already handled. You can close this window.")
            return
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        if client_secret:
            form["client_secret"] = client_secret
        tok, err = _oauth_post_form(token_url, form)
        if err is not None:
            with _oauth_lock:
                sess["status"] = "error"
                sess["err_text"] = err
            self._oauth_write_page(400, "Token exchange failed", err)
            return
        with _oauth_lock:
            sess["status"] = "authorized"
            sess["access_token"] = tok["access"]
            sess["refresh_token"] = tok["refresh"]
            sess["token_type"] = tok["token_type"]
            sess["expires_in"] = tok["expires"]
            if tok["scope"]:
                sess["scope_out"] = tok["scope"]
            sess["err_text"] = ""
        self._oauth_write_page(200, "Authorization complete",
            "You can close this window and return to the app.")

    def _oauth_write_page(self, status, title, message):
        body = ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<title>%s</title></head>\n<body style=\"font-family:system-ui,sans-serif;padding:2rem;max-width:32rem\">\n"
                "<h1>%s</h1>\n<p>%s</p>\n"
                "<script>setTimeout(function(){try{window.close();}catch(e){}},900);</script>\n"
                "</body></html>\n") % (html.escape(title), html.escape(title), html.escape(message))
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(data)
