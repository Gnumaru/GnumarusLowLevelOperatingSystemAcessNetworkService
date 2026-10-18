//! HTTP API: routing, auth, CORS, filesystem/shell/sqlite/sysinfo, help catalog.

use crate::http::{Request, Response};

const SERVICE: &str = "GnumarusPythonNativeOperationsHttpService";


#[derive(Clone)]
pub struct State {
    pub auth_required: bool,
    pub auth_token: String,
    pub cwd: String,
}

// ---------------------------------------------------------------------------
// plumbing
// ---------------------------------------------------------------------------

pub(crate) fn cors_headers() -> Vec<(String, String)> {
    vec![
        ("Access-Control-Allow-Origin".to_string(), "*".to_string()),
        (
            "Access-Control-Allow-Methods".to_string(),
            "GET, POST, PUT, DELETE, PATCH, OPTIONS".to_string(),
        ),
        (
            "Access-Control-Allow-Headers".to_string(),
            "Content-Type, Authorization, X-Requested-With".to_string(),
        ),
        ("Access-Control-Max-Age".to_string(), "86400".to_string()),
    ]
}

pub(crate) fn send_json(status: u16, body: &str) -> Response {
    let mut headers = cors_headers();
    headers.push((
        "Content-Type".to_string(),
        "application/json; charset=utf-8".to_string(),
    ));
    Response {
        status,
        headers,
        body: body.as_bytes().to_vec(),
    }
}

pub(crate) fn send_raw(status: u16, body: Vec<u8>, content_type: &str) -> Response {
    let mut headers = cors_headers();
    headers.push(("Content-Type".to_string(), content_type.to_string()));
    Response {
        status,
        headers,
        body,
    }
}

pub(crate) fn label(status: u16) -> &'static str {
    match status {
        400 => "bad_request",
        401 => "unauthorized",
        403 => "forbidden",
        404 => "not_found",
        405 => "method_not_allowed",
        409 => "conflict",
        _ => "error",
    }
}

pub(crate) fn err(status: u16, message: &str, path: Option<&str>) -> Response {
    let mut obj = serde_json::Map::new();
    obj.insert("error".to_string(), serde_json::Value::String(label(status).to_string()));
    obj.insert(
        "message".to_string(),
        serde_json::Value::String(message.to_string()),
    );
    if let Some(p) = path {
        obj.insert("path".to_string(), serde_json::Value::String(p.to_string()));
    }
    send_json(status, &serde_json::Value::Object(obj).to_string())
}

pub(crate) fn check_auth(req: &Request, state: &State) -> Option<Response> {
    if !crate::hosts::host_allowed(req.headers.get("host")) {
        return Some(err(403, crate::hosts::HOST_DENIED_MSG, None));
    }
    if !state.auth_required {
        return None;
    }
    if req.method == "GET" && (req.path == "/" || req.path == "/api/help") {
        return None;
    }
    let mut supplied = String::new();
    if let Some(h) = req.headers.get("authorization") {
        if let Some(tok) = h.strip_prefix("Bearer ") {
            supplied = tok.trim().to_string();
        }
    }
    if supplied.is_empty() {
        supplied = req.query.get("token").cloned().unwrap_or_default();
    }
    let a = supplied.as_bytes();
    let b = state.auth_token.as_bytes();
    // constant-time compare (lengths must match first)
    let mut diff = (a.len() ^ b.len()) as u8;
    for i in 0..a.len().min(b.len()) {
        diff |= a[i] ^ b[i];
    }
    // NOTE: lengths compared above fold into diff; also require non-empty token.
    if !state.auth_token.is_empty() && diff == 0 && a.len() == b.len() {
        return None;
    }
    let mut headers = cors_headers();
    headers.push((
        "Content-Type".to_string(),
        "application/json; charset=utf-8".to_string(),
    ));
    headers.push(("WWW-Authenticate".to_string(), "Bearer".to_string()));
    Some(Response {
        status: 401,
        headers,
        body: br#"{"error":"unauthorized","message":"Valid API token required: send Authorization: Bearer <token> or ?token=."}"#
            .to_vec(),
    })
}

