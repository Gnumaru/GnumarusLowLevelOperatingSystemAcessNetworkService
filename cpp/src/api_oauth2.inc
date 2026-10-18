// OAuth2 endpoint handlers (included by api.cpp; uses its includes).
namespace {

#include <openssl/sha.h>
#include <openssl/rand.h>
#include <sstream>

static const std::string OAUTH_CALLBACK_PATH = "/api/oauth2/callback";
static const std::string OAUTH_DEFAULT_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
static const std::string OAUTH_DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token";
static const std::string OAUTH_DEFAULT_SCOPE = "https://mail.google.com/";
static const int OAUTH_IDLE_TIMEOUT = 600;
static const int OAUTH_MAX_SESSIONS = 64;
static const int OAUTH_EXCHANGE_TIMEOUT = 20;

struct OAuthSession {
    std::string client_id, client_secret, auth_url, token_url, scope, redirect_uri, state, code_verifier, status, access_token, refresh_token, token_type, scope_out, err_text;
    long long expires_in = 0;
    std::chrono::steady_clock::time_point last;
};

static std::mutex oauth_mu;
static std::map<std::string, OAuthSession> oauth_sessions;
static std::once_flag oauth_sweeper_once;

static std::string b64url_nopad(const unsigned char* data, size_t len) {
    static const char* T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    out.reserve((len + 2) / 3 * 4);
    size_t i = 0;
    while (i + 3 <= len) {
        uint32_t n = (data[i] << 16) | (data[i+1] << 8) | data[i+2];
        out.push_back(T[(n >> 18) & 63]);
        out.push_back(T[(n >> 12) & 63]);
        out.push_back(T[(n >> 6) & 63]);
        out.push_back(T[n & 63]);
        i += 3;
    }
    size_t rem = len - i;
    if (rem == 1) {
        uint32_t n = data[i] << 16;
        out.push_back(T[(n >> 18) & 63]);
        out.push_back(T[(n >> 12) & 63]);
    } else if (rem == 2) {
        uint32_t n = (data[i] << 16) | (data[i+1] << 8);
        out.push_back(T[(n >> 18) & 63]);
        out.push_back(T[(n >> 12) & 63]);
        out.push_back(T[(n >> 6) & 63]);
    }
    return out;
}

static std::string oauth_random(int n) {
    std::vector<unsigned char> buf(n);
    if (RAND_bytes(buf.data(), n) != 1) {
        return std::to_string(std::chrono::system_clock::now().time_since_epoch().count());
    }
    return b64url_nopad(buf.data(), buf.size());
}

static std::vector<unsigned char> sha256_bytes(const std::string& data) {
    std::vector<unsigned char> out(SHA256_DIGEST_LENGTH);
    SHA256((const unsigned char*)data.data(), data.size(), out.data());
    return out;
}

static std::string sha256_b64url(const std::string& data) {
    auto d = sha256_bytes(data);
    return b64url_nopad(d.data(), d.size());
}

static bool oauth_valid_url(const std::string& raw) {
    size_t pos = raw.find("://");
    if (pos == std::string::npos) return false;
    std::string scheme = raw.substr(0, pos);
    if (scheme != "http" && scheme != "https") return false;
    std::string rest = raw.substr(pos + 3);
    size_t slash = rest.find('/');
    std::string hostport = (slash == std::string::npos) ? rest : rest.substr(0, slash);
    size_t colon = hostport.find(':');
    std::string host = (colon == std::string::npos) ? hostport : hostport.substr(0, colon);
    return !host.empty();
}

static std::string url_encode(const std::string& s) {
    std::string out;
    for (unsigned char b : s) {
        if ((b >= 'A' && b <= 'Z') || (b >= 'a' && b <= 'z') || (b >= '0' && b <= '9') ||
            b == '-' || b == '_' || b == '.' || b == '~') {
            out.push_back((char)b);
        } else {
            char buf[8];
            std::snprintf(buf, sizeof(buf), "%%%02X", b);
            out += buf;
        }
    }
    return out;
}

static void oauth_sweep() {
    auto now = std::chrono::steady_clock::now();
    std::lock_guard<std::mutex> lk(oauth_mu);
    for (auto it = oauth_sessions.begin(); it != oauth_sessions.end();) {
        if (std::chrono::duration_cast<std::chrono::seconds>(now - it->second.last).count() > OAUTH_IDLE_TIMEOUT) {
            it = oauth_sessions.erase(it);
        } else {
            ++it;
        }
    }
}

static void oauth_ensure_sweeper() {
    std::call_once(oauth_sweeper_once, []() {
        std::thread([]() {
            for (;;) {
                std::this_thread::sleep_for(std::chrono::seconds(60));
                oauth_sweep();
            }
        }).detach();
    });
}

struct OAuthToken { std::string access, refresh, token_type, scope; long long expires = 0; };

static bool parse_url(const std::string& raw, std::string& scheme, std::string& host, int& port, std::string& path) {
    size_t pos = raw.find("://");
    if (pos == std::string::npos) return false;
    scheme = raw.substr(0, pos);
    for (auto& c : scheme) c = std::tolower((unsigned char)c);
    if (scheme != "http" && scheme != "https") return false;
    std::string rest = raw.substr(pos + 3);
    size_t slash = rest.find('/');
    std::string hostport;
    if (slash == std::string::npos) { hostport = rest; path = "/"; }
    else { hostport = rest.substr(0, slash); path = rest.substr(slash); if (path.empty()) path = "/"; }
    size_t colon = hostport.find(':');
    if (colon == std::string::npos) {
        host = hostport;
        port = (scheme == "https") ? 443 : 80;
    } else {
        host = hostport.substr(0, colon);
        try { port = std::stoi(hostport.substr(colon + 1)); } catch (...) { return false; }
    }
    if (host.empty()) return false;
    return true;
}

static std::string oauth_http_post_form(const std::string& token_url, const std::vector<std::pair<std::string,std::string>>& form) {
    std::string scheme, host, path;
    int port = 0;
    if (!parse_url(token_url, scheme, host, port, path)) return "ERR:bad url";

    std::string body;
    for (size_t i = 0; i < form.size(); ++i) {
        if (i) body += "&";
        body += url_encode(form[i].first) + "=" + url_encode(form[i].second);
    }

    std::string errmsg;
    int fd = sockws::sock_connect(host, port, OAUTH_EXCHANGE_TIMEOUT, errmsg);
    if (fd < 0) return "ERR:" + errmsg;

    SSL_CTX* ctx = nullptr;
    SSL* ssl = nullptr;
    if (scheme == "https") {
        ctx = sockws::sock_tls_context("", errmsg);
        if (!ctx) { close(fd); return "ERR:" + errmsg; }
        ssl = sockws::sock_tls_handshake(fd, host, ctx, OAUTH_EXCHANGE_TIMEOUT, errmsg);
        if (!ssl) { if (ctx) SSL_CTX_free(ctx); close(fd); return "ERR:" + errmsg; }
    }

    std::string req = "POST " + path + " HTTP/1.1\r\nHost: " + host +
        "\r\nContent-Type: application/x-www-form-urlencoded\r\nAccept: application/json\r\n" +
        "Content-Length: " + std::to_string(body.size()) + "\r\nConnection: close\r\n\r\n" + body;

    if (!sockws::sock_write_all(fd, ssl, req.data(), req.size(), errmsg)) {
        if (ssl) SSL_free(ssl);
        if (ctx) SSL_CTX_free(ctx);
        close(fd);
        return "ERR:" + errmsg;
    }

    std::string resp;
    char buf[8192];
    for (;;) {
        ssize_t n = sockws::sock_read(fd, ssl, buf, sizeof(buf));
        if (n > 0) resp.append(buf, n);
        else if (n == 0) break;
        else {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == ETIMEDOUT) break;
            break;
        }
    }
    if (ssl) SSL_free(ssl);
    if (ctx) SSL_CTX_free(ctx);
    close(fd);

