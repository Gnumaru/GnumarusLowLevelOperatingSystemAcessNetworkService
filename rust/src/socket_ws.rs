//! WebSocket (RFC 6455) transport for socket sessions.
//!
//! GET /api/socket/ws?id=<sid> with `Upgrade: websocket` attaches a
//! WebSocket to an existing session. After the 101 handshake, WS
//! message payloads flow transparently client->TCP (text frames as
//! UTF-8 bytes) and TCP bytes flow back as binary frames; ping gets
//! an auto-pong. A close from either side, or any TCP/protocol error,
//! ends the whole session (a WS close is a session close, like HTTP
//! close). Stdlib only: hand-rolled SHA-1 (for the accept key) plus
//! framing over the hijacked TcpStream, no new dependencies.

use crate::api::{err, State};
use crate::http::{Request, Response};
use std::io::{Read, Write};
use std::net::{Shutdown, TcpStream};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::Duration;

pub const WS_MAX_MESSAGE: usize = 4 * 1024 * 1024;
const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

const WS_CONT: u8 = 0x0;
const WS_TEXT: u8 = 0x1;
const WS_BIN: u8 = 0x2;
const WS_CLOSE: u8 = 0x8;
const WS_PING: u8 = 0x9;
const WS_PONG: u8 = 0xA;

// ---------------------------------------------------------------------------
// SHA-1 (FIPS 180-4, for the accept key only)
// ---------------------------------------------------------------------------

fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [
        0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0,
    ];
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());
    for block in msg.chunks_exact(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                block[4 * i],
                block[4 * i + 1],
                block[4 * i + 2],
                block[4 * i + 3],
            ]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, &word) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let tmp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = tmp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    let mut out = [0u8; 20];
    for (i, v) in h.iter().enumerate() {
        out[4 * i..4 * i + 4].copy_from_slice(&v.to_be_bytes());
    }
    out
}

fn ws_accept_key(key: &str) -> String {
    crate::codec::b64_encode(&sha1(format!("{key}{WS_GUID}").as_bytes()))
}

fn ws_has_token(header_val: Option<&String>, token: &str) -> bool {
    match header_val {
        None => false,
        Some(v) => v.split(',').any(|p| p.trim().eq_ignore_ascii_case(token)),
    }
}

// ---------------------------------------------------------------------------
// framing
// ---------------------------------------------------------------------------

struct Shared {
    /// Every WS write from both threads goes through here.
    wwr: Mutex<TcpStream>,
    closed: Mutex<bool>, // close frame already sent
    main_done: AtomicBool,
}

fn ws_send_frame(shared: &Shared, opcode: u8, payload: &[u8]) -> bool {
    let mut wr = match shared.wwr.lock() {
        Ok(g) => g,
        Err(_) => return false,
    };
    if *shared.closed.lock().unwrap_or_else(|e| e.into_inner()) {
        return false;
    }
    let mut hdr = vec![0x80 | opcode];
    if payload.len() < 126 {
        hdr.push(payload.len() as u8);
    } else if payload.len() < 65536 {
        hdr.push(126);
        hdr.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    } else {
        hdr.push(127);
        hdr.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    }
    wr.write_all(&hdr).is_ok() && wr.write_all(payload).is_ok()
}

fn ws_send_close(shared: &Shared, code: u16, reason: &[u8]) {
    let mut wr = match shared.wwr.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    {
        let mut c = shared.closed.lock().unwrap_or_else(|e| e.into_inner());
        if *c {
            return;
        }
        *c = true;
    }
    let mut payload = Vec::with_capacity(2 + reason.len());
    payload.extend_from_slice(&code.to_be_bytes());
    payload.extend_from_slice(reason);
    let mut hdr = vec![WS_CLOSE | 0x80];
    hdr.push(payload.len() as u8); // close payloads here are tiny
    let _ = wr.write_all(&hdr);
    let _ = wr.write_all(&payload);
}

struct Frame {
    fin: bool,
    opcode: u8,
    payload: Vec<u8>,
    is_ctl: bool,
}

struct ProtoFail(u16);

