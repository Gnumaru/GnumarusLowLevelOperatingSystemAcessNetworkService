// Transparent TCP socket proxy (included by api.cpp; uses its includes
// and shared helpers). TLS via the system OpenSSL (-lssl -lcrypto).
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>

namespace sockws {

const double kSockDefaultConnectTimeout = 5.0;
const double kSockMaxConnectTimeout = 30.0;
const double kSockDefaultRecvTimeout = 5.0;
const double kSockMaxRecvTimeout = 60.0;
const size_t kSockDefaultMaxBytes = 65536;
const size_t kSockMaxBytes = 1024 * 1024;
const size_t kSockMaxSessions = 128;
const long kSockIdleSecs = 300;

struct SockSession {
    int fd = -1;
    // TLS layer over fd (null for plaintext). send/recv/close/WS all
    // see the cleartext stream; the fd itself is never dup'd.
    SSL* ssl = nullptr;
    std::string host;
    int port = 0;
    // "tcp" or "tls": TLS is a transport property of the session,
    // negotiated at open (implicit) or via starttls, and verified
    // against the system roots (+ optional ca_pem) with hostname
    // check and SNI.
    std::string kind = "tcp";
    std::chrono::steady_clock::time_point last;
    bool eof = false;
    bool recv_busy = false;
    // ws_attached marks a live WebSocket bridge: the idle sweeper
    // skips it and HTTP send/recv are refused while set.
    bool ws_attached = false;
};

std::map<std::string, SockSession>& sock_sessions() {
    static std::map<std::string, SockSession> sessions;
    return sessions;
}

std::mutex& sock_mutex() {
    static std::mutex m;
    return m;
}

void sock_sweep_expired() {
    auto now = std::chrono::steady_clock::now();
    std::lock_guard<std::mutex> lock(sock_mutex());
    auto& sessions = sock_sessions();
    for (auto it = sessions.begin(); it != sessions.end();) {
        long idle = std::chrono::duration_cast<std::chrono::seconds>(
                        now - it->second.last)
                        .count();
        if (!it->second.ws_attached && idle > kSockIdleSecs) {
            if (it->second.ssl) SSL_free(it->second.ssl);
            close(it->second.fd);
            it = sessions.erase(it);
        } else {
            ++it;
        }
    }
}

void sock_ensure_sweeper() {
    static std::once_flag flag;
    std::call_once(flag, [] {
        std::thread([] {
            while (true) {
                std::this_thread::sleep_for(std::chrono::seconds(60));
                sock_sweep_expired();
            }
        }).detach();
    });
}

std::string sock_mint_id() {
    unsigned char bytes[16];
    FILE* fh = fopen("/dev/urandom", "rb");
    if (fh) {
        size_t n = fread(bytes, 1, sizeof(bytes), fh);
        fclose(fh);
        if (n != sizeof(bytes)) memset(bytes, 0, sizeof(bytes));
    } else {
        auto ns = std::chrono::steady_clock::now().time_since_epoch().count();
        for (size_t i = 0; i < sizeof(bytes); ++i)
            bytes[i] = (unsigned char)(ns >> (8 * (i % 8)));
    }
    static const char* b64 =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    for (size_t i = 0; i < sizeof(bytes); i += 3) {
        uint32_t n = 0;
        size_t chunk = 0;
        for (size_t j = 0; j < 3 && i + j < sizeof(bytes); ++j, ++chunk)
            n |= (uint32_t)bytes[i + j] << (16 - 8 * j);
        size_t chars = chunk == 3 ? 4 : chunk == 2 ? 3 : 2;
        for (size_t k = 0; k < chars; ++k)
            out.push_back(b64[(n >> (18 - 6 * k)) & 63]);
    }
    return out;
}

// Connect with timeout (non-blocking + select). Returns fd or -1 + message.
int sock_connect(const std::string& host, int port, double timeout,
                 std::string& errmsg) {
    struct addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo* list = nullptr;
    if (getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints,
                    &list) != 0) {
        errmsg = "DNS resolution failed";
        return -1;
    }
    int fd = -1;
    for (struct addrinfo* ai = list; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) {
            errmsg = strerror(errno);
            continue;
        }
        int flags = fcntl(fd, F_GETFL, 0);
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
        int rc = connect(fd, ai->ai_addr, ai->ai_addrlen);
        if (rc != 0 && errno != EINPROGRESS) {
            errmsg = strerror(errno);
            close(fd);
            fd = -1;
            continue;
        }
        struct pollfd pfd{fd, POLLOUT, 0};
        int wait_ms = (int)(timeout * 1000.0);
        int pr = poll(&pfd, 1, wait_ms);
        if (pr <= 0) {
            errmsg = pr == 0 ? "connect timed out" : strerror(errno);
            close(fd);
            fd = -1;
            continue;
        }
        int so_err = 0;
        socklen_t len = sizeof(so_err);
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &so_err, &len);
        if (so_err != 0) {
            errmsg = strerror(so_err);
            close(fd);
            fd = -1;
            continue;
        }
        fcntl(fd, F_SETFL, flags);
        break;
    }
    freeaddrinfo(list);
    return fd;
}

