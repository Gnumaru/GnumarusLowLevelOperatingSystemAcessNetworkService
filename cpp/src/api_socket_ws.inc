// WebSocket (RFC 6455) transport for socket sessions (included by
// api.cpp; uses its includes and shared helpers).
//
// GET /api/socket/ws?id=<sid> with `Upgrade: websocket` attaches a
// WebSocket to an existing session. After the 101 handshake, WS
// message payloads flow transparently client->TCP (text frames as
// UTF-8 bytes) and TCP bytes flow back as binary frames; ping gets
// an auto-pong. A close from either side, or any TCP/protocol error,
// ends the whole session (a WS close is a session close, like HTTP
// close). Stdlib + POSIX only: hand-rolled SHA-1 plus framing over
// the hijacked fd, no new dependencies.
//
// fd discipline (raw fds MUST close exactly once — no refcounting):
// the WS client fd closes only in the bridge cleanup (call_once);
// the peer fd closes only in sock_ws_end_session (remove-if-present).
// shutdown() is safe to call any number of times and unblocks
// readers without releasing the number.

namespace sockws {

const char* kWsGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const size_t kWsMaxMessage = 4 * 1024 * 1024;

const uint8_t kWsCont = 0x0;
const uint8_t kWsText = 0x1;
const uint8_t kWsBin = 0x2;
const uint8_t kWsClose = 0x8;
const uint8_t kWsPing = 0x9;
const uint8_t kWsPong = 0xA;

// --- SHA-1 (FIPS 180-4, accept key only) ------------------------------------

std::array<uint8_t, 20> ws_sha1(const std::string& data) {
    uint32_t h[5] = {0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476,
                     0xC3D2E1F0};
    std::string msg = data;
    uint64_t bit_len = (uint64_t)data.size() * 8;
    msg.push_back((char)0x80);
    while (msg.size() % 64 != 56) msg.push_back(0);
    for (int i = 7; i >= 0; --i) msg.push_back((char)(bit_len >> (8 * i)));
    for (size_t off = 0; off < msg.size(); off += 64) {
        uint32_t w[80];
        for (int i = 0; i < 16; ++i) {
            w[i] = ((uint32_t)(uint8_t)msg[off + 4 * i] << 24) |
                   ((uint32_t)(uint8_t)msg[off + 4 * i + 1] << 16) |
                   ((uint32_t)(uint8_t)msg[off + 4 * i + 2] << 8) |
                   (uint32_t)(uint8_t)msg[off + 4 * i + 3];
        }
        for (int i = 16; i < 80; ++i) {
            uint32_t v = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16];
            w[i] = (v << 1) | (v >> 31);
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
        for (int i = 0; i < 80; ++i) {
            uint32_t f, k;
            if (i < 20) {
                f = (b & c) | (~b & d);
                k = 0x5A827999;
            } else if (i < 40) {
                f = b ^ c ^ d;
                k = 0x6ED9EBA1;
            } else if (i < 60) {
                f = (b & c) | (b & d) | (c & d);
                k = 0x8F1BBCDC;
            } else {
                f = b ^ c ^ d;
                k = 0xCA62C1D6;
            }
            uint32_t tmp = ((a << 5) | (a >> 27)) + f + e + k + w[i];
            e = d;
            d = c;
            c = (b << 30) | (b >> 2);
            b = a;
            a = tmp;
        }
        h[0] += a;
        h[1] += b;
        h[2] += c;
        h[3] += d;
        h[4] += e;
    }
    std::array<uint8_t, 20> out{};
    for (int i = 0; i < 5; ++i) {
        out[4 * i] = (h[i] >> 24) & 0xFF;
        out[4 * i + 1] = (h[i] >> 16) & 0xFF;
        out[4 * i + 2] = (h[i] >> 8) & 0xFF;
        out[4 * i + 3] = h[i] & 0xFF;
    }
    return out;
}

std::string ws_accept_key(const std::string& key) {
    auto digest = ws_sha1(key + kWsGuid);
    return b64_encode(
        std::string(reinterpret_cast<const char*>(digest.data()), digest.size()));
}

bool ws_has_token(const std::string& header_val, const char* token) {
    size_t i = 0;
    while (i <= header_val.size()) {
        size_t comma = header_val.find(',', i);
        if (comma == std::string::npos) comma = header_val.size();
        std::string part = header_val.substr(i, comma - i);
        size_t a = part.find_first_not_of(" \t");
        size_t b = part.find_last_not_of(" \t");
        std::string t = (a == std::string::npos) ? "" : part.substr(a, b - a + 1);
        for (auto& ch : t) ch = std::tolower((unsigned char)ch);
        if (t == token) return true;
        i = comma + 1;
    }
    return false;
}

// Strict UTF-8 (reject overlongs/surrogates/out-of-range).
bool ws_valid_utf8(const std::string& s) {
    size_t i = 0;
    while (i < s.size()) {
        unsigned c = (unsigned char)s[i];
        size_t size = 0;
        uint32_t cp = 0;
        if (c < 0x80) {
            size = 1;
            cp = c;
        } else if ((c >> 5) == 0x6) {
            size = 2;
            cp = c & 0x1F;
        } else if ((c >> 4) == 0xE) {
            size = 3;
            cp = c & 0x0F;
        } else if ((c >> 3) == 0x1E) {
            size = 4;
            cp = c & 0x07;
        } else {
            return false;
        }
        if (i + size > s.size()) return false;
        for (size_t j = 1; j < size; ++j) {
            unsigned d = (unsigned char)s[i + j];
            if ((d >> 6) != 0x2) return false;
            cp = (cp << 6) | (d & 0x3F);
        }
        if ((size == 2 && cp < 0x80) || (size == 3 && cp < 0x800) ||
            (size == 4 && cp < 0x10000) || cp > 0x10FFFF ||
            (cp >= 0xD800 && cp <= 0xDFFF))
            return false;
        i += size;
    }
    return true;
}

// --- bridge ------------------------------------------------------------------

struct WsBridge {
    int ws_fd = -1;     // client connection (hijacked)
    int peer_fd = -1;   // proxied TCP session socket
    SSL* peer_ssl = nullptr;  // TLS layer over peer_fd (null = plaintext)
    std::string sid;
    std::mutex wmu;     // serializes every WS write
    bool closed = false;  // close frame already sent (under wmu)
    std::atomic<bool> main_done{false};
    std::once_flag cleanup_once;
};

bool ws_send_all(int fd, const char* data, size_t len) {
    while (len > 0) {
        ssize_t n = send(fd, data, len, MSG_NOSIGNAL);
        if (n <= 0) {
            if (errno == EINTR) continue;
            return false;
        }
        data += n;
        len -= (size_t)n;
    }
    return true;
}

bool ws_send_frame(const std::shared_ptr<WsBridge>& br, uint8_t opcode,
                   const std::string& payload) {
    std::lock_guard<std::mutex> lock(br->wmu);
    if (br->closed) return false;
    std::string hdr;
    hdr.push_back((char)(0x80 | opcode));
    if (payload.size() < 126) {
        hdr.push_back((char)payload.size());
    } else if (payload.size() < 65536) {
        hdr.push_back(126);
        hdr.push_back((char)(payload.size() >> 8));
        hdr.push_back((char)(payload.size() & 0xFF));
    } else {
        hdr.push_back(127);
        uint64_t len = payload.size();
        for (int i = 7; i >= 0; --i) hdr.push_back((char)(len >> (8 * i)));
    }
    return ws_send_all(br->ws_fd, hdr.data(), hdr.size()) &&
           (payload.empty() ||
            ws_send_all(br->ws_fd, payload.data(), payload.size()));
}

void ws_send_close(const std::shared_ptr<WsBridge>& br, uint16_t code,
                   const std::string& reason) {
    std::lock_guard<std::mutex> lock(br->wmu);
    if (br->closed) return;
    br->closed = true;
    std::string payload;
    payload.push_back((char)(code >> 8));
    payload.push_back((char)(code & 0xFF));
    payload += reason;
    std::string frame;
    frame.push_back((char)(kWsClose | 0x80));
    frame.push_back((char)payload.size());  // close payloads here are tiny
    ws_send_all(br->ws_fd, frame.data(), frame.size());
    ws_send_all(br->ws_fd, payload.data(), payload.size());
}

bool ws_read_exact(int fd, char* out, size_t len) {
    while (len > 0) {
        ssize_t n = recv(fd, out, len, 0);
        if (n <= 0) return false;
        out += n;
        len -= (size_t)n;
    }
    return true;
}

struct WsFrame {
    bool fin = false;
    uint8_t opcode = 0;
    std::string payload;
    bool is_ctl = false;
};

// Returns true with a frame, or false on transport error. On a
// bounded protocol violation the frame is fully consumed and vcode
// carries the fail echo (clean FIN); only unbounded declarations
// abort with vcode set and nothing consumed.
bool ws_read_frame(int fd, WsFrame& fr, uint16_t& vcode) {
    vcode = 0;
    char hdr[2];
    if (!ws_read_exact(fd, hdr, 2)) return false;
    uint8_t b0 = hdr[0], b1 = hdr[1];
    bool rsv = (b0 & 0x70) != 0;
    bool masked = (b1 & 0x80) != 0;
    uint64_t length = b1 & 0x7F;
    if (length == 126) {
        char ext[2];
        if (!ws_read_exact(fd, ext, 2)) return false;
        length = ((uint64_t)(uint8_t)ext[0] << 8) | (uint8_t)ext[1];
    } else if (length == 127) {
        char ext[8];
        if (!ws_read_exact(fd, ext, 8)) return false;
        length = 0;
        for (int i = 0; i < 8; ++i)
            length = (length << 8) | (uint8_t)ext[i];
        if (length > kWsMaxMessage) {
            vcode = 1009;
            return true;  // abortive: cannot bound
        }
    }
    bool is_ctl = (b0 & 0x0F) >= 0x8;
    if (length > kWsMaxMessage) {
        vcode = 1009;  // abortive: cannot bound
        return true;
    }
    std::string payload;
    payload.resize((size_t)length);
    if (masked) {
        char key[4];
        if (!ws_read_exact(fd, key, 4)) return false;
        if (!ws_read_exact(fd, payload.data(), payload.size())) return false;
        for (size_t i = 0; i < payload.size(); ++i)
            payload[i] ^= key[i % 4];
    } else {
        // Unmasked client frames carry no key: consume payload only.
        if (!ws_read_exact(fd, payload.data(), payload.size())) return false;
    }
    if (rsv) {
        vcode = 1002;
        return true;
    }
    if (!masked) {
        vcode = 1002;  // clients MUST mask
        return true;
    }
    if (is_ctl && (!((b0 & 0x80) != 0) || length > 125)) {
        vcode = 1002;
        return true;
    }
    fr.fin = (b0 & 0x80) != 0;
    fr.opcode = b0 & 0x0F;
    fr.payload = std::move(payload);
    fr.is_ctl = is_ctl;
    return true;
}

}  // namespace sockws

