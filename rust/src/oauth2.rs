use crate::api::{err, jstr, send_json, send_raw};
use crate::http::Response;
use crate::tags::jenc_int;
use openssl::hash::{hash, MessageDigest};
use openssl::rand::rand_bytes;
use openssl::ssl::{SslConnector, SslMethod, SslVersion};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::{Mutex, Once, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const OAUTH_CALLBACK_PATH: &str = "/api/oauth2/callback";
const OAUTH_DEFAULT_AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const OAUTH_DEFAULT_TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const OAUTH_DEFAULT_SCOPE: &str = "https://mail.google.com/";
const OAUTH_IDLE_TIMEOUT: u64 = 600;
const OAUTH_MAX_SESSIONS: usize = 64;
const OAUTH_EXCHANGE_TIMEOUT: u64 = 20;

#[derive(Clone)]
struct OAuthSession {
    client_id: String,
    client_secret: String,
    auth_url: String,
    token_url: String,
    scope: String,
    redirect_uri: String,
    state: String,
    code_verifier: String,
    status: String,
    access_token: String,
    refresh_token: String,
    token_type: String,
    expires_in: i64,
    scope_out: String,
    err_text: String,
    last: Instant,
}

static SESSIONS: OnceLock<Mutex<HashMap<String, OAuthSession>>> = OnceLock::new();

fn sessions() -> &'static Mutex<HashMap<String, OAuthSession>> {
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}
static SWEEPER: Once = Once::new();

pub fn ensure_sweeper() {
    SWEEPER.call_once(|| {
        std::thread::spawn(|| loop {
            std::thread::sleep(Duration::from_secs(60));
            sweep();
        });
    });
}

fn sweep() {
    let now = Instant::now();
    let mut g = sessions().lock().unwrap();
    g.retain(|_, s| now.duration_since(s.last) <= Duration::from_secs(OAUTH_IDLE_TIMEOUT));
}