bool sock_is_ip_literal(const std::string& s) {
    if (s.find(':') != std::string::npos) return true;
    if (s.empty()) return false;
    for (char c : s)
        if (!(c >= '0' && c <= '9') && c != '.') return false;
    int parts = 0;
    size_t start = 0;
    for (size_t i = 0; i <= s.size(); ++i) {
        if (i == s.size() || s[i] == '.') {
            std::string p = s.substr(start, i - start);
            if (p.empty() || p.size() > 3) return false;
            for (char c : p)
                if (!(c >= '0' && c <= '9')) return false;
            if (std::stoi(p) > 255) return false;
            ++parts;
            start = i + 1;
        }
    }
    return parts == 4;
}

// Transparent read: cleartext through the TLS layer when present.
ssize_t sock_read(int fd, SSL* ssl, char* buf, size_t len) {
    if (!ssl) return ::recv(fd, buf, len, 0);
    if (len > 1048576) len = 1048576;
    int r = SSL_read(ssl, buf, (int)len);
    if (r <= 0) {
        int e = SSL_get_error(ssl, r);
        if (e == SSL_ERROR_WANT_READ || e == SSL_ERROR_WANT_WRITE) {
            errno = EAGAIN;
            return -1;
        }
        if (e == SSL_ERROR_ZERO_RETURN) return 0;
        errno = EIO;
        return -1;
    }
    return r;
}

// Transparent write-all: cleartext through the TLS layer when present.
bool sock_write_all(int fd, SSL* ssl, const char* data, size_t len,
                    std::string& errmsg) {
    size_t off = 0;
    while (off < len) {
        size_t chunk = len - off;
        if (chunk > 1048576) chunk = 1048576;
        ssize_t n;
        if (ssl) {
            int r = SSL_write(ssl, data + off, (int)chunk);
            if (r <= 0) {
                int e = SSL_get_error(ssl, r);
                if (e == SSL_ERROR_WANT_READ ||
                    e == SSL_ERROR_WANT_WRITE)
                    continue;
                errmsg = "TLS write failed";
                return false;
            }
            n = r;
        } else {
            n = send(fd, data + off, chunk, MSG_NOSIGNAL);
            if (n <= 0) {
                if (errno == EINTR) continue;
                errmsg = strerror(errno);
                return false;
            }
        }
        off += (size_t)n;
    }
    return true;
}

// Optional sni/ca_pem fields shared by open and starttls.
// Returns false + message on validation failure.
bool sock_parse_tls_identity(const json& obj, std::string& sni_out,
                             std::string& ca_pem_out, std::string& err_out) {
    sni_out.clear();
    ca_pem_out.clear();
    auto its = obj.find("sni");
    if (its != obj.end() && !its->is_null()) {
        if (!its->is_string()) {
            err_out = "Field 'sni' must be a non-empty string.";
            return false;
        }
        std::string s = its->get<std::string>();
        size_t a = s.find_first_not_of(" \t\n\r");
        size_t b = s.find_last_not_of(" \t\n\r");
        if (a == std::string::npos) {
            err_out = "Field 'sni' must be a non-empty string.";
            return false;
        }
        sni_out = s.substr(a, b - a + 1);
    }
    auto itc = obj.find("ca_pem");
    if (itc != obj.end() && !itc->is_null()) {
        if (!itc->is_string()) {
            err_out = "Field 'ca_pem' must be a PEM CA bundle string.";
            return false;
        }
        std::string s = itc->get<std::string>();
        if (s.find_first_not_of(" \t\n\r") == std::string::npos) {
            err_out = "Field 'ca_pem' must be a PEM CA bundle string.";
            return false;
        }
        ca_pem_out = s;
    }
    return true;
}