    size_t sep_pos = resp.find("\r\n\r\n");
    if (sep_pos == std::string::npos) return "ERR:bad response";
    std::string head = resp.substr(0, sep_pos);
    std::string rbody = resp.substr(sep_pos + 4);
    std::istringstream hs(head);
    std::string httpver;
    int code = 0;
    hs >> httpver >> code;
    json payload;
    try { payload = rbody.empty() ? json::object() : json::parse(rbody); } catch (...) { payload = json::object(); }
    auto get = [&](const std::string& k) -> std::string {
        auto it = payload.find(k);
        if (it != payload.end() && it->is_string()) return it->get<std::string>();
        return "";
    };
    if (code != 200) {
        std::string msg = get("error_description");
        if (msg.empty()) msg = get("error");
        if (msg.empty()) msg = "HTTP " + std::to_string(code);
        return "ERR:" + msg;
    }
    std::string access = get("access_token");
    if (access.empty()) return "ERR:token response without an access_token";
    std::string token_type = get("token_type");
    if (token_type.empty()) token_type = "Bearer";
    long long expires = 0;
    auto ite = payload.find("expires_in");
    if (ite != payload.end()) {
        if (ite->is_number_integer()) expires = ite->get<long long>();
        else if (ite->is_number_float()) expires = (long long)ite->get<double>();
        else if (ite->is_string()) { try { expires = std::stoll(ite->get<std::string>()); } catch (...) {} }
    }
    if (expires == 0) expires = 3600;
    json tok;
    tok["access"] = access;
    tok["refresh"] = get("refresh_token");
    tok["token_type"] = token_type;
    tok["expires"] = expires;
    tok["scope"] = get("scope");
    return "OK:" + tok.dump();
}