fn oauth_random(n: usize) -> String {
    let mut buf = vec![0u8; n];
    if rand_bytes(&mut buf).is_ok() {
        b64url_nopad(&buf)
    } else {
        format!("{}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos())
    }
}

fn b64url_nopad(data: &[u8]) -> String {
    const B64: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let mut n: u32 = 0;
        for (i, &b) in chunk.iter().enumerate() {
            n |= (b as u32) << (16 - 8 * i);
        }
        let chars = if chunk.len() == 3 { 4 } else if chunk.len() == 2 { 3 } else { 2 };
        for i in 0..chars {
            out.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

fn sha256_bytes(data: &[u8]) -> Vec<u8> {
    hash(MessageDigest::sha256(), data).map(|b| b.to_vec()).unwrap_or_default()
}

fn oauth_valid_url(raw: &str) -> bool {
    let (scheme, rest) = match raw.split_once("://") {
        Some(x) => x,
        None => return false,
    };
    if scheme != "http" && scheme != "https" { return false; }
    let hostpart = rest.split('/').next().unwrap_or(rest);
    let host = hostpart.split(':').next().unwrap_or(hostpart);
    !host.is_empty()
}

fn url_encode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

fn first_query(q: &HashMap<String, String>, key: &str) -> String {
    q.get(key).cloned().unwrap_or_default()
}

struct OAuthToken {
    access: String,
    refresh: String,
    token_type: String,
    expires: i64,
    scope: String,
}

fn form_encode(pairs: &[(String, String)]) -> String {
    let mut parts = Vec::new();
    for (k, v) in pairs {
        parts.push(format!("{}={}", url_encode(k), url_encode(v)));
    }
    parts.join("&")
}

fn http_post_form(token_url: &str, form_pairs: &[(String, String)]) -> Result<OAuthToken, String> {
    use std::net::ToSocketAddrs;
    let (scheme, rest) = token_url.split_once("://").ok_or_else(|| "bad url".to_string())?;
    let scheme = scheme.to_lowercase();
    if scheme != "http" && scheme != "https" { return Err("bad scheme".to_string()); }
    let (hostport, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let path = if path.is_empty() { "/" } else { path };
    let (host, port) = if let Some((h, p)) = hostport.split_once(':') {
        let port: u16 = p.parse().map_err(|_| "bad port".to_string())?;
        (h.to_string(), port)
    } else {
        let default = if scheme == "https" { 443 } else { 80 };
        (hostport.to_string(), default)
    };

    let addr = (host.as_str(), port).to_socket_addrs().map_err(|e| e.to_string())?.next().ok_or_else(|| "dns failed".to_string())?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(OAUTH_EXCHANGE_TIMEOUT)).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(OAUTH_EXCHANGE_TIMEOUT))).map_err(|e| e.to_string())?;
    stream.set_write_timeout(Some(Duration::from_secs(OAUTH_EXCHANGE_TIMEOUT))).map_err(|e| e.to_string())?;

    let body = form_encode(form_pairs);
    let req = format!(
        "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/x-www-form-urlencoded\r\nAccept: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        path, host, body.len(), body
    );

    let resp = if scheme == "https" {
        let mut builder = SslConnector::builder(SslMethod::tls()).map_err(|e| e.to_string())?;
        builder.set_min_proto_version(Some(SslVersion::TLS1_2)).ok();
        let connector = builder.build();
        let mut ssl = connector.connect(&host, stream).map_err(|e| e.to_string())?;
        ssl.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
        let mut resp = Vec::new();
        let mut buf = [0u8; 8192];
        loop {
            match ssl.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => resp.extend_from_slice(&buf[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut => break,
                Err(e) => return Err(e.to_string()),
            }
        }
        resp
    } else {
        stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
        let mut resp = Vec::new();
        let mut buf = [0u8; 8192];
        loop {
            match stream.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => resp.extend_from_slice(&buf[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut => break,
                Err(e) => return Err(e.to_string()),
            }
        }
        resp
    };

    parse_http_response(&resp)
}

fn parse_http_response(resp: &[u8]) -> Result<OAuthToken, String> {
    let text = String::from_utf8_lossy(resp);
    let (head, body) = match text.find("\r\n\r\n") {
        Some(i) => (&text[..i], &text[i + 4..]),
        None => return Err("bad response".to_string()),
    };
    let status_line = head.lines().next().unwrap_or("");
    let mut parts = status_line.split_whitespace();
    let _http = parts.next();
    let code: u16 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let payload: serde_json::Value = serde_json::from_str(body).unwrap_or(json!({}));
    let get = |k: &str| -> String {
        payload.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
    };
    if code != 200 {
        let d = get("error_description");
        let msg = if d.is_empty() { get("error") } else { d };
        let msg = if msg.is_empty() { format!("HTTP {}", code) } else { msg };
        return Err(msg);
    }
    let access = get("access_token");
    if access.is_empty() { return Err("token response without an access_token".to_string()); }
    let mut token_type = get("token_type");
    if token_type.is_empty() { token_type = "Bearer".to_string(); }
    let expires = payload.get("expires_in").and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))).unwrap_or(0);
    let expires = if expires == 0 { 3600 } else { expires };
    Ok(OAuthToken { access, refresh: get("refresh_token"), token_type, expires, scope: get("scope") })
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;")
}

fn oauth_write_page(status: u16, title: &str, message: &str) -> Response {
    let body = format!(
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>{}</title></head>\n<body style=\"font-family:system-ui,sans-serif;padding:2rem;max-width:32rem\">\n<h1>{}</h1>\n<p>{}</p>\n<script>setTimeout(function(){{try{{window.close();}}catch(e){{}}}},900);</script>\n</body></html>\n",
        html_escape(title), html_escape(title), html_escape(message)
    );
    send_raw(status, body.into_bytes(), "text/html; charset=utf-8")
}