pub fn resolve_path(raw: &str, cwd: &str) -> String {
    let mut p = raw.to_string();
    if p == "~" || p.starts_with("~/") {
        let home = std::env::var("HOME")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_default();
        if !home.is_empty() {
            p = format!("{}{}", home, &p[1..]);
        }
    }
    let abs = if std::path::Path::new(&p).is_absolute() {
        std::path::PathBuf::from(&p)
    } else {
        std::path::Path::new(cwd).join(&p)
    };
    normpath(&abs.to_string_lossy())
}

fn normpath(p: &str) -> String {
    // Lexical normalization (no symlink resolution), both separators.
    let (mut root, mut rest) = ("", p);
    if p.len() >= 2
        && p.as_bytes()[0].is_ascii_alphabetic()
        && p.as_bytes()[1] == b':'
        && (p.len() == 2 || p.as_bytes()[2] == b'\\' || p.as_bytes()[2] == b'/')
    {
        root = &p[..2];
        rest = p[2..].trim_start_matches(['\\', '/']);
    } else if rest.starts_with(r"\\") {
        root = r"\\";
        rest = &rest[2..];
    } else if rest.starts_with('/') {
        root = "/";
        rest = &rest[1..];
    }
    let sep = if root == "/" || root.is_empty() { '/' } else { '\\' };
    let mut parts: Vec<&str> = Vec::new();
    for seg in rest.split(['/', '\\']) {
        if seg.is_empty() || seg == "." {
            continue;
        } else if seg == ".." {
            parts.pop();
        } else {
            parts.push(seg);
        }
    }
    if root.is_empty() || root == "/" {
        format!("{}{}", root, parts.join("/"))
    } else {
        let mut out = root.to_string();
        out.push(sep);
        out.push_str(&parts.join(&sep.to_string()));
        out
    }
}

pub(crate) fn lexists(p: &str) -> bool {
    std::fs::symlink_metadata(p).is_ok()
}

pub(crate) fn entry_type(p: &str) -> &'static str {
    match std::fs::symlink_metadata(p) {
        Ok(m) if m.file_type().is_symlink() => "symlink",
        Ok(_) => match std::fs::metadata(p) {
            Ok(m) if m.is_dir() => "dir",
            Ok(m) if m.is_file() => "file",
            _ => "other",
        },
        Err(_) => "other",
    }
}

#[cfg(unix)]
pub(crate) fn mode_string(p: &str) -> String {
    use std::os::unix::fs::PermissionsExt;
    match std::fs::symlink_metadata(p) {
        Ok(m) => format!("0o{:o}", m.permissions().mode() & 0o777),
        Err(_) => "0o644".to_string(),
    }
}

#[cfg(not(unix))]
pub(crate) fn mode_string(_p: &str) -> String {
    "0o644".to_string()
}

pub(crate) fn file_times(p: &str) -> Option<(f64, f64, f64)> {
    let m = std::fs::symlink_metadata(p).ok()?;
    let atime = m.accessed().ok()?;
    let mtime = m.modified().ok()?;
    // ctime has no std API: Linux statx, else fall back to mtime.
    let ctime = crate::timex::statx_times(p).0.unwrap_or_else(|| {
        mtime
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    });
    let a = atime
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let mt = mtime
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    Some((a, mt, ctime))
}

pub(crate) fn guess_mime(p: &str) -> &'static str {
    let ext = p.rsplit('.').next().unwrap_or("").to_lowercase();
    match ext.as_str() {
        "txt" => "text/plain",
        "html" | "htm" => "text/html",
        "json" => "application/json",
        "xml" => "text/xml",
        "css" => "text/css",
        "js" => "text/javascript",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "pdf" => "application/pdf",
        "zip" => "application/zip",
        "csv" => "text/csv",
        "md" => "text/markdown",
        _ => "application/octet-stream",
    }
}

pub(crate) fn decode_strict(data: &[u8]) -> Option<String> {
    match std::str::from_utf8(data) {
        Ok(s) => Some(s.to_string()),
        Err(_) => None,
    }
}

pub(crate) fn has_binary_controls(s: &str) -> bool {
    s.chars()
        .any(|c| (c as u32) < 32 && !['\t', '\n', '\r'].contains(&c))
}

// ---------------------------------------------------------------------------
// dispatch
// ---------------------------------------------------------------------------