// Root pool from the system store plus an optional extra PEM bundle.
// Returns nullptr + message on failure.
SSL_CTX* sock_tls_context(const std::string& ca_pem, std::string& err_out) {
    ERR_clear_error();
    SSL_CTX* ctx = SSL_CTX_new(TLS_client_method());
    if (!ctx) {
        err_out = "TLS setup failed.";
        return nullptr;
    }
    SSL_CTX_set_min_proto_version(ctx, TLS1_2_VERSION);
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, nullptr);
    if (!SSL_CTX_set_default_verify_paths(ctx)) {
        err_out = "TLS setup failed.";
        SSL_CTX_free(ctx);
        return nullptr;
    }
    if (!ca_pem.empty()) {
        BIO* bio = BIO_new_mem_buf(ca_pem.data(), (int)ca_pem.size());
        if (!bio) {
            err_out = "TLS setup failed.";
            SSL_CTX_free(ctx);
            return nullptr;
        }
        X509_STORE* store = SSL_CTX_get_cert_store(ctx);
        int added = 0;
        while (true) {
            X509* cert = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
            if (!cert) break;
            if (X509_STORE_add_cert(store, cert)) ++added;
            X509_free(cert);
        }
        BIO_free(bio);
        ERR_clear_error();
        if (added == 0) {
            err_out = "Field 'ca_pem' must be a valid PEM CA bundle.";
            SSL_CTX_free(ctx);
            return nullptr;
        }
    }
    return ctx;
}

// Client handshake over an established fd (same fd, no dup: the
// session upgrades in place). Timeouts bound the handshake; they are
// cleared afterwards. Returns nullptr + message on failure (the raw
// fd stays open: open drops it, starttls keeps the session).
SSL* sock_tls_handshake(int fd, const std::string& sni, SSL_CTX* ctx,
                        int timeout_sec, std::string& err_out) {
    ERR_clear_error();
    SSL* ssl = SSL_new(ctx);
    if (!ssl) {
        err_out = "TLS setup failed.";
        return nullptr;
    }
    SSL_set_fd(ssl, fd);
    if (!sock_is_ip_literal(sni)) {
        if (SSL_set_tlsext_host_name(ssl, sni.c_str()) != 1) {
            err_out = "TLS setup failed.";
            SSL_free(ssl);
            return nullptr;
        }
    }
    // Hostname verification (DNS + IP SANs, RFC 6125 wildcards).
    if (SSL_set1_host(ssl, sni.c_str()) != 1) {
        err_out = "TLS setup failed.";
        SSL_free(ssl);
        return nullptr;
    }
    struct timeval tv{};
    tv.tv_sec = timeout_sec;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    int r = SSL_connect(ssl);
    tv.tv_sec = 0;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    if (r != 1) {
        unsigned long e = ERR_get_error();
        char buf[256];
        ERR_error_string_n(e, buf, sizeof(buf));
        err_out = buf[0] ? buf : "handshake failed";
        SSL_free(ssl);
        return nullptr;
    }
    ERR_clear_error();
    return ssl;
}

bool sock_get_id(const HttpRequest& req, std::string& sid_out) {
    auto it = req.query.find("id");
    if (it == req.query.end() || it->second.empty()) return false;
    sid_out = it->second;
    return true;
}