pub fn handle_start(req: &crate::http::Request) -> Response {
    ensure_sweeper();
    let ct = req.headers.get("content-type").map(|s| s.as_str()).unwrap_or("");
    if !ct.contains("application/json") {
        return err(400, "Content-Type must be application/json with body { client_id, client_secret?, auth_url?, token_url?, scope? }.", None);
    }
    let obj = match crate::api::req_json(req) { Ok(m) => m, Err(r) => return r };
    fn get_str(obj: &serde_json::Map<String, Value>, key: &str) -> Result<String, Response> {
        match obj.get(key) {
            None | Some(Value::Null) => Ok(String::new()),
            Some(Value::String(s)) => Ok(s.clone()),
            Some(_) => Err(err(400, &format!("Field '{}' must be a string.", key), None)),
        }
    }
    let client_id = match get_str(&obj, "client_id") { Ok(s) => s.trim().to_string(), Err(r) => return r };
    if client_id.is_empty() { return err(400, "Missing or empty required field: client_id.", None); }
    let client_secret = match get_str(&obj, "client_secret") { Ok(s) => s, Err(r) => return r };
    let mut auth_url = match get_str(&obj, "auth_url") { Ok(s) => s.trim().to_string(), Err(r) => return r };
    let mut token_url = match get_str(&obj, "token_url") { Ok(s) => s.trim().to_string(), Err(r) => return r };
    let mut scope = match get_str(&obj, "scope") { Ok(s) => s.trim().to_string(), Err(r) => return r };
    if auth_url.is_empty() { auth_url = OAUTH_DEFAULT_AUTH_URL.to_string(); }
    if token_url.is_empty() { token_url = OAUTH_DEFAULT_TOKEN_URL.to_string(); }
    if scope.is_empty() { scope = OAUTH_DEFAULT_SCOPE.to_string(); }
    if !oauth_valid_url(&auth_url) { return err(400, "Field 'auth_url' must be an absolute http(s) URL.", None); }
    if !oauth_valid_url(&token_url) { return err(400, "Field 'token_url' must be an absolute http(s) URL.", None); }

    let host = req.headers.get("host").cloned().unwrap_or_default();
    let redirect_uri = format!("http://{}{}", host, OAUTH_CALLBACK_PATH);
    let state = oauth_random(24);
    let verifier = oauth_random(32);
    let sum = sha256_bytes(verifier.as_bytes());
    let challenge = b64url_nopad(&sum);

    let mut vals: Vec<(&str, String)> = Vec::new();
    vals.push(("client_id", client_id.clone()));
    vals.push(("redirect_uri", redirect_uri.clone()));
    vals.push(("response_type", "code".to_string()));
    vals.push(("scope", scope.clone()));
    vals.push(("state", state.clone()));
    vals.push(("code_challenge", challenge));
    vals.push(("code_challenge_method", "S256".to_string()));
    vals.push(("access_type", "offline".to_string()));
    vals.push(("prompt", "consent".to_string()));

    let sep = if auth_url.contains('?') { "&" } else { "?" };
    let mut qparts: Vec<String> = Vec::new();
    for (k, v) in &vals { qparts.push(format!("{}={}", url_encode(k), url_encode(v))); }
    let full = format!("{}{}{}", auth_url, sep, qparts.join("&"));

    {
        let g = sessions().lock().unwrap();
        if g.len() >= OAUTH_MAX_SESSIONS {
            return err(400, &format!("Too many open OAuth sessions ({}).", OAUTH_MAX_SESSIONS), None);
        }
    }

    let id = oauth_random(16);
    let sess = OAuthSession {
        client_id, client_secret, auth_url, token_url, scope: scope.clone(), redirect_uri: redirect_uri.clone(),
        state: state.clone(), code_verifier: verifier, status: "pending".to_string(),
        access_token: String::new(), refresh_token: String::new(), token_type: String::new(), expires_in: 0,
        scope_out: String::new(), err_text: String::new(), last: Instant::now(),
    };
    sessions().lock().unwrap().insert(id.clone(), sess);

    let mut o = serde_json::Map::new();
    o.insert("id".to_string(), jstr(&id));
    o.insert("auth_url".to_string(), jstr(&full));
    o.insert("redirect_uri".to_string(), jstr(&redirect_uri));
    o.insert("scope".to_string(), jstr(&scope));
    send_json(201, &Value::Object(o).to_string())
}

