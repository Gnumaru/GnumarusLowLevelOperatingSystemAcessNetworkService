#pragma once

#include <optional>
#include <string>

#include "config.h"
#include "http.h"
#include "nlohmann/json.hpp"

using nlohmann::json;

// Shared server state (auth + working directory).
struct ApiState {
    bool auth_required = true;
    std::string auth_token;
    std::string cwd;
};

// Top-level dispatch (defined in api.cpp).
HttpResponse api_handle(const HttpRequest& req, const ApiState& st);

// --- helpers shared across api_*.cpp ---------------------------------------

// JSON builders (payloads carry only string/array/object + tagged scalars).
json jstr(const std::string& s);
json jerr(int status, const std::string& message,
          const std::string& path = "");

// Auth gate: nullopt when the request may proceed, else the 401 response.
std::optional<HttpResponse> check_auth(const HttpRequest& req,
                                       const ApiState& st);

// Shared JSON responders (defined in api.cpp, used across TUs).
void add_cors(HttpResponse& res);
HttpResponse send_json(int status, const json& obj);
HttpResponse send_raw(int status, const std::string& body,
                      const std::string& content_type);

namespace sockws {
// WebSocket upgrade entry points (defined in api.cpp via
// api_socket_ws.inc). The hijack predicate is true only for upgrade
// attempts to our route; the handler owns the fd on true.
bool ws_is_upgrade_request(const HttpRequest& req);
void handle_socket_ws_upgrade(int fd, const HttpRequest& req,
                              const ApiState& st);
}  // namespace sockws

// Path helpers.
std::string resolve_path(const std::string& raw, const std::string& cwd);
std::string entry_type(const std::string& path);
std::string mode_string(const std::string& path);
std::string guess_mime(const std::string& path);

// Times as epoch seconds (double). ctime falls back to mtime when the
// platform cannot provide it; birth() returns nullopt when unavailable.
double file_atime(const std::string& path);
double file_mtime(const std::string& path);
double file_ctime(const std::string& path);
std::optional<double> file_birth(const std::string& path);