HttpResponse handle_socket_open(const HttpRequest& req) {
    sock_ensure_sweeper();
    auto ctit = req.headers.find("content-type");
    std::string ct = ctit == req.headers.end() ? "" : ctit->second;
    std::string lower = ct;
    for (auto& c : lower) c = std::tolower((unsigned char)c);
    if (lower.find("application/json") == std::string::npos)
        return send_json(
            400, jerr(400, "Content-Type must be application/json with body "
                           "{ host, port, timeout? }."));
    json obj;
    try {
        obj = req.body.empty() ? json::object() : json::parse(req.body);
    } catch (...) {
        return send_json(400, jerr(400, "Invalid JSON body."));
    }
    if (!obj.is_object())
        return send_json(400, jerr(400, "JSON body must be an object."));
    auto ith = obj.find("host");
    if (ith == obj.end() || !ith->is_string() ||
        ith->get<std::string>().find_first_not_of(" \t\n\r") ==
            std::string::npos)
        return send_json(400, jerr(400, "Missing or empty required field: host."));
    std::string host = ith->get<std::string>();
    auto itp = obj.find("port");
    if (itp == obj.end() || itp->is_null())
        return send_json(400, jerr(400, "Missing required field: port (i:<1-65535>)."));
    if (!itp->is_string() ||
        itp->get<std::string>().rfind("i:", 0) != 0)
        return send_json(
            400, jerr(400, "Field 'port' must be an integer 1-65535 (i:<int>)."));
    int port = 0;
    try {
        Tagged t = jdec(itp->get<std::string>());
        auto* n = std::get_if<int64_t>(&t);
        if (!n) throw BadTag{"not int"};
        if (*n < 1 || *n > 65535) throw BadTag{"range"};
        port = (int)*n;
    } catch (const BadTag&) {
        return send_json(
            400, jerr(400, "Field 'port' must be an integer 1-65535 (i:<int>)."));
    }
    double timeout = kSockDefaultConnectTimeout;
    auto itt = obj.find("timeout");
    if (itt != obj.end() && !itt->is_null()) {
        if (!itt->is_string())
            return send_json(400, jerr(400, "Field 'timeout' must be a tagged number "
                                            "(i:<int> or f:<float>)."));
        try {
            Tagged t = jdec(itt->get<std::string>());
            if (auto* n = std::get_if<int64_t>(&t))
                timeout = (double)*n;
            else if (auto* f = std::get_if<double>(&t))
                timeout = *f;
            else
                throw BadTag{"not number"};
        } catch (const BadTag&) {
            return send_json(400, jerr(400, "Field 'timeout' must be a tagged number "
                                            "(i:<int> or f:<float>)."));
        }
    }
    if (!(timeout > 0 && timeout <= kSockMaxConnectTimeout))
        return send_json(400, jerr(400, "Field 'timeout' must be within (0, 30]."));
    bool tls_mode = false;
    auto itl = obj.find("tls");
    if (itl != obj.end() && !itl->is_null()) {
        if (!itl->is_string())
            return send_json(400, jerr(400, "Field 'tls' must be b:true or b:false."));
        try {
            Tagged t = jdec(itl->get<std::string>());
            auto* b = std::get_if<bool>(&t);
            if (!b) throw BadTag{"not bool"};
            tls_mode = *b;
        } catch (const BadTag&) {
            return send_json(400, jerr(400, "Field 'tls' must be b:true or b:false."));
        }
    }
    std::string sni, ca_pem, ident_err;
    if (!sock_parse_tls_identity(obj, sni, ca_pem, ident_err))
        return send_json(400, jerr(400, ident_err));
    if (sni.empty()) sni = host;
    SSL_CTX* tls_ctx = nullptr;
    if (tls_mode) {
        std::string ctx_err;
        tls_ctx = sock_tls_context(ca_pem, ctx_err);
        if (!tls_ctx) return send_json(400, jerr(400, ctx_err));
    }
    sock_sweep_expired();
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        if (sock_sessions().size() >= kSockMaxSessions) {
            if (tls_ctx) SSL_CTX_free(tls_ctx);
            return send_json(400, jerr(400, "Too many open socket sessions (128)."));
        }
    }
    std::string errmsg;
    int fd = sock_connect(host, port, timeout, errmsg);
    if (fd < 0) {
        if (tls_ctx) SSL_CTX_free(tls_ctx);
        return send_json(
            400, jerr(400, "Cannot connect to " + host + ":" +
                           std::to_string(port) + ": " + errmsg));
    }
    std::string kind = "tcp";
    SSL* tls_ssl = nullptr;
    if (tls_ctx) {
        std::string hs_err;
        tls_ssl = sock_tls_handshake(fd, sni, tls_ctx, (int)timeout, hs_err);
        SSL_CTX_free(tls_ctx);
        if (!tls_ssl) {
            close(fd);
            return send_json(400, jerr(400, "TLS handshake with " + host +
                                            ":" + std::to_string(port) +
                                            " failed: " + hs_err));
        }
        kind = "tls";
    }
    // Bounded blocking sends (30s); recvs use per-call select timeouts.
    struct timeval tv{};
    tv.tv_sec = 30;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    std::string sid = sock_mint_id();
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        SockSession sess;
        sess.fd = fd;
        sess.ssl = tls_ssl;
        sess.kind = kind;
        sess.host = host;
        sess.port = port;
        sess.last = std::chrono::steady_clock::now();
        sess.ws_attached = false;
        sock_sessions()[sid] = sess;
    }
    json o;
    o["id"] = sid;
    o["kind"] = kind;
    o["host"] = host;
    o["port"] = jenc_int(port);
    return send_json(201, o);
}