namespace sockws {

// --- message assembly ----------------------------------------------------------

enum class WsInbound { Msg, Ping, Closed, Error };

struct WsMsg {
    WsInbound kind = WsInbound::Error;
    uint8_t opcode = 0;
    std::string payload;
    uint16_t code = 0;  // fail echo (Error) or peer echo (Closed)
};

// Reassemble one data message; pongs swallowed. Violations echo
// inline (session doomed). Clean close returns Closed WITHOUT
// echoing: the caller ends the session first, so the echo implies
// "gone". Error returns code 0 (transport) or the fail echo.
WsMsg ws_read_message(int fd) {
    WsMsg out;
    bool started = false;
    uint8_t msg_op = 0;
    std::string msg;
    while (true) {
        WsFrame fr;
        uint16_t vcode = 0;
        if (!ws_read_frame(fd, fr, vcode)) {
            out.kind = WsInbound::Error;
            out.code = 0;
            return out;
        }
        if (vcode != 0) {
            // Frame fully consumed: graceful echo, then report.
            WsMsg m;
            m.kind = WsInbound::Error;
            m.code = vcode;
            // Echo here (session doomed; no HTTP-state test follows).
            // The echo needs a bridge; the caller owns it — signal
            // via code and let the caller echo. Fall through: the
            // caller echoes vcode for Error.
            return m;
        }
        if (fr.is_ctl) {
            if (fr.opcode == kWsClose) {
                if (fr.payload.size() == 1) {
                    out.kind = WsInbound::Error;
                    out.code = 1002;
                    return out;
                }
                out.kind = WsInbound::Closed;
                out.code = 1000;
                if (fr.payload.size() >= 2) {
                    out.code = ((uint16_t)(uint8_t)fr.payload[0] << 8) |
                               (uint8_t)fr.payload[1];
                    out.payload = fr.payload.substr(2);
                }
                return out;
            }
            if (fr.opcode == kWsPing) {
                out.kind = WsInbound::Ping;
                out.payload = std::move(fr.payload);
                return out;
            }
            if (fr.opcode == kWsPong) continue;
            out.kind = WsInbound::Error;
            out.code = 1002;
            return out;
        }
        if (fr.opcode == kWsCont) {
            if (!started) {
                out.kind = WsInbound::Error;
                out.code = 1002;
                return out;
            }
        } else if (fr.opcode == kWsText || fr.opcode == kWsBin) {
            if (started) {
                out.kind = WsInbound::Error;
                out.code = 1002;
                return out;
            }
            started = true;
            msg_op = fr.opcode;
        } else {
            out.kind = WsInbound::Error;
            out.code = 1003;
            return out;
        }
        if (msg.size() + fr.payload.size() > kWsMaxMessage) {
            out.kind = WsInbound::Error;
            out.code = 1009;
            return out;
        }
        msg += fr.payload;
        if (fr.fin) {
            if (msg_op == kWsText && !ws_valid_utf8(msg)) {
                out.kind = WsInbound::Error;
                out.code = 1007;
                return out;
            }
            out.kind = WsInbound::Msg;
            out.opcode = msg_op;
            out.payload = std::move(msg);
            return out;
        }
    }
}

// --- bridge --------------------------------------------------------------------

void ws_touch_session(const std::string& sid) {
    std::lock_guard<std::mutex> lock(sock_mutex());
    auto it = sock_sessions().find(sid);
    if (it != sock_sessions().end())
        it->second.last = std::chrono::steady_clock::now();
}

void ws_mark_session_eof(const std::string& sid) {
    std::lock_guard<std::mutex> lock(sock_mutex());
    auto it = sock_sessions().find(sid);
    if (it != sock_sessions().end()) {
        it->second.eof = true;
        it->second.last = std::chrono::steady_clock::now();
    }
}

// Exactly-once bridge cleanup: ends the TCP session, then closes
// the WS client fd (never the reverse order elsewhere).
void ws_bridge_cleanup(const std::shared_ptr<WsBridge>& br) {
    std::call_once(br->cleanup_once, [&] {
        sock_ws_end_session(br->sid);
        close(br->ws_fd);
    });
}

void ws_tcp_to_ws(const std::shared_ptr<WsBridge>& br) {
    char buf[32768];
    while (true) {
        ssize_t n = sock_read(br->peer_fd, br->peer_ssl, buf, sizeof(buf));
        if (n > 0) {
            ws_touch_session(br->sid);
            if (!ws_send_frame(br, kWsBin,
                               std::string(buf, (size_t)n)))
                return;
            continue;
        }
        if (n == 0) {
            ws_mark_session_eof(br->sid);
            ws_send_close(br, 1000, "");
        } else {
            ws_send_close(br, 1011, "");
        }
        break;
    }
    // TCP ended: grace period for the close echo round-trip, then
    // force the main reader out so nothing lingers.
    for (int i = 0; i < 20; ++i) {
        if (br->main_done.load()) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    shutdown(br->ws_fd, SHUT_RDWR);
}

void ws_to_tcp(const std::shared_ptr<WsBridge>& br) {
    while (true) {
        WsMsg m = ws_read_message(br->ws_fd);
        if (m.kind == WsInbound::Ping) {
            if (!ws_send_frame(br, kWsPong, m.payload)) {
                ws_bridge_cleanup(br);
                return;
            }
            continue;
        }
        if (m.kind == WsInbound::Closed) {
            // Delete BEFORE echo: the echo implies "gone".
            sock_ws_end_session(br->sid);
            ws_send_close(br, m.code, m.payload);
            ws_bridge_cleanup(br);
            return;
        }
        // Error: echo fail code first (session doomed), then end.
        if (m.kind == WsInbound::Error) {
            if (m.code != 0) ws_send_close(br, m.code, "");
            ws_bridge_cleanup(br);
            return;
        }
        struct timeval tv{};
        tv.tv_sec = 30;
        setsockopt(br->peer_fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        std::string werr;
        if (!sock_write_all(br->peer_fd, br->peer_ssl, m.payload.data(),
                            m.payload.size(), werr)) {
            ws_bridge_cleanup(br);
            return;
        }
        ws_touch_session(br->sid);
    }
}

// --- upgrade -------------------------------------------------------------------

const char* ws_phrase(int status) {
    switch (status) {
        case 400: return "Bad Request";
        case 401: return "Unauthorized";
        case 404: return "Not Found";
        default: return "Error";
    }
}

void ws_write_http_response(int fd, const HttpResponse& res) {
    std::string head = "HTTP/1.1 " + std::to_string(res.status) + " " +
                       ws_phrase(res.status) + "\r\n";
    for (auto& [k, v] : res.headers) head += k + ": " + v + "\r\n";
    head += "Content-Length: " + std::to_string(res.body.size()) + "\r\n\r\n";
    ws_send_all(fd, head.data(), head.size());
    ws_send_all(fd, res.body.data(), res.body.size());
}

bool ws_is_upgrade_request(const HttpRequest& req) {
    if (req.method != "GET" || req.path != "/api/socket/ws") return false;
    auto it = req.headers.find("connection");
    return it != req.headers.end() && ws_has_token(it->second, "upgrade");
}

// Runs the bridge on the connection thread (which becomes the WS
// reader); returns true when the fd was hijacked (caller must skip
// its normal response AND close — cleanup owns the fd now).
void handle_socket_ws_upgrade(int fd, const HttpRequest& req,
                                const ApiState& st) {
    auto fail = [&](int status, const std::string& message) {
        ws_write_http_response(fd, send_json(status, jerr(status, message)));
        // Validation failures are fully handled here (regular HTTP
        // error): drop the fd so the caller only skips its response.
        shutdown(fd, SHUT_RDWR);
        close(fd);
    };
    if (req.path != "/api/socket/ws") {
        fail(404, "Unknown route: " + req.path);
        return;  // not ours; normal path continues (will 404 too)
    }
    if (auto unauth = check_auth(req, st)) {
        ws_write_http_response(fd, *unauth);
        return;
    }
    auto get_hdr = [&](const char* name) {
        auto it = req.headers.find(name);
        return it == req.headers.end() ? "" : it->second;
    };
    std::string up = get_hdr("upgrade");
    for (auto& c : up) c = std::tolower((unsigned char)c);
    if (up != "websocket") {
        fail(400, "This route requires a WebSocket upgrade "
                  "(Upgrade: websocket).");
        return;
    }
    if (get_hdr("sec-websocket-version") != "13") {
        fail(400, "Unsupported WebSocket version (need 13).");
        return;
    }
    std::string key = get_hdr("sec-websocket-key");
    if (key.empty()) {
        fail(400, "Missing Sec-WebSocket-Key.");
        return;
    }
    std::string sid;
    {
        auto it = req.query.find("id");
        if (it == req.query.end() || it->second.empty()) {
            fail(400, "Missing required query parameter: id");
            return;
        }
        sid = it->second;
    }
    int peer_fd = -1;
    SSL* peer_ssl = nullptr;
    {
        HttpResponse err_res{0, {}, ""};
        if (!sock_ws_attach(sid, peer_fd, peer_ssl, err_res)) {
            ws_write_http_response(fd, err_res);
            return;
        }
    }
    std::string resp =
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: " +
        ws_accept_key(key) + "\r\n\r\n";
    if (!ws_send_all(fd, resp.data(), resp.size())) {
        // 101 never went out: end the session and drop the fd here
        // (no bridge exists yet, so cleanup owns nothing).
        sock_ws_end_session(sid);
        close(fd);
        return;  // hijacked (dead); caller must skip its close
    }
    struct timeval tv{};
    tv.tv_sec = 30;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    auto br = std::make_shared<WsBridge>();
    br->ws_fd = fd;
    br->peer_fd = peer_fd;
    br->peer_ssl = peer_ssl;
    br->sid = sid;
    std::thread([br] { ws_tcp_to_ws(br); }).detach();
    ws_to_tcp(br);
    return;
}

}  // namespace sockws
