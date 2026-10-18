#pragma once

#include <functional>
#include <map>
#include <string>

// Minimal HTTP/1.1 server over POSIX sockets (stdlib threads).
// Supports origin-form requests with Content-Length bodies; every
// response carries `Connection: close`.

struct HttpRequest {
    std::string method;
    std::string path;  // percent-decoded, without query
    std::map<std::string, std::string> query;  // first value wins
    std::map<std::string, std::string> headers;  // lowercased names
    std::string body;
};

struct HttpResponse {
    int status = 200;
    std::vector<std::pair<std::string, std::string>> headers;
    std::string body;
};

// Blocks serving forever (thread per connection, detached).
// handler receives a parsed request and returns a response.
// try_upgrade, when set, runs first: return true to hijack the fd
// (no response is written and the fd is NOT closed by the server;
// the hijacker owns it now, with exactly-once close discipline).
void http_serve(const std::string& host, int port,
                std::function<HttpResponse(const HttpRequest&)> handler,
                std::function<bool(int, const HttpRequest&)> try_upgrade = {});