fn read_frame(rd: &mut TcpStream) -> Result<Frame, Option<ProtoFail>> {
    let mut hdr = [0u8; 2];
    rd.read_exact(&mut hdr).map_err(|_| None)?;
    let fin = hdr[0] & 0x80 != 0;
    let opcode = hdr[0] & 0x0F;
    let rsv = hdr[0] & 0x70 != 0;
    let masked = hdr[1] & 0x80 != 0;
    let mut length = (hdr[1] & 0x7F) as u64;
    if length == 126 {
        let mut ext = [0u8; 2];
        rd.read_exact(&mut ext).map_err(|_| None)?;
        length = u16::from_be_bytes(ext) as u64;
    } else if length == 127 {
        let mut ext = [0u8; 8];
        rd.read_exact(&mut ext).map_err(|_| None)?;
        length = u64::from_be_bytes(ext);
        if length > WS_MAX_MESSAGE as u64 {
            return Err(Some(ProtoFail(1009))); // abortive: cannot bound
        }
    }
    let is_ctl = opcode >= 0x8;
    if length > WS_MAX_MESSAGE as u64 {
        return Err(Some(ProtoFail(1009))); // abortive: cannot bound
    }
    // Unmasked client frames carry no key: consume payload only.
    let mut payload = vec![0u8; length as usize];
    if masked {
        let mut key = [0u8; 4];
        rd.read_exact(&mut key).map_err(|_| None)?;
        rd.read_exact(&mut payload).map_err(|_| None)?;
        for (i, b) in payload.iter_mut().enumerate() {
            *b ^= key[i % 4];
        }
    } else {
        rd.read_exact(&mut payload).map_err(|_| None)?;
    }
    if rsv {
        return Err(Some(ProtoFail(1002)));
    }
    if !masked {
        return Err(Some(ProtoFail(1002))); // clients MUST mask
    }
    if is_ctl && (!fin || length > 125) {
        return Err(Some(ProtoFail(1002)));
    }
    Ok(Frame {
        fin,
        opcode,
        payload,
        is_ctl,
    })
}

enum Inbound {
    Msg(u8, Vec<u8>),
    Ping(Vec<u8>),
    Closed(u16, Vec<u8>),
}

/// Reassemble one data message; pongs are swallowed. Protocol
/// violations echo inline (session doomed). A clean close returns
/// Closed WITHOUT echoing: the caller ends the session first, so the
/// echo implies "gone".
fn read_message(rd: &mut TcpStream) -> Result<Inbound, Option<ProtoFail>> {
    let mut msg_op: Option<u8> = None;
    let mut msg: Vec<u8> = Vec::new();
    loop {
        let fr = read_frame(rd)?;
        if fr.is_ctl {
            match fr.opcode {
                WS_CLOSE => {
                    if fr.payload.len() == 1 {
                        return Err(Some(ProtoFail(1002)));
                    }
                    let (code, reason) = if fr.payload.len() >= 2 {
                        (
                            u16::from_be_bytes([fr.payload[0], fr.payload[1]]),
                            fr.payload[2..].to_vec(),
                        )
                    } else {
                        (1000, Vec::new())
                    };
                    return Ok(Inbound::Closed(code, reason));
                }
                WS_PING => return Ok(Inbound::Ping(fr.payload)),
                WS_PONG => continue,
                _ => return Err(Some(ProtoFail(1002))),
            }
        }
        // Note: ping needs an immediate pong even mid-message; the
        // caller handles it — but read_message owns the loop, so
        // answer here via a callback-free path: return it up.
        // (Handled below by restructuring: pings surface as events.)
        if fr.opcode == WS_CONT {
            if msg_op.is_none() {
                return Err(Some(ProtoFail(1002)));
            }
        } else if fr.opcode == WS_TEXT || fr.opcode == WS_BIN {
            if msg_op.is_some() {
                return Err(Some(ProtoFail(1002)));
            }
            msg_op = Some(fr.opcode);
        } else {
            return Err(Some(ProtoFail(1003)));
        }
        if msg.len() + fr.payload.len() > WS_MAX_MESSAGE {
            return Err(Some(ProtoFail(1009)));
        }
        msg.extend_from_slice(&fr.payload);
        if fr.fin {
            let op = msg_op.unwrap_or(WS_BIN);
            if op == WS_TEXT && std::str::from_utf8(&msg).is_err() {
                return Err(Some(ProtoFail(1007)));
            }
            return Ok(Inbound::Msg(op, msg));
        }
    }
}

// ---------------------------------------------------------------------------
// bridge + upgrade
// ---------------------------------------------------------------------------

fn phrase(status: u16) -> &'static str {
    match status {
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        _ => "Error",
    }
}

fn write_err(stream: &mut TcpStream, res: Response) {
    let mut head = format!("HTTP/1.1 {} {}\r\n", res.status, phrase(res.status));
    for (k, v) in &res.headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str(&format!("Content-Length: {}\r\n\r\n", res.body.len()));
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(&res.body);
    let _ = stream.flush();
}

/// True only for upgrade attempts to our route (plain GETs fall
/// through to the normal dispatcher, which answers 400).
pub fn is_ws_upgrade_request(req: &Request) -> bool {
    req.method == "GET"
        && req.path == "/api/socket/ws"
        && ws_has_token(req.headers.get("connection"), "upgrade")
}