pub fn handle(req: Request, state: &State) -> Response {
    if req.method == "OPTIONS" {
        if !crate::hosts::host_allowed(req.headers.get("host")) {
            return err(403, crate::hosts::HOST_DENIED_MSG, None);
        }
        return Response {
            status: 204,
            headers: cors_headers(),
            body: Vec::new(),
        };
    }
    if let Some(r) = check_auth(&req, state) {
        return r;
    }
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| match req.method.as_str() {
        "GET" => do_get(&req, state),
        "POST" => do_post(&req, state),
        "PUT" => do_put(&req, state),
        "DELETE" => do_delete(&req, state),
        m => err(405, &format!("Method {m} not supported."), None),
    }));
    match result {
        Ok(r) => r,
        Err(_) => err(500, "Internal error.", None),
    }
}

fn do_get(req: &Request, state: &State) -> Response {
    match req.path.as_str() {
        "/api/help" => handle_help(state),
        "/api/dir" => handle_read_dir(req, state),
        "/api/file" => handle_read_file(req, state),
        "/" => {
            let mut o = serde_json::Map::new();
            o.insert("service".to_string(), jstr(SERVICE));
            o.insert("version".to_string(), jstr(crate::VERSION));
            o.insert("help".to_string(), jstr("/api/help"));
            send_json_obj(200, &o)
        }
        "/api/shell" | "/api/sqlite/create" | "/api/sqlite/query" | "/api/socket/open" | "/api/socket/send"
        | "/api/socket/close" | "/api/socket/starttls" => {
            err(405, "Method GET not supported for this route (use POST).", None)
        }
        "/api/socket/recv" => crate::socket::handle_recv(req),
        "/api/socket/ws" => err(
            400,
            "This route requires a WebSocket upgrade (Upgrade: websocket).",
            None,
        ),
        "/api/sysinfo" => handle_sysinfo(),
        _ => err(404, &format!("Unknown route: {}", req.path), None),
    }
}

fn do_post(req: &Request, state: &State) -> Response {
    match req.path.as_str() {
        "/api/file" => handle_create_file(req, state),
        "/api/shell" => handle_exec_shell(req, state),
        "/api/sqlite/create" => handle_sqlite_create(req, state),
        "/api/sqlite/query" => handle_sqlite_query(req, state),
        "/api/socket/open" => crate::socket::handle_open(req),
        "/api/socket/send" => crate::socket::handle_send(req),
        "/api/socket/close" => crate::socket::handle_close(req),
        "/api/socket/starttls" => crate::socket::handle_starttls(req),
        "/" | "/api/help" | "/api/dir" | "/api/sysinfo" | "/api/socket/recv" | "/api/socket/ws" => {
            err(405, "Method POST not supported for this route.", None)
        }
        _ => err(404, &format!("Unknown route: {}", req.path), None),
    }
}

fn do_put(req: &Request, state: &State) -> Response {
    if req.path == "/api/file" {
        return handle_overwrite_file(req, state);
    }
    for known in [
        "/",
        "/api/help",
        "/api/dir",
        "/api/shell",
        "/api/sqlite/create",
        "/api/sqlite/query",
        "/api/sysinfo",
        "/api/socket/open",
        "/api/socket/send",
        "/api/socket/recv",
        "/api/socket/close",
        "/api/socket/starttls",
        "/api/socket/ws",
    ] {
        if req.path == known {
            return err(405, "Method PUT not supported for this route.", None);
        }
    }
    err(404, &format!("Unknown route: {}", req.path), None)
}

fn do_delete(req: &Request, state: &State) -> Response {
    if req.path == "/api/file" {
        return handle_delete_file(req, state);
    }
    if req.path == "/api/dir" {
        return handle_delete_dir(req, state);
    }
    for known in [
        "/",
        "/api/help",
        "/api/shell",
        "/api/sqlite/create",
        "/api/sqlite/query",
        "/api/sysinfo",
        "/api/socket/open",
        "/api/socket/send",
        "/api/socket/recv",
        "/api/socket/close",
        "/api/socket/starttls",
        "/api/socket/ws",
    ] {
        if req.path == known {
            return err(405, "Method DELETE not supported for this route.", None);
        }
    }
    err(404, &format!("Unknown route: {}", req.path), None)
}

