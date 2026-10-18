#include "http.h"

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstring>
#include <stdexcept>
#include <thread>

#include "codec.h"

namespace {

const char* reason(int status) {
    switch (status) {
        case 200: return "OK";
        case 201: return "Created";
        case 204: return "No Content";
        case 400: return "Bad Request";
        case 401: return "Unauthorized";
        case 403: return "Forbidden";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 409: return "Conflict";
        case 500: return "Internal Server Error";
        case 503: return "Service Unavailable";
        default: return "Error";
    }
}

std::string lower(std::string s) {
    for (auto& c : s) c = std::tolower((unsigned char)c);
    return s;
}

bool send_all(int fd, const char* data, size_t len) {
    while (len > 0) {
        ssize_t n = send(fd, data, len, MSG_NOSIGNAL);
        if (n <= 0) return false;
        data += n;
        len -= (size_t)n;
    }
    return true;
}

// Read until "\r\n\r\n" (cap 1 MB) then exactly content_length body bytes.
bool read_request(int fd, HttpRequest& out) {
    std::string buf;
    buf.reserve(8192);
    char tmp[8192];
    size_t head_end = std::string::npos;
    while (true) {
        head_end = buf.find("\r\n\r\n");
        if (head_end != std::string::npos) break;
        if (buf.size() > 1024 * 1024) return false;
        ssize_t n = recv(fd, tmp, sizeof(tmp), 0);
        if (n <= 0) return false;
        buf.append(tmp, (size_t)n);
    }
    size_t body_start = head_end + 4;
    // Request line.
    size_t eol = buf.find("\r\n");
    if (eol == std::string::npos) return false;
    std::string request_line = buf.substr(0, eol);
    size_t sp1 = request_line.find(' ');
    size_t sp2 = request_line.find(' ', sp1 == std::string::npos ? 0 : sp1 + 1);
    if (sp1 == std::string::npos || sp2 == std::string::npos) return false;
    out.method = request_line.substr(0, sp1);
    std::string target = request_line.substr(sp1 + 1, sp2 - sp1 - 1);
    // Headers.
    size_t pos = eol + 2;
    while (pos + 1 < head_end) {
        size_t e = buf.find("\r\n", pos);
        if (e == std::string::npos || e > head_end) break;
        std::string line = buf.substr(pos, e - pos);
        pos = e + 2;
        if (line.empty()) break;
        size_t colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string name = line.substr(0, colon);
        std::string value = line.substr(colon + 1);
        value.erase(0, value.find_first_not_of(" \t"));
        out.headers[lower(name)] = value;
    }
    // Path + query.
    std::string path = target, qs;
    size_t q = target.find('?');
    if (q != std::string::npos) {
        path = target.substr(0, q);
        qs = target.substr(q + 1);
    }
    out.path = percent_decode(path);
    size_t i = 0;
    while (i <= qs.size()) {
        size_t amp = qs.find('&', i);
        if (amp == std::string::npos) amp = qs.size();
        std::string part = qs.substr(i, amp - i);
        if (!part.empty()) {
            size_t eq = part.find('=');
            std::string k = percent_decode(eq == std::string::npos
                                               ? part
                                               : part.substr(0, eq));
            if (!out.query.count(k))
                out.query[k] = percent_decode(eq == std::string::npos
                                                  ? ""
                                                  : part.substr(eq + 1));
        }
        i = amp + 1;
    }
    // Body.
    size_t content_length = 0;
    auto it = out.headers.find("content-length");
    if (it != out.headers.end()) {
        try {
            content_length = (size_t)std::stoul(it->second);
        } catch (...) {
            content_length = 0;
        }
    }
    out.body = buf.substr(body_start);
    while (out.body.size() < content_length) {
        ssize_t n = recv(fd, tmp, sizeof(tmp), 0);
        if (n <= 0) break;
        out.body.append(tmp, (size_t)n);
    }
    out.body.resize(std::min(out.body.size(), content_length));
    return true;
}

void write_response(int fd, const HttpResponse& res) {
    std::string head = "HTTP/1.1 " + std::to_string(res.status) + " " +
                       reason(res.status) + "\r\nConnection: close\r\n";
    for (auto& [k, v] : res.headers) head += k + ": " + v + "\r\n";
    head += "Content-Length: " + std::to_string(res.body.size()) + "\r\n\r\n";
    if (!send_all(fd, head.data(), head.size())) return;
    send_all(fd, res.body.data(), res.body.size());
}

HttpResponse bad_request() {
    return HttpResponse{
        400,
        {{"Content-Type", "application/json; charset=utf-8"},
         {"Access-Control-Allow-Origin", "*"}},
        R"({"error":"bad_request","message":"Malformed HTTP request"})"};
}

}  // namespace

void http_serve(const std::string& host, int port,
                std::function<HttpResponse(const HttpRequest&)> handler,
                std::function<bool(int, const HttpRequest&)> try_upgrade) {
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) throw std::runtime_error("socket() failed");
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (host == "0.0.0.0") {
        addr.sin_addr.s_addr = INADDR_ANY;
    } else {
        // Numeric fast path; otherwise resolve (e.g. "localhost").
        // Config validation already restricted this to loopback.
        bool resolved = false;
        if (inet_pton(AF_INET, host.c_str(), &addr.sin_addr) == 1) {
            resolved = true;
        } else {
            struct addrinfo hints{};
            hints.ai_family = AF_INET;
            hints.ai_socktype = SOCK_STREAM;
            struct addrinfo* list = nullptr;
            if (getaddrinfo(host.c_str(), nullptr, &hints, &list) == 0) {
                for (struct addrinfo* ai = list; ai; ai = ai->ai_next) {
                    auto* in4 = (sockaddr_in*)ai->ai_addr;
                    if (ai->ai_family == AF_INET) {
                        addr.sin_addr = in4->sin_addr;
                        resolved = true;
                        break;
                    }
                }
                freeaddrinfo(list);
            }
        }
        if (!resolved) {
            close(srv);
            throw std::runtime_error("invalid bind address: " + host);
        }
    }
    if (bind(srv, (sockaddr*)&addr, sizeof(addr)) != 0) {
        close(srv);
        throw std::runtime_error("bind() failed (port in use?)");
    }
    if (listen(srv, 128) != 0) {
        close(srv);
        throw std::runtime_error("listen() failed");
    }
    while (true) {
        int fd = accept(srv, nullptr, nullptr);
        if (fd < 0) continue;
        std::thread([fd, handler, try_upgrade]() {
            HttpResponse res;
            bool hijacked = false;
            try {
                HttpRequest req;
                if (!read_request(fd, req)) {
                    res = bad_request();
                } else if (try_upgrade && try_upgrade(fd, req)) {
                    hijacked = true;
                } else {
                    res = handler(req);
                }
            } catch (...) {
                res = HttpResponse{
                    500,
                    {{"Content-Type", "application/json; charset=utf-8"},
                     {"Access-Control-Allow-Origin", "*"}},
                    R"({"error":"error","message":"Internal error"})"};
            }
            if (!hijacked) {
                try {
                    write_response(fd, res);
                } catch (...) {
                }
                close(fd);
            }
        }).detach();
    }
}