pub fn handle_poll(req: &crate::http::Request) -> Response {
    let id = first_query(&req.query, "id");
    if id.is_empty() { return err(400, "Missing required query parameter: id", None); }
    let mut g = sessions().lock().unwrap();
    let sess = match g.get_mut(&id) { Some(s) => s, None => return err(404, &format!("Unknown OAuth session: {}", id), None) };
    sess.last = Instant::now();
    let mut out = serde_json::Map::new();
    out.insert("id".to_string(), jstr(&id));
    out.insert("status".to_string(), jstr(&sess.status));
    if sess.status == "authorized" {
        out.insert("access_token".to_string(), jstr(&sess.access_token));
        out.insert("token_type".to_string(), jstr(&sess.token_type));
        out.insert("expires_in".to_string(), jstr(&jenc_int(sess.expires_in)));
        out.insert("scope".to_string(), jstr(&sess.scope_out));
    } else if sess.status == "error" {
        out.insert("error".to_string(), jstr(&sess.err_text));
    }
    send_json(200, &Value::Object(out).to_string())
}

pub fn handle_refresh(req: &crate::http::Request) -> Response {
    let id = first_query(&req.query, "id");
    if id.is_empty() { return err(400, "Missing required query parameter: id", None); }
    let (rt, status, client_id, client_secret, token_url) = {
        let mut g = sessions().lock().unwrap();
        let sess = match g.get_mut(&id) { Some(s) => s, None => return err(404, &format!("Unknown OAuth session: {}", id), None) };
        sess.last = Instant::now();
        (sess.refresh_token.clone(), sess.status.clone(), sess.client_id.clone(), sess.client_secret.clone(), sess.token_url.clone())
    };
    if rt.is_empty() || status == "pending" || status == "exchanging" {
        return err(400, "No refresh token for this session; authorize again.", None);
    }
    let mut form = vec![
        ("grant_type".to_string(), "refresh_token".to_string()),
        ("refresh_token".to_string(), rt.clone()),
        ("client_id".to_string(), client_id.clone()),
    ];
    if !client_secret.is_empty() { form.push(("client_secret".to_string(), client_secret.clone())); }
    let tok = match http_post_form(&token_url, &form) { Ok(t) => t, Err(e) => return err(400, &format!("Token refresh failed: {}", e), None) };
    let (access, ttype, expires, scope_out) = {
        let mut g = sessions().lock().unwrap();
        let sess = g.get_mut(&id).unwrap();
        sess.access_token = tok.access.clone();
        sess.token_type = tok.token_type.clone();
        sess.expires_in = tok.expires;
        if !tok.scope.is_empty() { sess.scope_out = tok.scope.clone(); }
        sess.status = "authorized".to_string();
        sess.err_text.clear();
        sess.last = Instant::now();
        (sess.access_token.clone(), sess.token_type.clone(), sess.expires_in, sess.scope_out.clone())
    };
    let mut o = serde_json::Map::new();
    o.insert("id".to_string(), jstr(&id));
    o.insert("access_token".to_string(), jstr(&access));
    o.insert("token_type".to_string(), jstr(&ttype));
    o.insert("expires_in".to_string(), jstr(&jenc_int(expires)));
    o.insert("scope".to_string(), jstr(&scope_out));
    send_json(200, &Value::Object(o).to_string())
}