pub(crate) fn jstr(s: &str) -> serde_json::Value {
    serde_json::Value::String(s.to_string())
}

fn handle_help(state: &State) -> Response {
    let routes = crate::help::routes();
    let mut auth = serde_json::Map::new();
    auth.insert(
        "required".to_string(),
        jstr(if state.auth_required { "b:true" } else { "b:false" }),
    );
    auth.insert(
        "open_routes".to_string(),
        serde_json::Value::Array(
            ["/", "/api/help", "OPTIONS *"]
                .iter()
                .map(|s| jstr(s))
                .collect(),
        ),
    );
    auth.insert(
        "how".to_string(),
        jstr("Authorization: Bearer <token> header (preferred) or ?token= query parameter."),
    );
    let mut o = serde_json::Map::new();
    o.insert("version".to_string(), jstr(crate::VERSION));
    o.insert(
        "count".to_string(),
        jstr(&format!("i:{}", routes.len())),
    );
    o.insert(
        "routes".to_string(),
        serde_json::Value::Array(routes),
    );
    o.insert("auth".to_string(), serde_json::Value::Object(auth));
    send_json_obj(200, &o)
}

fn handle_sysinfo() -> Response {
    crate::sysinfo::handle()
}

fn handle_exec_shell(req: &Request, state: &State) -> Response {
    crate::shell::handle(req, state)
}

fn handle_sqlite_create(req: &Request, state: &State) -> Response {
    crate::sqlite_api::handle_create(req, state)
}

fn handle_sqlite_query(req: &Request, state: &State) -> Response {
    crate::sqlite_api::handle_query(req, state)
}

pub(crate) fn send_json_obj(status: u16, map: &serde_json::Map<String, serde_json::Value>) -> Response {
    send_json(status, &serde_json::Value::Object(map.clone()).to_string())
}

pub(crate) fn req_path(req: &Request) -> Option<String> {
    match req.query.get("path") {
        Some(p) if !p.is_empty() => Some(p.clone()),
        _ => None,
    }
}

// -- read endpoints ----------------------------------------------------------

fn handle_read_dir(req: &Request, state: &State) -> Response {
    let Some(raw) = req_path(req) else {
        return err(400, "Missing required query parameter: path", None);
    };
    let abs = resolve_path(&raw, &state.cwd);
    if !lexists(&abs) {
        return err(404, "Path does not exist", Some(&abs));
    }
    match std::fs::metadata(&abs) {
        Ok(m) if m.is_dir() => {}
        Ok(_) => return err(400, "Path is not a directory", Some(&abs)),
        Err(_) => return err(404, "Path does not exist", Some(&abs)),
    }
    let entries_dir = match std::fs::read_dir(&abs) {
        Ok(rd) => rd,
        Err(_) => return err(403, "Cannot list directory", Some(&abs)),
    };
    let mut names: Vec<String> = Vec::new();
    for entry in entries_dir {
        match entry {
            Ok(e) => names.push(e.file_name().to_string_lossy().into_owned()),
            Err(_) => return err(403, "Cannot list directory", Some(&abs)),
        }
    }
    names.sort();
    let mut entries: Vec<serde_json::Value> = Vec::new();
    for name in &names {
        let full = format!("{abs}/{name}");
        let (atime, mtime, ctime) = match file_times(&full) {
            Some(t) => t,
            None => continue,
        };
        let (_, btime) = crate::timex::statx_times(&full);
        let mut o = serde_json::Map::new();
        o.insert("name".to_string(), jstr(name));
        o.insert("path".to_string(), jstr(&full));
        o.insert("type".to_string(), jstr(entry_type(&full)));
        let size = std::fs::symlink_metadata(&full).map(|m| m.len() as i64).unwrap_or(0);
        o.insert("size".to_string(), jstr(&crate::tags::jenc_int(size)));
        o.insert("atime".to_string(), jstr(&jf(atime)));
        o.insert("mtime".to_string(), jstr(&jf(mtime)));
        o.insert("ctime".to_string(), jstr(&jf(ctime)));
        o.insert(
            "crtime".to_string(),
            jstr(&btime.map(|v| jf(v)).unwrap_or_else(|| "null".to_string())),
        );
        o.insert("mode".to_string(), jstr(&mode_string(&full)));
        entries.push(serde_json::Value::Object(o));
    }
    let mut o = serde_json::Map::new();
    o.insert("path".to_string(), jstr(&raw));
    o.insert("absolute_path".to_string(), jstr(&abs));
    o.insert("count".to_string(), jstr(&crate::tags::jenc_int(entries.len() as i64)));
    o.insert(
        "entries".to_string(),
        serde_json::Value::Array(entries),
    );
    send_json_obj(200, &o)
}