bool sock_send_parse(const HttpRequest& req, std::string& data_out) {
    auto ctit = req.headers.find("content-type");
    std::string ct = ctit == req.headers.end() ? "" : ctit->second;
    std::string lower = ct;
    for (auto& c : lower) c = std::tolower((unsigned char)c);
    if (lower.find("application/json") == std::string::npos) {
        data_out = req.body;
        return true;
    }
    json obj;
    try {
        obj = req.body.empty() ? json::object() : json::parse(req.body);
    } catch (...) {
        return false;
    }
    if (!obj.is_object()) return false;
    std::string content;
    auto itc = obj.find("content");
    if (itc != obj.end() && !itc->is_null()) {
        if (!itc->is_string()) return false;
        content = itc->get<std::string>();
    }
    std::string encoding = "text";
    auto ite = obj.find("encoding");
    if (ite != obj.end() && !ite->is_null()) {
        if (!ite->is_string()) return false;
        encoding = ite->get<std::string>();
        for (auto& c : encoding) c = std::tolower((unsigned char)c);
    }
    if (encoding == "text") {
        data_out = content;
        return true;
    }
    if (encoding == "base64") {
        std::string decoded;
        if (!b64_decode(content, decoded)) return false;
        data_out = decoded;
        return true;
    }
    return false;
}

HttpResponse handle_socket_send(const HttpRequest& req) {
    std::string sid;
    if (!sock_get_id(req, sid))
        return send_json(400, jerr(400, "Missing required query parameter: id"));
    int fd = -1;
    SSL* ssl = nullptr;
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it == sock_sessions().end())
            return send_json(404, jerr(404, "Unknown socket session: " + sid));
        if (it->second.ws_attached)
            return send_json(
                400, jerr(400, "Socket session is attached to a websocket "
                               "(HTTP send refused while attached)."));
        it->second.last = std::chrono::steady_clock::now();
        fd = it->second.fd;
        ssl = it->second.ssl;
    }
    std::string data;
    // NOTE: sock_send_parse returns false for every JSON shape
    // violation; distinguish the messages like the file endpoints do.
    auto ctit = req.headers.find("content-type");
    std::string ctval = ctit == req.headers.end() ? "" : ctit->second;
    for (auto& c : ctval) c = std::tolower((unsigned char)c);
    bool is_json = ctval.find("application/json") != std::string::npos;
    if (!sock_send_parse(req, data)) {
        if (!is_json) return send_json(400, jerr(400, "Cannot read body."));
        return send_json(400, jerr(400, "Invalid JSON body or 'content'/'encoding'."));
    }
    std::string werr;
    if (!sock_write_all(fd, ssl, data.data(), data.size(), werr))
        return send_json(400, jerr(400, std::string("Socket error: ") + werr));
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it != sock_sessions().end())
            it->second.last = std::chrono::steady_clock::now();
    }
    json o;
    o["id"] = sid;
    o["bytes"] = jenc_int((int64_t)data.size());
    return send_json(200, o);
}

bool sock_plain_timeout(const std::string& text, double dflt, double max,
                        double& out) {
    if (text.empty()) {
        out = dflt;
        return true;
    }
    try {
        size_t pos = 0;
        out = std::stod(text, &pos);
        if (pos != text.size()) return false;
    } catch (...) {
        return false;
    }
    return out > 0 && out <= max && std::isfinite(out);
}