static std::string html_escape(const std::string& s) {
    std::string out;
    for (char c : s) {
        switch (c) {
            case '&': out += "&amp;"; break;
            case '<': out += "&lt;"; break;
            case '>': out += "&gt;"; break;
            case '"': out += "&quot;"; break;
            default: out.push_back(c);
        }
    }
    return out;
}

static HttpResponse oauth_write_page(int status, const std::string& title, const std::string& message) {
    std::string body = "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>" + html_escape(title) +
        "</title></head>\n<body style=\"font-family:system-ui,sans-serif;padding:2rem;max-width:32rem\">\n<h1>" + html_escape(title) +
        "</h1>\n<p>" + html_escape(message) + "</p>\n<script>setTimeout(function(){try{window.close();}catch(e){}},900);</script>\n</body></html>\n";
    HttpResponse res = send_raw(status, body, "text/html; charset=utf-8");
    return res;
}

static std::string first_query(const HttpRequest& req, const std::string& key) {
    auto it = req.query.find(key);
    if (it == req.query.end()) return "";
    return it->second;
}

HttpResponse handle_oauth2_start(const HttpRequest& req, const ApiState& st) {
    (void)st;
    oauth_ensure_sweeper();
    auto ctit = req.headers.find("content-type");
    std::string ct = ctit == req.headers.end() ? "" : ctit->second;
    std::string lower = ct;
    for (auto& c : lower) c = std::tolower((unsigned char)c);
    if (lower.find("application/json") == std::string::npos)
        return send_json(400, jerr(400, "Content-Type must be application/json with body { client_id, client_secret?, auth_url?, token_url?, scope? }."));

    json obj;
    try { obj = req.body.empty() ? json::object() : json::parse(req.body); } catch (...) {
        return send_json(400, jerr(400, "Invalid JSON body."));
    }
    if (!obj.is_object()) return send_json(400, jerr(400, "JSON body must be an object."));

    auto get_str = [&](const std::string& key, std::string& out, HttpResponse& errres) -> bool {
        auto it = obj.find(key);
        if (it == obj.end() || it->is_null()) { out.clear(); return true; }
        if (!it->is_string()) { errres = send_json(400, jerr(400, "Field '" + key + "' must be a string.")); return false; }
        out = it->get<std::string>();
        return true;
    };

    std::string client_id, client_secret, auth_url, token_url, scope;
    HttpResponse tmp;
    if (!get_str("client_id", client_id, tmp)) return tmp;
    auto trim = [](std::string s){ size_t a=s.find_first_not_of(" \t\n\r"); if(a==std::string::npos) return std::string(); size_t b=s.find_last_not_of(" \t\n\r"); return s.substr(a,b-a+1); };
    client_id = trim(client_id);
    if (client_id.empty()) return send_json(400, jerr(400, "Missing or empty required field: client_id."));
    if (!get_str("client_secret", client_secret, tmp)) return tmp;
    if (!get_str("auth_url", auth_url, tmp)) return tmp;
    if (!get_str("token_url", token_url, tmp)) return tmp;
    if (!get_str("scope", scope, tmp)) return tmp;
    auth_url = trim(auth_url);
    token_url = trim(token_url);
    scope = trim(scope);
    if (auth_url.empty()) auth_url = OAUTH_DEFAULT_AUTH_URL;
    if (token_url.empty()) token_url = OAUTH_DEFAULT_TOKEN_URL;
    if (scope.empty()) scope = OAUTH_DEFAULT_SCOPE;
    if (!oauth_valid_url(auth_url)) return send_json(400, jerr(400, "Field 'auth_url' must be an absolute http(s) URL."));
    if (!oauth_valid_url(token_url)) return send_json(400, jerr(400, "Field 'token_url' must be an absolute http(s) URL."));

    std::string host = "";
    auto hit = req.headers.find("host");
    if (hit != req.headers.end()) host = hit->second;
    std::string redirect_uri = "http://" + host + OAUTH_CALLBACK_PATH;
    std::string state = oauth_random(24);
    std::string verifier = oauth_random(32);
    std::string challenge = sha256_b64url(verifier);

    std::vector<std::pair<std::string,std::string>> vals;
    vals.push_back({"client_id", client_id});
    vals.push_back({"redirect_uri", redirect_uri});
    vals.push_back({"response_type", "code"});
    vals.push_back({"scope", scope});
    vals.push_back({"state", state});
    vals.push_back({"code_challenge", challenge});
    vals.push_back({"code_challenge_method", "S256"});
    vals.push_back({"access_type", "offline"});
    vals.push_back({"prompt", "consent"});

    std::string sep = auth_url.find('?') != std::string::npos ? "&" : "?";
    std::string qstr;
    for (size_t i = 0; i < vals.size(); ++i) {
        if (i) qstr += "&";
        qstr += url_encode(vals[i].first) + "=" + url_encode(vals[i].second);
    }
    std::string full = auth_url + sep + qstr;

    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        if (oauth_sessions.size() >= OAUTH_MAX_SESSIONS)
            return send_json(400, jerr(400, "Too many open OAuth sessions (" + std::to_string(OAUTH_MAX_SESSIONS) + ")."));
    }

    std::string id = oauth_random(16);
    OAuthSession sess;
    sess.client_id = client_id;
    sess.client_secret = client_secret;
    sess.auth_url = auth_url;
    sess.token_url = token_url;
    sess.scope = scope;
    sess.redirect_uri = redirect_uri;
    sess.state = state;
    sess.code_verifier = verifier;
    sess.status = "pending";
    sess.expires_in = 0;
    sess.last = std::chrono::steady_clock::now();
    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        oauth_sessions[id] = sess;
    }

    json o;
    o["id"] = id;
    o["auth_url"] = full;
    o["redirect_uri"] = redirect_uri;
    o["scope"] = scope;
    return send_json(201, o);
}