// jenc_* live in crate::tags; local float wrapper that cannot fail.
pub(crate) fn jf(v: f64) -> String {
    crate::tags::jenc_float(v).unwrap_or_else(|_| "null".to_string())
}

fn handle_read_file(req: &Request, state: &State) -> Response {
    let Some(raw) = req_path(req) else {
        return err(400, "Missing required query parameter: path", None);
    };
    let abs = resolve_path(&raw, &state.cwd);
    if !lexists(&abs) {
        return err(404, "Path does not exist", Some(&abs));
    }
    match std::fs::metadata(&abs) {
        Ok(m) if m.is_dir() => match std::fs::symlink_metadata(&abs) {
            Ok(lm) if lm.file_type().is_symlink() => {}
            _ => return err(400, "Path is a directory, not a file", Some(&abs)),
        },
        Err(_) => return err(404, "Path does not exist", Some(&abs)),
        _ => {}
    }
    let encoding = req
        .query
        .get("encoding")
        .map(|s| s.to_lowercase())
        .unwrap_or_else(|| "auto".to_string());
    if !["auto", "text", "base64"].contains(&encoding.as_str()) {
        return err(
            400,
            &format!("Invalid encoding param: '{encoding}' (use auto|text|base64)"),
            None,
        );
    }
    let raw_mode = req.query.get("raw").map(|s| s.as_str()) == Some("1");
    let data = match std::fs::read(&abs) {
        Ok(d) => d,
        Err(e) => {
            use std::io::ErrorKind::*;
            return match e.kind() {
                NotFound => err(404, "Path does not exist", Some(&abs)),
                PermissionDenied => err(403, "Permission denied", Some(&abs)),
                _ => err(403, &format!("Cannot read file: {e}"), Some(&abs)),
            };
        }
    };
    let mtime = std::fs::symlink_metadata(&abs)
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| jf(d.as_secs_f64()))
        .unwrap_or_else(|| "null".to_string());
    if raw_mode {
        return send_raw(200, data, guess_mime(&abs));
    }
    let mime = guess_mime(&abs).to_string();
    let meta = |binary: bool, enc: &str, content: String| {
        let mut o = serde_json::Map::new();
        o.insert("path".to_string(), jstr(&raw));
        o.insert("absolute_path".to_string(), jstr(&abs));
        o.insert("size".to_string(), jstr(&crate::tags::jenc_int(data.len() as i64)));
        o.insert("mtime".to_string(), jstr(&mtime));
        o.insert("mime_type".to_string(), jstr(&mime));
        o.insert(
            "is_binary".to_string(),
            jstr(if binary { "b:true" } else { "b:false" }),
        );
        o.insert("encoding".to_string(), jstr(enc));
        o.insert("content".to_string(), jstr(&content));
        send_json_obj(200, &o)
    };
    if encoding == "text" {
        return meta(false, "utf-8", String::from_utf8_lossy(&data).into_owned());
    }
    if encoding == "base64" {
        return meta(true, "base64", crate::codec::b64_encode(&data));
    }
    match decode_strict(&data) {
        Some(text) if !has_binary_controls(&text) => meta(false, "utf-8", text),
        _ => meta(true, "base64", crate::codec::b64_encode(&data)),
    }
}

// -- write/delete endpoints --------------------------------------------------

