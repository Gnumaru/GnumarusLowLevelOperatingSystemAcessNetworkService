//! Transparent TCP socket proxy: the service holds a real TCP client
//! socket per session and exposes a byte-transparent tunnel over plain
//! HTTP, so browser pages (which cannot open raw sockets) can speak
//! arbitrary TCP protocols through the local service.

use crate::api::{err, jstr, send_json_obj};
use crate::http::{Request, Response};
use crate::tags::{jdec, jenc_int, Tagged};
use openssl::ssl::{SslConnector, SslMethod, SslStream, SslVersion};
use openssl::x509::X509;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::{Arc, Mutex, Once, OnceLock};
use std::time::{Duration, Instant};

pub const SOCK_DEFAULT_CONNECT_TIMEOUT: f64 = 5.0;
pub const SOCK_MAX_CONNECT_TIMEOUT: f64 = 30.0;
pub const SOCK_DEFAULT_RECV_TIMEOUT: f64 = 5.0;
pub const SOCK_MAX_RECV_TIMEOUT: f64 = 60.0;
pub const SOCK_DEFAULT_MAX_BYTES: usize = 65536;
pub const SOCK_MAX_BYTES: usize = 1024 * 1024;
pub const SOCK_MAX_SESSIONS: usize = 128;
pub const SOCK_IDLE_SECS: u64 = 300;

/// Transport of one session: plaintext TCP, or TLS over TCP
/// (implicit TLS at open, or STARTTLS upgrade). send/recv/close/WS
/// all see the cleartext stream.
enum Stream {
    Plain(TcpStream),
    Tls(SslStream<TcpStream>),
}

impl Read for Stream {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Stream::Plain(s) => s.read(buf),
            Stream::Tls(s) => s.read(buf),
        }
    }
}

impl Write for Stream {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        match self {
            Stream::Plain(s) => s.write(buf),
            Stream::Tls(s) => s.write(buf),
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            Stream::Plain(s) => s.flush(),
            Stream::Tls(s) => s.flush(),
        }
    }
}

impl Stream {
    fn set_read_timeout(&self, d: Option<Duration>) -> std::io::Result<()> {
        match self {
            Stream::Plain(s) => s.set_read_timeout(d),
            Stream::Tls(s) => s.get_ref().set_read_timeout(d),
        }
    }
    fn set_write_timeout(&self, d: Option<Duration>) -> std::io::Result<()> {
        match self {
            Stream::Plain(s) => s.set_write_timeout(d),
            Stream::Tls(s) => s.get_ref().set_write_timeout(d),
        }
    }
    /// Plain TCP clone for unblocking pumps (shutdown is
    /// socket-level, so it also unblocks TLS readers).
    fn killer_dup(&self) -> std::io::Result<TcpStream> {
        match self {
            Stream::Plain(s) => s.try_clone(),
            Stream::Tls(s) => s.get_ref().try_clone(),
        }
    }
    /// Owned plaintext fd for a STARTTLS handshake (the session fd
    /// itself stays untouched until the upgrade commits).
    fn plain_dup(&self) -> Option<TcpStream> {
        match self {
            Stream::Plain(s) => s.try_clone().ok(),
            Stream::Tls(_) => None,
        }
    }
}

struct Session {
    stream: Arc<Mutex<Stream>>,
    /// Raw dup used only to unblock pumps on close (no locks taken).
    killer: TcpStream,
    host: String,
    port: i64,
    /// "tcp" or "tls": TLS is a transport property of the session,
    /// negotiated at open (implicit) or via starttls, and verified
    /// against the system roots (+ optional ca_pem) with hostname
    /// check and SNI. send/recv/close/WS see cleartext.
    kind: String,
    last: Instant,
    eof: bool,
    recv_busy: bool,
    // ws_attached marks a live WebSocket bridge: the idle sweeper
    // skips it and HTTP send/recv are refused while set.
    ws_attached: bool,
}

static SESSIONS: OnceLock<Mutex<HashMap<String, Session>>> = OnceLock::new();
static SWEEPER: Once = Once::new();

