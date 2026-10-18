//! Minimal HTTP/1.1 server over TcpListener (stdlib only).
//!
//! Supports origin-form requests with Content-Length bodies; every
//! response carries `Connection: close`.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};

pub struct Request {
    pub method: String,
    pub path: String,
    pub query: HashMap<String, String>,
    pub headers: HashMap<String, String>,
    pub body: Vec<u8>,
}

pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        _ => "Error",
    }
}

fn read_line(buf: &[u8], mut pos: usize) -> Option<(String, usize)> {
    let start = pos;
    while pos + 1 < buf.len() {
        if buf[pos] == b'\r' && buf[pos + 1] == b'\n' {
            let line = String::from_utf8_lossy(&buf[start..pos]).into_owned();
            return Some((line, pos + 2));
        }
        pos += 1;
    }
    None
}

pub fn read_request(stream: &mut TcpStream) -> Result<Request, String> {
    let mut buf = Vec::with_capacity(8192);
    let mut tmp = [0u8; 8192];
    // Read until end of headers (cap 1 MB).
    loop {
        // linear scan for \r\n\r\n
        if let Some(end) = find_headers_end(&buf) {
            let head = buf[..end].to_vec();
            let mut rest = buf[end..].to_vec();
            return parse_request(head, &mut rest, stream);
        }
        if buf.len() > 1024 * 1024 {
            return Err("headers too large".to_string());
        }
        match stream.read(&mut tmp) {
            Ok(0) => {
                if buf.is_empty() {
                    return Err("empty connection".to_string());
                }
                return Err("truncated headers".to_string());
            }
            Ok(n) => buf.extend_from_slice(&tmp[..n]),
            Err(e) => return Err(format!("read error: {e}")),
        }
    }
}

fn find_headers_end(buf: &[u8]) -> Option<usize> {
    if buf.len() < 4 {
        return None;
    }
    for i in 0..buf.len() - 3 {
        if buf[i] == b'\r' && buf[i + 1] == b'\n' && buf[i + 2] == b'\r' && buf[i + 3] == b'\n' {
            return Some(i + 4);
        }
    }
    None
}

fn parse_request(
    head: Vec<u8>,
    rest: &mut Vec<u8>,
    stream: &mut TcpStream,
) -> Result<Request, String> {
    let mut pos = 0;
    let (request_line, next) = read_line(&head, pos).ok_or("bad request line")?;
    pos = next;
    let mut parts = request_line.splitn(3, ' ');
    let method = parts.next().ok_or("bad request line")?.to_string();
    let target = parts.next().ok_or("bad request line")?.to_string();
    if parts.next().is_none() {
        return Err("bad request line".to_string());
    }
    let mut headers = HashMap::new();
    loop {
        let (line, next) = read_line(&head, pos).ok_or("bad headers")?;
        pos = next;
        if line.is_empty() {
            break;
        }
        if let Some(colon) = line.find(':') {
            headers.insert(
                line[..colon].trim().to_lowercase(),
                line[colon + 1..].trim().to_string(),
            );
        }
    }
    let (path, query) = split_target(&target);
    let content_length: usize = headers
        .get("content-length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let mut body = std::mem::take(rest);
    while body.len() < content_length {
        let mut tmp = [0u8; 65536];
        match stream.read(&mut tmp) {
            Ok(0) => break,
            Ok(n) => body.extend_from_slice(&tmp[..n]),
            Err(e) => return Err(format!("body read error: {e}")),
        }
    }
    body.truncate(content_length);
    Ok(Request {
        method,
        path,
        query,
        headers,
        body,
    })
}

fn split_target(target: &str) -> (String, HashMap<String, String>) {
    let (path, qs) = match target.find('?') {
        Some(i) => (&target[..i], &target[i + 1..]),
        None => (target, ""),
    };
    let mut query = HashMap::new();
    for part in qs.split('&') {
        if part.is_empty() {
            continue;
        }
        let (k, v) = match part.find('=') {
            Some(i) => (&part[..i], &part[i + 1..]),
            None => (part, ""),
        };
        let key = crate::codec::percent_decode(k);
        // first value wins (parse_qs parity)
        if !query.contains_key(&key) {
            query.insert(key, crate::codec::percent_decode(v));
        }
    }
    (crate::codec::percent_decode(path), query)
}

pub fn write_response(stream: &mut TcpStream, res: &Response) -> std::io::Result<()> {
    let mut head = format!("HTTP/1.1 {} {}\r\n", res.status, reason(res.status));
    head.push_str("Connection: close\r\n");
    for (k, v) in &res.headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str(&format!("Content-Length: {}\r\n\r\n", res.body.len()));
    stream.write_all(head.as_bytes())?;
    stream.write_all(&res.body)?;
    stream.flush()
}

pub fn serve(
    listener: TcpListener,
    state: std::sync::Arc<crate::api::State>,
    handle: impl Fn(Request) -> Response + Send + Sync + 'static,
) {
    let handle = std::sync::Arc::new(handle);
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else {
            continue;
        };
        let handle = handle.clone();
        let state = state.clone();
        std::thread::spawn(move || {
            let req = match read_request(&mut stream) {
                Ok(req) => req,
                Err(_) => {
                    let response = Response {
                        status: 400,
                        headers: vec![
                            ("Content-Type".to_string(),
                                "application/json; charset=utf-8".to_string()),
                            ("Access-Control-Allow-Origin".to_string(), "*".to_string()),
                        ],
                        body: br#"{"error":"bad_request","message":"Malformed HTTP request"}"#.to_vec(),
                    };
                    let _ = write_response(&mut stream, &response);
                    return;
                }
            };
            // A WebSocket upgrade hijacks the connection: no normal
            // response follows (plain GETs fall through to do_get).
            if crate::socket_ws::is_ws_upgrade_request(&req) {
                crate::socket_ws::handle_upgrade(stream, req, &state);
                return;
            }
            let response = handle(req);
            let _ = write_response(&mut stream, &response);
        });
    }
}