pub(crate) fn req_json(req: &Request) -> Result<serde_json::Map<String, serde_json::Value>, Response> {
    let ct = req.headers.get("content-type").map(|s| s.as_str()).unwrap_or("");
    if !ct.contains("application/json") {
        return Err(err(400, "Content-Type must be application/json.", None));
    }
    if req.body.is_empty() {
        return Ok(serde_json::Map::new());
    }
    match serde_json::from_slice::<serde_json::Value>(&req.body) {
        Ok(serde_json::Value::Object(m)) => Ok(m),
        Ok(_) => Err(err(400, "JSON body must be an object.", None)),
        Err(_) => Err(err(400, "Invalid JSON body.", None)),
    }
}

pub(crate) fn parse_write_body(req: &Request) -> Result<Vec<u8>, Response> {
    let ct = req.headers.get("content-type").map(|s| s.as_str()).unwrap_or("");
    if !ct.contains("application/json") {
        return Ok(req.body.clone());
    }
    let obj = req_json(req)?;
    let content = obj.get("content");
    let encoding = match obj.get("encoding") {
        None | Some(serde_json::Value::Null) => "text".to_string(),
        Some(serde_json::Value::String(s)) => s.to_lowercase(),
        Some(_) => {
            return Err(err(400, "Invalid encoding: (use text|base64).", None));
        }
    };
    if encoding == "text" {
        match content {
            None | Some(serde_json::Value::Null) => Ok(Vec::new()),
            Some(serde_json::Value::String(s)) => Ok(s.as_bytes().to_vec()),
            Some(_) => Err(err(400, "'content' must be a string for text encoding.", None)),
        }
    } else if encoding == "base64" {
        match content {
            None | Some(serde_json::Value::Null) => Ok(Vec::new()),
            Some(serde_json::Value::String(s)) => match crate::codec::b64_decode(s) {
                Some(d) => Ok(d),
                None => Err(err(400, "Invalid base64 'content'.", None)),
            },
            Some(_) => Err(err(400, "'content' must be a base64 string.", None)),
        }
    } else {
        Err(err(
            400,
            &format!("Invalid encoding: '{encoding}' (use text|base64)."),
            None,
        ))
    }
}