fn sessions() -> &'static Mutex<HashMap<String, Session>> {
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn sweep_expired() {
    let now = Instant::now();
    let mut map = sessions().lock().unwrap();
    map.retain(|_, s| {
        s.ws_attached || now.duration_since(s.last).as_secs() <= SOCK_IDLE_SECS
    });
}

fn ensure_sweeper() {
    SWEEPER.call_once(|| {
        std::thread::spawn(|| loop {
            std::thread::sleep(Duration::from_secs(60));
            sweep_expired();
        });
    });
}

fn mint_id() -> String {
    use std::io::Read;
    let mut buf = [0u8; 16];
    if let Ok(mut fh) = std::fs::File::open("/dev/urandom") {
        let _ = fh.read_exact(&mut buf);
    } else {
        let n = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        buf.copy_from_slice(&n.to_le_bytes());
    }
    const B64URL: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(22);
    for chunk in buf.chunks(3) {
        let mut n: u32 = 0;
        for (i, &b) in chunk.iter().enumerate() {
            n |= (b as u32) << (16 - 8 * i);
        }
        let chars = if chunk.len() == 3 { 4 } else if chunk.len() == 2 { 3 } else { 2 };
        for i in 0..chars {
            out.push(B64URL[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

fn req_obj(req: &Request) -> Result<serde_json::Map<String, serde_json::Value>, Response> {
    let ct = req.headers.get("content-type").map(|s| s.as_str()).unwrap_or("");
    if !ct.contains("application/json") {
        return Err(err(
            400,
            "Content-Type must be application/json with body { host, port, timeout? }.",
            None,
        ));
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

/// Optional sni/ca_pem body fields shared by socket_open and
/// socket_starttls. Absent (or explicit null) means "not given".
fn parse_tls_identity(
    obj: &serde_json::Map<String, serde_json::Value>,
) -> Result<(String, String), Response> {
    let mut sni = String::new();
    if let Some(v) = obj.get("sni") {
        if !v.is_null() {
            match v.as_str() {
                Some(s) if !s.trim().is_empty() => sni = s.trim().to_string(),
                _ => {
                    return Err(err(
                        400,
                        "Field 'sni' must be a non-empty string.",
                        None,
                    ))
                }
            }
        }
    }
    let mut ca_pem = String::new();
    if let Some(v) = obj.get("ca_pem") {
        if !v.is_null() {
            match v.as_str() {
                Some(s) if !s.trim().is_empty() => ca_pem = s.to_string(),
                _ => {
                    return Err(err(
                        400,
                        "Field 'ca_pem' must be a PEM CA bundle string.",
                        None,
                    ))
                }
            }
        }
    }
    Ok((sni, ca_pem))
}

/// Root pool from the system store plus an optional extra PEM bundle
/// (corporate intercepts, test PKIs).
fn tls_connector(ca_pem: &str) -> Result<SslConnector, Response> {
    let fail = |e: String| err(400, &format!("TLS setup failed: {e}"), None);
    let mut builder =
        SslConnector::builder(SslMethod::tls()).map_err(|e| fail(e.to_string()))?;
    builder
        .set_min_proto_version(Some(SslVersion::TLS1_2))
        .map_err(|e| fail(e.to_string()))?;
    let mut store = openssl::x509::store::X509StoreBuilder::new()
        .map_err(|e| fail(e.to_string()))?;
    store
        .set_default_paths()
        .map_err(|e| fail(e.to_string()))?;
    if !ca_pem.is_empty() {
        let certs = X509::stack_from_pem(ca_pem.as_bytes())
            .ok()
            .filter(|v| !v.is_empty());
        match certs {
            Some(list) => {
                for c in &list {
                    store.add_cert(c.clone()).map_err(|e| fail(e.to_string()))?;
                }
            }
            None => {
                return Err(err(
                    400,
                    "Field 'ca_pem' must be a valid PEM CA bundle.",
                    None,
                ))
            }
        }
    }
    builder.set_cert_store(store.build());
    Ok(builder.build())
}

/// Client handshake over an established TCP connection. The timeout
/// bounds the whole handshake. On failure the raw connection is
/// dropped by the caller: open drops it, starttls keeps the
/// plaintext session.
fn tls_handshake(
    stream: TcpStream,
    sni: &str,
    connector: &SslConnector,
    timeout: Duration,
) -> Result<SslStream<TcpStream>, String> {
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    match connector.connect(sni, stream) {
        Ok(tls) => {
            let _ = tls.get_ref().set_read_timeout(None);
            let _ = tls.get_ref().set_write_timeout(None);
            Ok(tls)
        }
        Err(e) => Err(e.to_string()),
    }
}

pub fn handle_open(req: &Request) -> Response {
    ensure_sweeper();
    let obj = match req_obj(req) {
        Ok(o) => o,
        Err(r) => return r,
    };
    let host = match obj.get("host") {
        Some(serde_json::Value::String(s)) if !s.trim().is_empty() => s.clone(),
        _ => return err(400, "Missing or empty required field: host.", None),
    };
    let port_raw = match obj.get("port") {
        None | Some(serde_json::Value::Null) => {
            return err(400, "Missing required field: port (i:<1-65535>).", None)
        }
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(_) => {
            return err(400, "Field 'port' must be an integer 1-65535 (i:<int>).", None)
        }
    };
    if !port_raw.starts_with("i:") {
        return err(400, "Field 'port' must be an integer 1-65535 (i:<int>).", None);
    }
    let port = match jdec(&port_raw) {
        Ok(Tagged::Int(n)) if n >= 1 && n <= 65535 => n,
        _ => return err(400, "Field 'port' must be an integer 1-65535 (i:<int>).", None),
    };
    let timeout = match obj.get("timeout") {
        None | Some(serde_json::Value::Null) => SOCK_DEFAULT_CONNECT_TIMEOUT,
        Some(serde_json::Value::String(s)) => match jdec(s) {
            Ok(Tagged::Int(n)) => n as f64,
            Ok(Tagged::Float(f)) => f,
            _ => {
                return err(
                    400,
                    "Field 'timeout' must be a tagged number (i:<int> or f:<float>).",
                    None,
                )
            }
        },
        Some(_) => {
            return err(
                400,
                "Field 'timeout' must be a tagged number (i:<int> or f:<float>).",
                None,
            )
        }
    };
    if !(timeout > 0.0 && timeout <= SOCK_MAX_CONNECT_TIMEOUT) {
        return err(
            400,
            &format!("Field 'timeout' must be within (0, {SOCK_MAX_CONNECT_TIMEOUT}]."),
            None,
        );
    }
    let tls_mode = match obj.get("tls") {
        None | Some(serde_json::Value::Null) => false,
        Some(serde_json::Value::String(s)) => match jdec(s) {
            Ok(Tagged::Bool(b)) => b,
            _ => return err(400, "Field 'tls' must be b:true or b:false.", None),
        },
        Some(_) => return err(400, "Field 'tls' must be b:true or b:false.", None),
    };
    let (mut sni, ca_pem) = match parse_tls_identity(&obj) {
        Ok(v) => v,
        Err(r) => return r,
    };
    if sni.is_empty() {
        sni = host.clone();
    }
    let connector = if tls_mode {
        match tls_connector(&ca_pem) {
            Ok(c) => Some(c),
            Err(r) => return r,
        }
    } else {
        None
    };
    sweep_expired();
    if sessions().lock().unwrap().len() >= SOCK_MAX_SESSIONS {
        return err(
            400,
            &format!("Too many open socket sessions ({SOCK_MAX_SESSIONS})."),
            None,
        );
    }
    let addrs: Vec<_> = match format!("{host}:{port}").to_socket_addrs() {
        Ok(it) => it.collect(),
        Err(e) => {
            return err(400, &format!("Cannot connect to {host}:{port}: {e}"), None);
        }
    };
    let dur = Duration::from_secs_f64(timeout);
    let mut stream_opt = None;
    let mut last_err = String::new();
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, dur) {
            Ok(s) => {
                stream_opt = Some(s);
                break;
            }
            Err(e) => last_err = e.to_string(),
        }
    }
    let stream = match stream_opt {
        Some(s) => s,
        None => {
            return err(
                400,
                &format!("Cannot connect to {host}:{port}: {last_err}"),
                None,
            )
        }
    };
    let mut kind = "tcp".to_string();
    let stream = if let Some(connector) = connector {
        match tls_handshake(stream, &sni, &connector, dur) {
            Ok(tls) => {
                kind = "tls".to_string();
                Stream::Tls(tls)
            }
            Err(e) => {
                return err(
                    400,
                    &format!("TLS handshake with {host}:{port} failed: {e}"),
                    None,
                )
            }
        }
    } else {
        Stream::Plain(stream)
    };
    let killer = match stream.killer_dup() {
        Ok(k) => k,
        Err(e) => return err(400, &format!("Socket error: {e}"), None),
    };
    let sid = mint_id();
    sessions().lock().unwrap().insert(
        sid.clone(),
        Session {
            stream: Arc::new(Mutex::new(stream)),
            killer,
            host: host.clone(),
            port,
            kind: kind.clone(),
            last: Instant::now(),
            eof: false,
            recv_busy: false,
            ws_attached: false,
        },
    );
    let mut o = serde_json::Map::new();
    o.insert("id".to_string(), jstr(&sid));
    o.insert("kind".to_string(), jstr(&kind));
    o.insert("host".to_string(), jstr(&host));
    o.insert("port".to_string(), jstr(&jenc_int(port)));
    send_json_obj(201, &o)
}

fn parse_send_body(
    body: &[u8],
    ct: &str,
) -> Result<Vec<u8>, Response> {
    if ct.contains("application/json") {
        let obj: serde_json::Map<String, serde_json::Value> = if body.is_empty() {
            serde_json::Map::new()
        } else {
            match serde_json::from_slice::<serde_json::Value>(body) {
                Ok(serde_json::Value::Object(m)) => m,
                Ok(_) => return Err(err(400, "JSON body must be an object.", None)),
                Err(_) => return Err(err(400, "Invalid JSON body.", None)),
            }
        };
        let content = match obj.get("content") {
            None | Some(serde_json::Value::Null) => String::new(),
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(_) => return Err(err(400, "'content' must be a string.", None)),
        };
        let encoding = match obj.get("encoding") {
            None | Some(serde_json::Value::Null) => "text".to_string(),
            Some(serde_json::Value::String(s)) => s.to_lowercase(),
            Some(_) => {
                return Err(err(400, "Invalid encoding: (use text|base64).", None));
            }
        };
        match encoding.as_str() {
            "text" => Ok(content.into_bytes()),
            "base64" => match crate::codec::b64_decode(&content) {
                Some(v) => Ok(v),
                None => Err(err(400, "Invalid base64 'content'.", None)),
            },
            other => Err(err(
                400,
                &format!("Invalid encoding: '{other}' (use text|base64)."),
                None,
            )),
        }
    } else {
        Ok(body.to_vec())
    }
}

pub fn handle_send(req: &Request) -> Response {
    let sid = match req.query.get("id") {
        Some(s) if !s.is_empty() => s.clone(),
        _ => return err(400, "Missing required query parameter: id", None),
    };
    let ct = req.headers.get("content-type").cloned().unwrap_or_default();
    let data = match parse_send_body(&req.body, &ct) {
        Ok(d) => d,
        Err(r) => return r,
    };
    {
        let map = sessions().lock().unwrap();
        match map.get(&sid) {
            Some(sess) if sess.ws_attached => {
                return err(
                    400,
                    "Socket session is attached to a websocket (HTTP send refused while attached).",
                    None,
                )
            }
            _ => {}
        }
    }
    let arc = {
        let mut map = sessions().lock().unwrap();
        match map.get_mut(&sid) {
            Some(sess) => {
                sess.last = Instant::now();
                sess.stream.clone()
            }
            None => return err(404, &format!("Unknown socket session: {sid}"), None),
        }
    };
    let write_res = {
        let mut guard = arc.lock().unwrap();
        if let Err(e) = guard.set_write_timeout(Some(Duration::from_secs(30))) {
            Err(e.to_string())
        } else {
            let r = guard.write_all(&data).map_err(|e| e.to_string());
            let _ = guard.set_write_timeout(None);
            r
        }
    };
    if let Err(e) = write_res {
        return err(400, &format!("Socket error: {e}"), None);
    }
    {
        let mut map = sessions().lock().unwrap();
        if let Some(sess) = map.get_mut(&sid) {
            sess.last = Instant::now();
        }
    }
    let mut o = serde_json::Map::new();
    o.insert("id".to_string(), jstr(&sid));
    o.insert("bytes".to_string(), jstr(&jenc_int(data.len() as i64)));
    send_json_obj(200, &o)
}

fn parse_plain_timeout(text: Option<&String>, dflt: f64, max: f64) -> Option<f64> {
    match text {
        None => Some(dflt),
        Some(s) if s.is_empty() => Some(dflt),
        Some(s) => match s.trim().parse::<f64>() {
            Ok(v) if v.is_finite() && v > 0.0 && v <= max => Some(v),
            _ => None,
        },
    }
}

pub fn handle_recv(req: &Request) -> Response {
    let timeout = match parse_plain_timeout(
        req.query.get("timeout"),
        SOCK_DEFAULT_RECV_TIMEOUT,
        SOCK_MAX_RECV_TIMEOUT,
    ) {
        Some(t) => t,
        None => {
            return err(
                400,
                &format!(
                    "Invalid timeout param (seconds within (0, {SOCK_MAX_RECV_TIMEOUT}])."
                ),
                None,
            )
        }
    };
    let max_bytes = match req.query.get("max_bytes") {
        None => SOCK_DEFAULT_MAX_BYTES,
        Some(s) if s.is_empty() => SOCK_DEFAULT_MAX_BYTES,
        Some(s) => match s.trim().parse::<usize>() {
            Ok(n) if n >= 1 && n <= SOCK_MAX_BYTES => n,
            _ => {
                return err(
                    400,
                    &format!("Invalid max_bytes param (1-{SOCK_MAX_BYTES})."),
                    None,
                )
            }
        },
    };
    let sid = match req.query.get("id") {
        Some(s) if !s.is_empty() => s.clone(),
        _ => return err(400, "Missing required query parameter: id", None),
    };
    // Claim the session for this recv (serializes concurrent recvs).
    let arc = {
        let mut map = sessions().lock().unwrap();
        let sess = match map.get_mut(&sid) {
            Some(s) => s,
            None => return err(404, &format!("Unknown socket session: {sid}"), None),
        };
        sess.last = Instant::now();
        if sess.ws_attached {
            return err(
                400,
                "Socket session is attached to a websocket (HTTP recv refused while attached).",
                None,
            );
        }
        if sess.eof {
            let mut o = serde_json::Map::new();
            o.insert("id".to_string(), jstr(&sid));
            o.insert("encoding".to_string(), jstr("base64"));
            o.insert("content".to_string(), jstr(""));
            o.insert("bytes".to_string(), jstr(&jenc_int(0)));
            o.insert("eof".to_string(), jstr("b:true"));
            o.insert("timed_out".to_string(), jstr("b:false"));
            return send_json_obj(200, &o);
        }
        if sess.recv_busy {
            return err(400, "Another recv is already pending on this session.", None);
        }
        sess.recv_busy = true;
        sess.stream.clone()
    };
    let mut buf = vec![0u8; max_bytes];
    let outcome = {
        let mut guard = arc.lock().unwrap();
        let _ = guard.set_read_timeout(Some(Duration::from_secs_f64(timeout)));
        let r = guard.read(&mut buf);
        let _ = guard.set_read_timeout(None);
        r
    };
    let mut map = sessions().lock().unwrap();
    let release = |map: &mut HashMap<String, Session>| {
        if let Some(sess) = map.get_mut(&sid) {
            sess.recv_busy = false;
            sess.last = Instant::now();
        }
    };
    match outcome {
        Ok(n) if n > 0 => {
            release(&mut map);
            let mut o = serde_json::Map::new();
            o.insert("id".to_string(), jstr(&sid));
            o.insert("encoding".to_string(), jstr("base64"));
            o.insert("content".to_string(), jstr(&crate::codec::b64_encode(&buf[..n])));
            o.insert("bytes".to_string(), jstr(&jenc_int(n as i64)));
            o.insert("eof".to_string(), jstr("b:false"));
            o.insert("timed_out".to_string(), jstr("b:false"));
            send_json_obj(200, &o)
        }
        Ok(_) => {
            if let Some(sess) = map.get_mut(&sid) {
                sess.eof = true;
                sess.recv_busy = false;
                sess.last = Instant::now();
            }
            let mut o = serde_json::Map::new();
            o.insert("id".to_string(), jstr(&sid));
            o.insert("encoding".to_string(), jstr("base64"));
            o.insert("content".to_string(), jstr(""));
            o.insert("bytes".to_string(), jstr(&jenc_int(0)));
            o.insert("eof".to_string(), jstr("b:true"));
            o.insert("timed_out".to_string(), jstr("b:false"));
            send_json_obj(200, &o)
        }
        Err(e) if e.kind() == std::io::ErrorKind::TimedOut
            || e.kind() == std::io::ErrorKind::WouldBlock =>
        {
            release(&mut map);
            let mut o = serde_json::Map::new();
            o.insert("id".to_string(), jstr(&sid));
            o.insert("encoding".to_string(), jstr("base64"));
            o.insert("content".to_string(), jstr(""));
            o.insert("bytes".to_string(), jstr(&jenc_int(0)));
            o.insert("eof".to_string(), jstr("b:false"));
            o.insert("timed_out".to_string(), jstr("b:true"));
            send_json_obj(200, &o)
        }
        Err(e) => {
            release(&mut map);
            err(400, &format!("Socket error: {e}"), None)
        }
    }
}

pub fn handle_close(req: &Request) -> Response {
    let sid = match req.query.get("id") {
        Some(s) if !s.is_empty() => s.clone(),
        _ => return err(400, "Missing required query parameter: id", None),
    };
    let removed = {
        let mut map = sessions().lock().unwrap();
        map.remove(&sid)
    };
    match removed {
        Some(sess) => {
            let _ = sess.killer.shutdown(std::net::Shutdown::Both);
            let mut o = serde_json::Map::new();
            o.insert("id".to_string(), jstr(&sid));
            o.insert("closed".to_string(), jstr("b:true"));
            send_json_obj(200, &o)
        }
        None => err(404, &format!("Unknown socket session: {sid}"), None),
    }
}

// ---------------------------------------------------------------------------
// WebSocket attach primitives (state lives here; framing in socket_ws).
// ---------------------------------------------------------------------------

/// One end of a WS bridge. Plain sessions hand out independent
/// dup'd fds (as before); TLS sessions share the single SslStream
/// behind a mutex — reads use a short timeout so a blocked pump
/// never pins the lock out from under the writer.
pub(crate) enum PeerStream {
    Plain(TcpStream),
    Tls(TlsHalf),
}

pub(crate) struct TlsHalf {
    shared: Arc<Mutex<Stream>>,
}

fn lock_err() -> std::io::Error {
    std::io::Error::other("stream lock unavailable")
}

impl Read for PeerStream {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        match self {
            PeerStream::Plain(s) => s.read(buf),
            PeerStream::Tls(h) => h
                .shared
                .lock()
                .map_err(|_| lock_err())?
                .read(buf),
        }
    }
}

impl Write for PeerStream {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        match self {
            PeerStream::Plain(s) => s.write(buf),
            PeerStream::Tls(h) => h
                .shared
                .lock()
                .map_err(|_| lock_err())?
                .write(buf),
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            PeerStream::Plain(s) => s.flush(),
            PeerStream::Tls(h) => h
                .shared
                .lock()
                .map_err(|_| lock_err())?
                .flush(),
        }
    }
}

impl PeerStream {
    pub(crate) fn set_write_timeout(&self, d: Option<Duration>) -> std::io::Result<()> {
        match self {
            PeerStream::Plain(s) => s.set_write_timeout(d),
            PeerStream::Tls(h) => h
                .shared
                .lock()
                .map_err(|_| lock_err())?
                .set_write_timeout(d),
        }
    }
}

/// Claimed ends for one WS bridge: pump reads peer_rd, main writes peer_wr.
pub(crate) struct WsAttach {
    pub sid: String,
    pub peer_rd: PeerStream,
    pub peer_wr: PeerStream,
}

/// Attach a WebSocket to a session: refuses unknown ids (404),
/// double attach and in-flight HTTP recv (400). Marks attached.
pub(crate) fn ws_attach(sid: &str) -> Result<WsAttach, Response> {
    let mut map = sessions().lock().unwrap();
    let sess = match map.get_mut(sid) {
        Some(s) => s,
        None => {
            return Err(err(
                404,
                &format!("Unknown socket session: {sid}"),
                None,
            ))
        }
    };
    sess.last = Instant::now();
    if sess.ws_attached {
        return Err(err(
            400,
            "Socket session is already attached to a websocket.",
            None,
        ));
    }
    if sess.recv_busy {
        return Err(err(
            400,
            "Another recv is already pending on this session.",
            None,
        ));
    }
    // Clear any stale socket-level timeouts left by HTTP recvs (they
    // are shared across dup'd fds); the bridge manages its own.
    let arc = sess.stream.clone();
    let (peer_rd, peer_wr) = {
        let guard = arc.lock().unwrap();
        match &*guard {
            Stream::Plain(s) => {
                let rd = s.try_clone();
                let wr = s.try_clone();
                match (rd, wr) {
                    (Ok(rd), Ok(wr)) => {
                        let _ = rd.set_read_timeout(None);
                        let _ = rd.set_write_timeout(None);
                        (PeerStream::Plain(rd), PeerStream::Plain(wr))
                    }
                    _ => {
                        return Err(err(400, "Socket error: dup failed.", None));
                    }
                }
            }
            Stream::Tls(_) => {
                let _ = guard.set_read_timeout(Some(Duration::from_millis(100)));
                let _ = guard.set_write_timeout(None);
                (
                    PeerStream::Tls(TlsHalf {
                        shared: arc.clone(),
                    }),
                    PeerStream::Tls(TlsHalf {
                        shared: arc.clone(),
                    }),
                )
            }
        }
    };
    sess.ws_attached = true;
    Ok(WsAttach {
        sid: sid.to_string(),
        peer_rd,
        peer_wr,
    })
}

pub(crate) fn ws_touch(sid: &str) {
    let mut map = sessions().lock().unwrap();
    if let Some(sess) = map.get_mut(sid) {
        sess.last = Instant::now();
    }
}

pub(crate) fn ws_mark_eof(sid: &str) {
    let mut map = sessions().lock().unwrap();
    if let Some(sess) = map.get_mut(sid) {
        sess.eof = true;
        sess.last = Instant::now();
    }
}

/// End a WS-bridged session: delete + shutdown the peer (shutdown is
/// socket-level, so it unblocks every cloned reader). Idempotent.
pub(crate) fn ws_end(sid: &str) {
    let removed = {
        let mut map = sessions().lock().unwrap();
        map.remove(sid)
    };
    if let Some(sess) = removed {
        let _ = sess.killer.shutdown(std::net::Shutdown::Both);
    }
}

/// Release a recv-busy claim (STARTTLS paths).
fn release_busy(sid: &str) {
    let mut map = sessions().lock().unwrap();
    if let Some(sess) = map.get_mut(sid) {
        sess.recv_busy = false;
        sess.last = Instant::now();
    }
}

fn starttls_body(req: &Request) -> Result<serde_json::Map<String, serde_json::Value>, Response> {
    if req.body.is_empty() {
        return Ok(serde_json::Map::new());
    }
    let ct = req.headers.get("content-type").map(|s| s.as_str()).unwrap_or("");
    if !ct.contains("application/json") {
        return Err(err(
            400,
            "Content-Type must be application/json with body { sni?, ca_pem? }.",
            None,
        ));
    }
    match serde_json::from_slice::<serde_json::Value>(&req.body) {
        Ok(serde_json::Value::Object(m)) => Ok(m),
        Ok(_) => Err(err(400, "JSON body must be an object.", None)),
        Err(_) => Err(err(400, "Invalid JSON body.", None)),
    }
}

/// Upgrade a plaintext session to TLS in place (STARTTLS-style).
/// A failed handshake leaves the plaintext session untouched.
pub fn handle_starttls(req: &Request) -> Response {
    let sid = match req.query.get("id") {
        Some(s) if !s.is_empty() => s.clone(),
        _ => return err(400, "Missing required query parameter: id", None),
    };
    let (host, port) = {
        let mut map = sessions().lock().unwrap();
        let sess = match map.get_mut(&sid) {
            Some(s) => s,
            None => return err(404, &format!("Unknown socket session: {sid}"), None),
        };
        sess.last = Instant::now();
        if sess.ws_attached {
            return err(
                400,
                "Socket session is attached to a websocket (STARTTLS refused while attached).",
                None,
            );
        }
        if sess.kind == "tls" {
            return err(400, "Socket session is already TLS.", None);
        }
        if sess.recv_busy {
            return err(400, "Another recv is already pending on this session.", None);
        }
        sess.recv_busy = true;
        (sess.host.clone(), sess.port)
    };
    let obj = match starttls_body(req) {
        Ok(o) => o,
        Err(r) => {
            release_busy(&sid);
            return r;
        }
    };
    let (mut sni, ca_pem) = match parse_tls_identity(&obj) {
        Ok(v) => v,
        Err(r) => {
            release_busy(&sid);
            return r;
        }
    };
    if sni.is_empty() {
        sni = host.clone();
    }
    let connector = match tls_connector(&ca_pem) {
        Ok(c) => c,
        Err(r) => {
            release_busy(&sid);
            return r;
        }
    };
    let plain = {
        let map = sessions().lock().unwrap();
        match map.get(&sid) {
            Some(sess) => match sess.stream.lock() {
                Ok(guard) => guard.plain_dup(),
                Err(_) => None,
            },
            None => None,
        }
    };
    let plain = match plain {
        Some(s) => s,
        None => {
            release_busy(&sid);
            return err(404, &format!("Unknown socket session: {sid}"), None);
        }
    };
    let tls = match tls_handshake(
        plain,
        &sni,
        &connector,
        Duration::from_secs_f64(SOCK_MAX_CONNECT_TIMEOUT),
    ) {
        Ok(t) => t,
        Err(e) => {
            release_busy(&sid);
            return err(
                400,
                &format!("TLS handshake with {host}:{port} failed: {e}"),
                None,
            );
        }
    };
    let killer = tls
        .get_ref()
        .try_clone()
        .ok();
    {
        let mut map = sessions().lock().unwrap();
        match map.get_mut(&sid) {
            Some(sess) if sess.kind != "tls" => {
                if let Some(k) = killer {
                    sess.killer = k;
                }
                *sess.stream.lock().unwrap() = Stream::Tls(tls);
                sess.kind = "tls".to_string();
                sess.recv_busy = false;
                sess.last = Instant::now();
            }
            _ => {
                return err(404, &format!("Unknown socket session: {sid}"), None);
            }
        }
    }
    let mut o = serde_json::Map::new();
    o.insert("id".to_string(), jstr(&sid));
    o.insert("kind".to_string(), jstr("tls"));
    send_json_obj(200, &o)
}