HttpResponse handle_socket_recv(const HttpRequest& req) {
    double timeout = 0;
    {
        auto it = req.query.find("timeout");
        std::string raw = it == req.query.end() ? "" : it->second;
        if (!sock_plain_timeout(raw, kSockDefaultRecvTimeout,
                                kSockMaxRecvTimeout, timeout))
            return send_json(
                400, jerr(400, "Invalid timeout param (seconds within (0, 60])."));
    }
    size_t max_bytes = 0;
    {
        auto it = req.query.find("max_bytes");
        if (it == req.query.end() || it->second.empty()) {
            max_bytes = kSockDefaultMaxBytes;
        } else {
            try {
                long n = std::stol(it->second);
                if (n < 1 || (uint64_t)n > kSockMaxBytes) throw BadTag{"range"};
                max_bytes = (size_t)n;
            } catch (...) {
                return send_json(
                    400, jerr(400, "Invalid max_bytes param (1-1048576)."));
            }
        }
    }
    std::string sid;
    if (!sock_get_id(req, sid))
        return send_json(400, jerr(400, "Missing required query parameter: id"));
    int fd = -1;
    SSL* ssl = nullptr;
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it == sock_sessions().end())
            return send_json(404, jerr(404, "Unknown socket session: " + sid));
        it->second.last = std::chrono::steady_clock::now();
        if (it->second.ws_attached)
            return send_json(
                400, jerr(400, "Socket session is attached to a websocket "
                               "(HTTP recv refused while attached)."));
        if (it->second.eof) {
            json o;
            o["id"] = sid;
            o["encoding"] = "base64";
            o["content"] = "";
            o["bytes"] = jenc_int(0);
            o["eof"] = jenc_bool(true);
            o["timed_out"] = jenc_bool(false);
            return send_json(200, o);
        }
        if (it->second.recv_busy)
            return send_json(
                400, jerr(400, "Another recv is already pending on this session."));
        it->second.recv_busy = true;
        fd = it->second.fd;
        ssl = it->second.ssl;
    }
    struct pollfd pfd{fd, POLLIN, 0};
    int wait_ms = (int)(timeout * 1000.0);
    // Decrypted bytes already buffered in the TLS layer do not make
    // the fd poll-readable: skip the wait when any are pending.
    int pr = (ssl && SSL_pending(ssl) > 0) ? 1 : poll(&pfd, 1, wait_ms);
    auto release = [&](bool eof) {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it != sock_sessions().end()) {
            it->second.recv_busy = false;
            it->second.last = std::chrono::steady_clock::now();
            if (eof) it->second.eof = true;
        }
    };
    if (pr <= 0) {
        release(false);
        if (pr == 0) {
            json o;
            o["id"] = sid;
            o["encoding"] = "base64";
            o["content"] = "";
            o["bytes"] = jenc_int(0);
            o["eof"] = jenc_bool(false);
            o["timed_out"] = jenc_bool(true);
            return send_json(200, o);
        }
        return send_json(
            400, jerr(400, std::string("Socket error: ") + strerror(errno)));
    }
    std::string chunk;
    chunk.resize(max_bytes);
    ssize_t n = sock_read(fd, ssl, chunk.data(), max_bytes);
    if (n < 0) {
        release(false);
        return send_json(
            400, jerr(400, std::string("Socket error: ") + strerror(errno)));
    }
    if (n == 0) {
        release(true);
        json o;
        o["id"] = sid;
        o["encoding"] = "base64";
        o["content"] = "";
        o["bytes"] = jenc_int(0);
        o["eof"] = jenc_bool(true);
        o["timed_out"] = jenc_bool(false);
        return send_json(200, o);
    }
    release(false);
    json o;
    o["id"] = sid;
    o["encoding"] = "base64";
    o["content"] = b64_encode(std::string(chunk.data(), (size_t)n));
    o["bytes"] = jenc_int((int64_t)n);
    o["eof"] = jenc_bool(false);
    o["timed_out"] = jenc_bool(false);
    return send_json(200, o);
}

// End a session: remove-if-present + shutdown + close. Every
// peer-fd close funnels through here (HTTP close, bridge end):
// exactly-once per fd, so raw close() can never hit a reused number.
// Returns true when a session existed.
bool sock_ws_end_session(const std::string& sid) {
    int fd = -1;
    SSL* ssl = nullptr;
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it == sock_sessions().end()) return false;
        fd = it->second.fd;
        ssl = it->second.ssl;
        sock_sessions().erase(it);
    }
    shutdown(fd, SHUT_RDWR);
    close(fd);
    if (ssl) SSL_free(ssl);
    return true;
}

// Attach a WebSocket to a session: unknown ids (404), double attach
// and in-flight HTTP recv (400) refused. Returns the peer fd (+TLS
// layer, null for plaintext).
bool sock_ws_attach(const std::string& sid, int& fd_out, SSL*& ssl_out,
                    HttpResponse& err_out) {
    std::lock_guard<std::mutex> lock(sock_mutex());
    auto it = sock_sessions().find(sid);
    if (it == sock_sessions().end()) {
        err_out = send_json(
            404, jerr(404, "Unknown socket session: " + sid));
        return false;
    }
    if (it->second.ws_attached) {
        err_out = send_json(
            400, jerr(400, "Socket session is already attached to a "
                           "websocket."));
        return false;
    }
    if (it->second.recv_busy) {
        err_out = send_json(
            400, jerr(400, "Another recv is already pending on this "
                           "session."));
        return false;
    }
    it->second.ws_attached = true;
    it->second.last = std::chrono::steady_clock::now();
    fd_out = it->second.fd;
    ssl_out = it->second.ssl;
    return true;
}