fn handle_create_file(req: &Request, state: &State) -> Response {
    let raw = match req.query.get("path") {
        Some(p) if !p.is_empty() => p.clone(),
        _ => return err(400, "Missing required query parameter: path", None),
    };
    let abs = resolve_path(&raw, &state.cwd);
    if lexists(&abs) {
        return err(409, "Path already exists (use PUT to overwrite).", Some(&abs));
    }
    let parent = std::path::Path::new(&abs)
        .parent()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    if !std::path::Path::new(&parent).is_dir() {
        return err(404, "Parent directory does not exist.", Some(&abs));
    }
    let data = match parse_write_body(req) {
        Ok(d) => d,
        Err(r) => return r,
    };
    match std::fs::OpenOptions::new().write(true).create_new(true).open(&abs) {
        Ok(mut fh) => {
            use std::io::Write;
            if fh.write_all(&data).is_err() {
                return err(400, "Cannot create file", Some(&abs));
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists || lexists(&abs) => {
            return err(409, "Path already exists (use PUT to overwrite).", Some(&abs));
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return err(404, "Parent directory does not exist.", Some(&abs));
        }
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            return err(403, "Permission denied", Some(&abs));
        }
        Err(e) => return err(400, &format!("Cannot create file: {e}"), Some(&abs)),
    }
    let mut o = serde_json::Map::new();
    o.insert("path".to_string(), jstr(&raw));
    o.insert("absolute_path".to_string(), jstr(&abs));
    o.insert("size".to_string(), jstr(&crate::tags::jenc_int(data.len() as i64)));
    o.insert("mime_type".to_string(), jstr(guess_mime(&abs)));
    send_json_obj(201, &o)
}

fn handle_overwrite_file(req: &Request, state: &State) -> Response {
    let raw = match req.query.get("path") {
        Some(p) if !p.is_empty() => p.clone(),
        _ => return err(400, "Missing required query parameter: path", None),
    };
    let abs = resolve_path(&raw, &state.cwd);
    if !lexists(&abs) {
        return err(404, "Path does not exist", Some(&abs));
    }
    match std::fs::metadata(&abs) {
        Ok(m) if m.is_dir() => match std::fs::symlink_metadata(&abs) {
            Ok(lm) if lm.file_type().is_symlink() => {}
            _ => return err(400, "Path is a directory, not a file", Some(&abs)),
        },
        Err(_) => return err(404, "Path does not exist", Some(&abs)),
        _ => {}
    }
    let data = match parse_write_body(req) {
        Ok(d) => d,
        Err(r) => return r,
    };
    match std::fs::write(&abs, &data) {
        Ok(()) => {}
        Err(e) => {
            use std::io::ErrorKind::*;
            return match e.kind() {
                PermissionDenied => err(403, "Permission denied", Some(&abs)),
                NotFound => err(404, "Path does not exist", Some(&abs)),
                _ => err(400, &format!("Cannot write file: {e}"), Some(&abs)),
            };
        }
    }
    let mut o = serde_json::Map::new();
    o.insert("path".to_string(), jstr(&raw));
    o.insert("absolute_path".to_string(), jstr(&abs));
    o.insert("size".to_string(), jstr(&crate::tags::jenc_int(data.len() as i64)));
    o.insert("mime_type".to_string(), jstr(guess_mime(&abs)));
    send_json_obj(200, &o)
}

fn handle_delete_file(req: &Request, state: &State) -> Response {
    let raw = match req.query.get("path") {
        Some(p) if !p.is_empty() => p.clone(),
        _ => return err(400, "Missing required query parameter: path", None),
    };
    let abs = resolve_path(&raw, &state.cwd);
    if !lexists(&abs) {
        return err(404, "Path does not exist", Some(&abs));
    }
    match std::fs::metadata(&abs) {
        Ok(m) if m.is_dir() => match std::fs::symlink_metadata(&abs) {
            Ok(lm) if lm.file_type().is_symlink() => {}
            _ => {
                return err(
                    400,
                    "Path is a directory, not a file (use DELETE /api/dir).",
                    Some(&abs),
                )
            }
        },
        Err(_) => return err(404, "Path does not exist", Some(&abs)),
        _ => {}
    }
    match std::fs::remove_file(&abs) {
        Ok(()) => {}
        Err(e) => {
            use std::io::ErrorKind::*;
            return match e.kind() {
                NotFound => err(404, "Path does not exist", Some(&abs)),
                PermissionDenied => err(403, "Permission denied", Some(&abs)),
                _ => err(400, &format!("Cannot delete file: {e}"), Some(&abs)),
            };
        }
    }
    let mut o = serde_json::Map::new();
    o.insert("path".to_string(), jstr(&raw));
    o.insert("absolute_path".to_string(), jstr(&abs));
    o.insert("deleted".to_string(), jstr("b:true"));
    send_json_obj(200, &o)
}

fn handle_delete_dir(req: &Request, state: &State) -> Response {
    let raw = match req.query.get("path") {
        Some(p) if !p.is_empty() => p.clone(),
        _ => return err(400, "Missing required query parameter: path", None),
    };
    let abs = resolve_path(&raw, &state.cwd);
    if !lexists(&abs) {
        return err(404, "Path does not exist", Some(&abs));
    }
    let is_link = std::fs::symlink_metadata(&abs)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false);
    if is_link || !std::path::Path::new(&abs).is_dir() {
        return err(400, "Path is not a directory", Some(&abs));
    }
    match std::fs::remove_dir(&abs) {
        Ok(()) => {}
        Err(e) => {
            use std::io::ErrorKind::*;
            return match e.kind() {
                NotFound => err(404, "Path does not exist", Some(&abs)),
                PermissionDenied => err(403, "Permission denied", Some(&abs)),
                _ => {
                    let nonempty = std::fs::read_dir(&abs)
                        .map(|mut rd| rd.any(|_| true))
                        .unwrap_or(false);
                    if nonempty {
                        err(
                            400,
                            &format!("Cannot delete directory: Directory not empty: '{abs}'"),
                            Some(&abs),
                        )
                    } else {
                        err(400, &format!("Cannot delete directory: {e}"), Some(&abs))
                    }
                }
            };
        }
    }
    let mut o = serde_json::Map::new();
    o.insert("path".to_string(), jstr(&raw));
    o.insert("absolute_path".to_string(), jstr(&abs));
    o.insert("deleted".to_string(), jstr("b:true"));
    send_json_obj(200, &o)
}