HttpResponse handle_oauth2_poll(const HttpRequest& req, const ApiState& st) {
    (void)st;
    std::string id = first_query(req, "id");
    if (id.empty()) return send_json(400, jerr(400, "Missing required query parameter: id"));
    std::lock_guard<std::mutex> lk(oauth_mu);
    auto it = oauth_sessions.find(id);
    if (it == oauth_sessions.end()) return send_json(404, jerr(404, "Unknown OAuth session: " + id));
    it->second.last = std::chrono::steady_clock::now();
    json out;
    out["id"] = id;
    out["status"] = it->second.status;
    if (it->second.status == "authorized") {
        out["access_token"] = it->second.access_token;
        out["token_type"] = it->second.token_type;
        out["expires_in"] = jenc_int(it->second.expires_in);
        out["scope"] = it->second.scope_out;
    } else if (it->second.status == "error") {
        out["error"] = it->second.err_text;
    }
    return send_json(200, out);
}

HttpResponse handle_oauth2_refresh(const HttpRequest& req, const ApiState& st) {
    (void)st;
    std::string id = first_query(req, "id");
    if (id.empty()) return send_json(400, jerr(400, "Missing required query parameter: id"));
    std::string rt, status, client_id, client_secret, token_url;
    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        auto it = oauth_sessions.find(id);
        if (it == oauth_sessions.end()) return send_json(404, jerr(404, "Unknown OAuth session: " + id));
        it->second.last = std::chrono::steady_clock::now();
        rt = it->second.refresh_token;
        status = it->second.status;
        client_id = it->second.client_id;
        client_secret = it->second.client_secret;
        token_url = it->second.token_url;
    }
    if (rt.empty() || status == "pending" || status == "exchanging")
        return send_json(400, jerr(400, "No refresh token for this session; authorize again."));

    std::vector<std::pair<std::string,std::string>> form;
    form.push_back({"grant_type", "refresh_token"});
    form.push_back({"refresh_token", rt});
    form.push_back({"client_id", client_id});
    if (!client_secret.empty()) form.push_back({"client_secret", client_secret});

    std::string res = oauth_http_post_form(token_url, form);
    if (res.rfind("ERR:", 0) == 0) {
        return send_json(400, jerr(400, "Token refresh failed: " + res.substr(4)));
    }
    std::string payload = res.substr(3);
    json tok = json::parse(payload);
    std::string access = tok.value("access", "");
    std::string ttype = tok.value("token_type", "");
    if (ttype.empty()) ttype = "Bearer";
    long long expires = tok.value("expires", 3600LL);
    std::string scope = tok.value("scope", "");

    std::string access_out, ttype_out, scope_out;
    long long expires_out = 0;
    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        auto it = oauth_sessions.find(id);
        it->second.access_token = access;
        it->second.token_type = ttype;
        it->second.expires_in = expires;
        if (!scope.empty()) it->second.scope_out = scope;
        it->second.status = "authorized";
        it->second.err_text.clear();
        it->second.last = std::chrono::steady_clock::now();
        access_out = it->second.access_token;
        ttype_out = it->second.token_type;
        expires_out = it->second.expires_in;
        scope_out = it->second.scope_out;
    }
    json o;
    o["id"] = id;
    o["access_token"] = access_out;
    o["token_type"] = ttype_out;
    o["expires_in"] = jenc_int(expires_out);
    o["scope"] = scope_out;
    return send_json(200, o);
}