HttpResponse handle_socket_close(const HttpRequest& req) {
    std::string sid;
    if (!sock_get_id(req, sid))
        return send_json(400, jerr(400, "Missing required query parameter: id"));
    if (!sock_ws_end_session(sid))
        return send_json(404, jerr(404, "Unknown socket session: " + sid));
    json o;
    o["id"] = sid;
    o["closed"] = jenc_bool(true);
    return send_json(200, o);
}

// Upgrade a plaintext session to TLS in place (STARTTLS-style). A
// failed handshake leaves the plaintext session untouched.
HttpResponse handle_socket_starttls(const HttpRequest& req) {
    std::string sid;
    if (!sock_get_id(req, sid))
        return send_json(400, jerr(400, "Missing required query parameter: id"));
    std::string host;
    int port = 0;
    int fd = -1;
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it == sock_sessions().end())
            return send_json(404, jerr(404, "Unknown socket session: " + sid));
        it->second.last = std::chrono::steady_clock::now();
        if (it->second.ws_attached)
            return send_json(400, jerr(400, "Socket session is attached to a "
                                            "websocket (STARTTLS refused while "
                                            "attached)."));
        if (it->second.kind == "tls")
            return send_json(400, jerr(400, "Socket session is already TLS."));
        if (it->second.recv_busy)
            return send_json(400, jerr(400, "Another recv is already pending on "
                                            "this session."));
        it->second.recv_busy = true;
        host = it->second.host;
        port = it->second.port;
        fd = it->second.fd;
    }
    auto release = [&] {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it != sock_sessions().end()) {
            it->second.recv_busy = false;
            it->second.last = std::chrono::steady_clock::now();
        }
    };
    json obj;
    if (!req.body.empty()) {
        auto ctit = req.headers.find("content-type");
        std::string ct = ctit == req.headers.end() ? "" : ctit->second;
        std::string lower = ct;
        for (auto& c : lower) c = std::tolower((unsigned char)c);
        if (lower.find("application/json") == std::string::npos) {
            release();
            return send_json(400, jerr(400, "Content-Type must be application/json "
                                           "with body { sni?, ca_pem? }."));
        }
        try {
            obj = json::parse(req.body);
        } catch (...) {
            release();
            return send_json(400, jerr(400, "Invalid JSON body."));
        }
        if (!obj.is_object()) {
            release();
            return send_json(400, jerr(400, "JSON body must be an object."));
        }
    } else {
        obj = json::object();
    }
    std::string sni, ca_pem, ident_err;
    if (!sock_parse_tls_identity(obj, sni, ca_pem, ident_err)) {
        release();
        return send_json(400, jerr(400, ident_err));
    }
    if (sni.empty()) sni = host;
    std::string ctx_err;
    SSL_CTX* tls_ctx = sock_tls_context(ca_pem, ctx_err);
    if (!tls_ctx) {
        release();
        return send_json(400, jerr(400, ctx_err));
    }
    std::string hs_err;
    SSL* tls_ssl = sock_tls_handshake(fd, sni, tls_ctx,
                                      (int)kSockMaxConnectTimeout, hs_err);
    SSL_CTX_free(tls_ctx);
    if (!tls_ssl) {
        release();
        return send_json(400, jerr(400, "TLS handshake with " + host + ":" +
                                      std::to_string(port) + " failed: " +
                                      hs_err));
    }
    {
        std::lock_guard<std::mutex> lock(sock_mutex());
        auto it = sock_sessions().find(sid);
        if (it != sock_sessions().end() && it->second.kind != "tls" &&
            it->second.fd == fd) {
            it->second.ssl = tls_ssl;
            it->second.kind = "tls";
            it->second.recv_busy = false;
            it->second.last = std::chrono::steady_clock::now();
        } else {
            SSL_free(tls_ssl);
        }
    }
    json o;
    o["id"] = sid;
    o["kind"] = "tls";
    return send_json(200, o);
}

}  // namespace sockws