pub fn handle_upgrade(mut stream: TcpStream, req: Request, state: &State) {
    if req.path != "/api/socket/ws" {
        let res = err(404, &format!("Unknown route: {}", req.path), None);
        write_err(&mut stream, res);
        return;
    }
    if let Some(denied) = crate::api::check_auth(&req, state) {
        write_err(&mut stream, denied);
        return;
    }
    let upgrade_ok = req
        .headers
        .get("upgrade")
        .map(|v| v.trim().eq_ignore_ascii_case("websocket"))
        .unwrap_or(false);
    if !upgrade_ok {
        let res = err(
            400,
            "This route requires a WebSocket upgrade (Upgrade: websocket).",
            None,
        );
        write_err(&mut stream, res);
        return;
    }
    if req
        .headers
        .get("sec-websocket-version")
        .map(|v| v.trim())
        != Some("13")
    {
        let res = err(400, "Unsupported WebSocket version (need 13).", None);
        write_err(&mut stream, res);
        return;
    }
    let key = req
        .headers
        .get("sec-websocket-key")
        .map(|v| v.trim().to_string())
        .unwrap_or_default();
    if key.is_empty() {
        let res = err(400, "Missing Sec-WebSocket-Key.", None);
        write_err(&mut stream, res);
        return;
    }
    let sid = match req.query.get("id") {
        Some(s) if !s.is_empty() => s.clone(),
        _ => {
            let res = err(400, "Missing required query parameter: id", None);
            write_err(&mut stream, res);
            return;
        }
    };
    let mut attach = match crate::socket::ws_attach(&sid) {
        Ok(a) => a,
        Err(res) => {
            write_err(&mut stream, res);
            return;
        }
    };
    let resp = format!(
        "HTTP/1.1 101 Switching Protocols\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Accept: {}\r\n\r\n",
        ws_accept_key(&key)
    );
    if stream.write_all(resp.as_bytes()).is_err() || stream.flush().is_err() {
        crate::socket::ws_end(&attach.sid);
        return;
    }
    // Bounded blocking writes from here on; reads block indefinitely
    // (the pump's grace shutdown unblocks the main reader at the end).
    let _ = stream.set_write_timeout(Some(Duration::from_secs(30)));
    let _ = stream.set_read_timeout(None);

    let shared = Arc::new(Shared {
        wwr: Mutex::new(match stream.try_clone() {
            Ok(s) => s,
            Err(_) => {
                crate::socket::ws_end(&attach.sid);
                return;
            }
        }),
        closed: Mutex::new(false),
        main_done: AtomicBool::new(false),
    });

    // --- TCP -> WS pump ---
    let pump_shared = shared.clone();
    let pump_sid = attach.sid.clone();
    let mut pump_peer = attach.peer_rd;
    let pump = std::thread::spawn(move || {
        let mut buf = vec![0u8; 32768];
        loop {
            match pump_peer.read(&mut buf) {
                Ok(n) if n > 0 => {
                    crate::socket::ws_touch(&pump_sid);
                    if !ws_send_frame(&pump_shared, WS_BIN, &buf[..n]) {
                        return;
                    }
                }
                Ok(_) => {
                    crate::socket::ws_mark_eof(&pump_sid);
                    ws_send_close(&pump_shared, 1000, &[]);
                    break;
                }
                Err(e)
                    if e.kind() == std::io::ErrorKind::TimedOut
                        || e.kind() == std::io::ErrorKind::WouldBlock =>
                {
                    // TLS halves poll with a short read timeout so a
                    // blocked pump never pins the shared stream.
                    continue;
                }
                Err(_) => {
                    ws_send_close(&pump_shared, 1011, &[]);
                    break;
                }
            }
        }
        // TCP ended: give the main reader a grace period to see the
        // close echo round-trip, then force it out so nothing lingers.
        for _ in 0..20 {
            if pump_shared.main_done.load(Ordering::SeqCst) {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        if let Ok(wr) = pump_shared.wwr.lock() {
            let _ = wr.shutdown(Shutdown::Read);
        }
    });

    // --- WS -> TCP reader (this connection thread) ---
    let mut ws_rd = stream;
    let send = |opcode: u8, payload: &[u8]| ws_send_frame(&shared, opcode, payload);
    let end_session_first = || crate::socket::ws_end(&attach.sid);
    loop {
        match read_message(&mut ws_rd) {
            Ok(Inbound::Msg(_, payload)) => {
                attach.peer_wr.set_write_timeout(Some(Duration::from_secs(30))).ok();
                let werr = attach.peer_wr.write_all(&payload).err();
                attach.peer_wr.set_write_timeout(None).ok();
                if werr.is_some() {
                    end_session_first();
                    break;
                }
                crate::socket::ws_touch(&attach.sid);
            }
            Ok(Inbound::Ping(payload)) => {
                if !send(WS_PONG, &payload) {
                    end_session_first();
                    break;
                }
            }
            Ok(Inbound::Closed(code, reason)) => {
                // Delete BEFORE echo: the echo implies "gone".
                end_session_first();
                ws_send_close(&shared, code, &reason);
                break;
            }
            Err(Some(ProtoFail(code))) => {
                // Protocol violation: echo (session already doomed),
                // then end it.
                ws_send_close(&shared, code, &[]);
                end_session_first();
                break;
            }
            Err(None) => {
                end_session_first();
                break;
            }
        }
    }
    shared.main_done.store(true, Ordering::SeqCst);
    let _ = pump.join();
}