pub fn handle_close(req: &crate::http::Request) -> Response {
    let id = first_query(&req.query, "id");
    if id.is_empty() { return err(400, "Missing required query parameter: id", None); }
    let mut g = sessions().lock().unwrap();
    if !g.contains_key(&id) { return err(404, &format!("Unknown OAuth session: {}", id), None); }
    g.remove(&id);
    let mut o = serde_json::Map::new();
    o.insert("id".to_string(), jstr(&id));
    o.insert("closed".to_string(), jstr("b:true"));
    send_json(200, &Value::Object(o).to_string())
}

pub fn handle_callback(req: &crate::http::Request) -> Response {
    let state = first_query(&req.query, "state");
    let code = first_query(&req.query, "code");
    let err_param = first_query(&req.query, "error");
    let err_desc = first_query(&req.query, "error_description");

    let sid = {
        let g = sessions().lock().unwrap();
        let mut found: Option<String> = None;
        for (s, sess) in g.iter() {
            if !state.is_empty() && sess.state == state { found = Some(s.clone()); break; }
        }
        found
    };
    let sid = match sid { Some(s) => s, None => return oauth_write_page(400, "Authorization failed", "Unknown or expired authorization request. Start the sign-in again from the app.") };
    { let mut g = sessions().lock().unwrap(); if let Some(s) = g.get_mut(&sid) { s.last = Instant::now(); } }

    if !err_param.is_empty() {
        let msg = if err_desc.is_empty() { format!("The provider reported: {}", err_param) } else { format!("The provider reported: {} ({})", err_param, err_desc) };
        { let mut g = sessions().lock().unwrap(); if let Some(s) = g.get_mut(&sid) { s.status = "error".to_string(); s.err_text = msg.clone(); } }
        return oauth_write_page(400, "Authorization denied", &msg);
    }
    if code.is_empty() {
        { let mut g = sessions().lock().unwrap(); if let Some(s) = g.get_mut(&sid) { s.status = "error".to_string(); s.err_text = "authorization response without a code".to_string(); } }
        return oauth_write_page(400, "Authorization failed", "The provider did not return an authorization code.");
    }

    let (already, client_id, client_secret, token_url, redirect_uri, verifier) = {
        let mut g = sessions().lock().unwrap();
        let s = g.get_mut(&sid).unwrap();
        let already = s.status != "pending";
        if !already { s.status = "exchanging".to_string(); }
        (already, s.client_id.clone(), s.client_secret.clone(), s.token_url.clone(), s.redirect_uri.clone(), s.code_verifier.clone())
    };
    if already { return oauth_write_page(200, "Already processed", "This authorization request was already handled. You can close this window."); }

    let mut form = vec![
        ("grant_type".to_string(), "authorization_code".to_string()),
        ("code".to_string(), code.clone()),
        ("client_id".to_string(), client_id.clone()),
        ("redirect_uri".to_string(), redirect_uri.clone()),
        ("code_verifier".to_string(), verifier.clone()),
    ];
    if !client_secret.is_empty() { form.push(("client_secret".to_string(), client_secret.clone())); }
    match http_post_form(&token_url, &form) {
        Ok(tok) => {
            let mut g = sessions().lock().unwrap();
            let s = g.get_mut(&sid).unwrap();
            s.status = "authorized".to_string();
            s.access_token = tok.access;
            s.refresh_token = tok.refresh;
            s.token_type = tok.token_type;
            s.expires_in = tok.expires;
            if !tok.scope.is_empty() { s.scope_out = tok.scope; }
            s.err_text.clear();
            oauth_write_page(200, "Authorization complete", "You can close this window and return to the app.")
        }
        Err(e) => {
            let mut g = sessions().lock().unwrap();
            if let Some(s) = g.get_mut(&sid) { s.status = "error".to_string(); s.err_text = e.clone(); }
            oauth_write_page(400, "Token exchange failed", &e)
        }
    }
}