HttpResponse handle_oauth2_close(const HttpRequest& req, const ApiState& st) {
    (void)st;
    std::string id = first_query(req, "id");
    if (id.empty()) return send_json(400, jerr(400, "Missing required query parameter: id"));
    std::lock_guard<std::mutex> lk(oauth_mu);
    auto it = oauth_sessions.find(id);
    if (it == oauth_sessions.end()) return send_json(404, jerr(404, "Unknown OAuth session: " + id));
    oauth_sessions.erase(it);
    json o;
    o["id"] = id;
    o["closed"] = "b:true";
    return send_json(200, o);
}

HttpResponse handle_oauth2_callback(const HttpRequest& req, const ApiState& st) {
    (void)st;
    std::string state = first_query(req, "state");
    std::string code = first_query(req, "code");
    std::string err_param = first_query(req, "error");
    std::string err_desc = first_query(req, "error_description");

    std::string sid;
    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        for (auto& kv : oauth_sessions) {
            if (!state.empty() && kv.second.state == state) { sid = kv.first; break; }
        }
    }
    if (sid.empty()) {
        return oauth_write_page(400, "Authorization failed", "Unknown or expired authorization request. Start the sign-in again from the app.");
    }
    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        auto it = oauth_sessions.find(sid);
        if (it != oauth_sessions.end()) it->second.last = std::chrono::steady_clock::now();
    }

    if (!err_param.empty()) {
        std::string msg = "The provider reported: " + err_param;
        if (!err_desc.empty()) msg += " (" + err_desc + ")";
        {
            std::lock_guard<std::mutex> lk(oauth_mu);
            auto it = oauth_sessions.find(sid);
            if (it != oauth_sessions.end()) { it->second.status = "error"; it->second.err_text = msg; }
        }
        return oauth_write_page(400, "Authorization denied", msg);
    }
    if (code.empty()) {
        {
            std::lock_guard<std::mutex> lk(oauth_mu);
            auto it = oauth_sessions.find(sid);
            if (it != oauth_sessions.end()) { it->second.status = "error"; it->second.err_text = "authorization response without a code"; }
        }
        return oauth_write_page(400, "Authorization failed", "The provider did not return an authorization code.");
    }

    bool already = false;
    std::string client_id, client_secret, token_url, redirect_uri, verifier;
    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        auto it = oauth_sessions.find(sid);
        already = it->second.status != "pending";
        if (!already) it->second.status = "exchanging";
        client_id = it->second.client_id;
        client_secret = it->second.client_secret;
        token_url = it->second.token_url;
        redirect_uri = it->second.redirect_uri;
        verifier = it->second.code_verifier;
    }
    if (already) {
        return oauth_write_page(200, "Already processed", "This authorization request was already handled. You can close this window.");
    }

    std::vector<std::pair<std::string,std::string>> form;
    form.push_back({"grant_type", "authorization_code"});
    form.push_back({"code", code});
    form.push_back({"client_id", client_id});
    form.push_back({"redirect_uri", redirect_uri});
    form.push_back({"code_verifier", verifier});
    if (!client_secret.empty()) form.push_back({"client_secret", client_secret});

    std::string res = oauth_http_post_form(token_url, form);
    if (res.rfind("ERR:", 0) == 0) {
        std::string e = res.substr(4);
        {
            std::lock_guard<std::mutex> lk(oauth_mu);
            auto it = oauth_sessions.find(sid);
            if (it != oauth_sessions.end()) { it->second.status = "error"; it->second.err_text = e; }
        }
        return oauth_write_page(400, "Token exchange failed", e);
    }
    std::string payload = res.substr(3);
    json tok = json::parse(payload);
    std::string access = tok.value("access", "");
    std::string refresh = tok.value("refresh", "");
    std::string ttype = tok.value("token_type", "");
    if (ttype.empty()) ttype = "Bearer";
    long long expires = tok.value("expires", 3600LL);
    std::string scope = tok.value("scope", "");

    {
        std::lock_guard<std::mutex> lk(oauth_mu);
        auto it = oauth_sessions.find(sid);
        it->second.status = "authorized";
        it->second.access_token = access;
        it->second.refresh_token = refresh;
        it->second.token_type = ttype;
        it->second.expires_in = expires;
        if (!scope.empty()) it->second.scope_out = scope;
        it->second.err_text.clear();
    }
    return oauth_write_page(200, "Authorization complete", "You can close this window and return to the app.");
}

}  // namespace
