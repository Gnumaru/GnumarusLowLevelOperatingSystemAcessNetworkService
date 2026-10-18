#include "api.h"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

#include <sqlite3.h>

#include "codec.h"
#include "hosts.h"
#include "routes.h"
#include "tags.h"
#include "timex.h"

namespace fs = std::filesystem;

json jstr(const std::string& s) { return json(s); }

void add_cors(HttpResponse& res);
HttpResponse send_json(int status, const json& obj);
HttpResponse send_raw(int status, const std::string& body,
                      const std::string& content_type);

namespace {

std::string status_label(int status) {
    switch (status) {
        case 400: return "bad_request";
        case 401: return "unauthorized";
        case 403: return "forbidden";
        case 404: return "not_found";
        case 405: return "method_not_allowed";
        case 409: return "conflict";
        default: return "error";
    }
}

// Constant-time token compare (lengths must match first).
bool token_ok(const std::string& supplied, const std::string& expected) {
    if (expected.empty() || supplied.size() != expected.size()) return false;
    unsigned diff = 0;
    for (size_t i = 0; i < supplied.size(); ++i)
        diff |= (unsigned)(supplied[i] ^ expected[i]);
    return diff == 0;
}

bool lexists(const std::string& p) {
    struct stat st;
    return lstat(p.c_str(), &st) == 0;
}

bool is_symlink(const std::string& p) {
    struct stat st;
    return lstat(p.c_str(), &st) == 0 && S_ISLNK(st.st_mode);
}

bool is_dir_follow(const std::string& p) {
    struct stat st;
    return stat(p.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

std::string utf8_sanitize(const std::string& data) {
    // Replace invalid sequences with U+FFFD (matches errors="replace").
    std::string out;
    out.reserve(data.size());
    size_t i = 0;
    while (i < data.size()) {
        unsigned c = (unsigned char)data[i];
        size_t size = 0;
        if (c < 0x80)
            size = 1;
        else if ((c >> 5) == 0x6)
            size = 2;
        else if ((c >> 4) == 0xE)
            size = 3;
        else if ((c >> 3) == 0x1E)
            size = 4;
        bool ok = size > 0 && i + size <= data.size();
        if (ok) {
            for (size_t j = 1; j < size; ++j)
                if (((unsigned char)data[i + j] >> 6) != 0x2) ok = false;
        }
        if (!ok) {
            out += "\xEF\xBF\xBD";
            ++i;
        } else {
            out.append(data, i, size);
            i += size;
        }
    }
    return out;
}

bool has_binary_controls(const std::string& s) {
    for (unsigned char c : s)
        if (c < 32 && c != '\t' && c != '\n' && c != '\r') return true;
    return false;
}

std::string read_file_bytes(const std::string& p, std::string& err_kind) {
    FILE* fh = fopen(p.c_str(), "rb");
    if (!fh) {
        err_kind = (errno == ENOENT) ? "missing" : "denied";
        return "";
    }
    std::string data;
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), fh)) > 0)
        data.append(buf, n);
    if (ferror(fh)) err_kind = "denied";
    fclose(fh);
    return data;
}

}  // namespace

void add_cors(HttpResponse& res) {
    res.headers.emplace_back("Access-Control-Allow-Origin", "*");
    res.headers.emplace_back("Access-Control-Allow-Methods",
                             "GET, POST, PUT, DELETE, PATCH, OPTIONS");
    res.headers.emplace_back("Access-Control-Allow-Headers",
                             "Content-Type, Authorization, X-Requested-With");
    res.headers.emplace_back("Access-Control-Max-Age", "86400");
}

HttpResponse send_json(int status, const json& obj) {
    HttpResponse res;
    res.status = status;
    add_cors(res);
    res.headers.emplace_back("Content-Type",
                             "application/json; charset=utf-8");
    res.body = obj.dump();
    return res;
}

HttpResponse send_raw(int status, const std::string& body,
                      const std::string& content_type) {
    HttpResponse res;
    res.status = status;
    add_cors(res);
    res.headers.emplace_back("Content-Type", content_type);
    res.body = body;
    return res;
}

json jerr(int status, const std::string& message, const std::string& path) {
    json obj;
    obj["error"] = status_label(status);
    obj["message"] = message;
    if (!path.empty()) obj["path"] = path;
    return obj;
}

std::optional<HttpResponse> check_auth(const HttpRequest& req,
                                       const ApiState& st) {
    auto hit = req.headers.find("host");
    if (!host_allowed(hit == req.headers.end() ? "" : hit->second)) {
        HttpResponse res;
        res.status = 403;
        add_cors(res);
        res.headers.emplace_back("Content-Type",
                                 "application/json; charset=utf-8");
        res.body = jerr(403, kHostDeniedMsg).dump();
        return res;
    }
    if (!st.auth_required) return std::nullopt;
    if (req.method == "GET" && (req.path == "/" || req.path == "/api/help"))
        return std::nullopt;
    std::string supplied;
    auto it = req.headers.find("authorization");
    if (it != req.headers.end() && it->second.rfind("Bearer ", 0) == 0)
        supplied = it->second.substr(7);
    // trim
    supplied.erase(0, supplied.find_first_not_of(" \t"));
    supplied.erase(supplied.find_last_not_of(" \t") + 1);
    if (supplied.empty()) {
        auto q = req.query.find("token");
        if (q != req.query.end()) supplied = q->second;
    }
    if (token_ok(supplied, st.auth_token)) return std::nullopt;
    HttpResponse res;
    res.status = 401;
    add_cors(res);
    res.headers.emplace_back("Content-Type",
                             "application/json; charset=utf-8");
    res.headers.emplace_back("WWW-Authenticate", "Bearer");
    res.body =
        R"({"error":"unauthorized","message":"Valid API token required: send Authorization: Bearer <token> or ?token=."})";
    return res;
}

std::string resolve_path(const std::string& raw, const std::string& cwd) {
    std::string p = raw;
    if (p == "~" || (p.size() > 1 && p[0] == '~' && p[1] == '/')) {
        const char* home = getenv("HOME");
        if (!home || !*home) home = getenv("USERPROFILE");
        if (home && *home) p = std::string(home) + p.substr(1);
    }
    fs::path abs =
        fs::path(p).is_absolute() ? fs::path(p) : fs::path(cwd) / p;
    return abs.lexically_normal().string();
}

std::string entry_type(const std::string& p) {
    if (is_symlink(p)) return "symlink";
    struct stat st;
    if (stat(p.c_str(), &st) != 0) return "other";
    if (S_ISDIR(st.st_mode)) return "dir";
    if (S_ISREG(st.st_mode)) return "file";
    return "other";
}

std::string mode_string(const std::string& p) {
    struct stat st;
    if (lstat(p.c_str(), &st) != 0) return "0o644";
    char buf[8];
    snprintf(buf, sizeof(buf), "0o%o", st.st_mode & 0777);
    return buf;
}

std::string guess_mime(const std::string& p) {
    static const std::pair<const char*, const char*> kMap[] = {
        {"txt", "text/plain"},         {"html", "text/html"},
        {"htm", "text/html"},          {"json", "application/json"},
        {"xml", "text/xml"},           {"css", "text/css"},
        {"js", "text/javascript"},     {"png", "image/png"},
        {"jpg", "image/jpeg"},         {"jpeg", "image/jpeg"},
        {"gif", "image/gif"},          {"pdf", "application/pdf"},
        {"zip", "application/zip"},    {"csv", "text/csv"},
        {"md", "text/markdown"},
    };
    size_t dot = p.rfind('.');
    if (dot == std::string::npos) return "application/octet-stream";
    std::string ext = p.substr(dot + 1);
    for (auto& c : ext) c = std::tolower((unsigned char)c);
    for (auto& [k, v] : kMap)
        if (ext == k) return v;
    return "application/octet-stream";
}

namespace {

double ts_to_epoch(time_t sec, long nsec) {
    return (double)sec + (double)nsec / 1e9;
}

}  // namespace

double file_atime(const std::string& p) {
    struct stat st;
    if (lstat(p.c_str(), &st) != 0) return 0;
#if defined(__APPLE__)
    return ts_to_epoch(st.st_atime, st.st_atimespec.tv_nsec);
#else
    return ts_to_epoch(st.st_atime, st.st_atim.tv_nsec);
#endif
}

double file_mtime(const std::string& p) {
    struct stat st;
    if (lstat(p.c_str(), &st) != 0) return 0;
#if defined(__APPLE__)
    return ts_to_epoch(st.st_mtime, st.st_mtimespec.tv_nsec);
#else
    return ts_to_epoch(st.st_mtime, st.st_mtim.tv_nsec);
#endif
}

double file_ctime(const std::string& p) {
    double fallback = file_mtime(p);
    auto locked = statx_ctime(p);
    return locked.value_or(fallback);
}

std::optional<double> file_birth(const std::string& p) {
    return statx_btime(p);
}

// ---------------------------------------------------------------------------
// dispatch + read/write/delete endpoints
// ---------------------------------------------------------------------------

namespace {


std::string req_path(const HttpRequest& req, bool& missing) {
    auto it = req.query.find("path");
    if (it == req.query.end() || it->second.empty()) {
        missing = true;
        return "";
    }
    missing = false;
    return it->second;
}

}  // namespace

// -- handler includes (same TU: they use the includes above) --------------
#include "api_fs.inc"
#include "api_shell.inc"
#include "api_socket.inc"
#include "api_socket_ws.inc"
#include "api_sqlite.inc"
#include "api_sysinfo.inc"
#include "routes.inc"

// -- dispatch ---------------------------------------------------------------

namespace {

HttpResponse do_get(const HttpRequest& req, const ApiState& st) {
    if (req.path == "/api/help") {
        json auth;
        auth["required"] = st.auth_required ? "b:true" : "b:false";
        auth["open_routes"] = json::array({"/", "/api/help", "OPTIONS *"});
        auth["how"] =
            "Authorization: Bearer <token> header (preferred) or ?token= query parameter.";
        json obj;
        obj["version"] = version_str();
        obj["count"] = jenc_int((int64_t)all_routes().size());
        obj["routes"] = all_routes();
        obj["auth"] = auth;
        return send_json(200, obj);
    }
    if (req.path == "/api/dir") return handle_read_dir(req, st);
    if (req.path == "/api/file") return handle_read_file(req, st);
    if (req.path == "/") {
        json obj;
        obj["service"] = "GnumarusPythonNativeOperationsHttpService";
        obj["version"] = version_str();
        obj["help"] = "/api/help";
        return send_json(200, obj);
    }
    if (req.path == "/api/shell" || req.path == "/api/sqlite/create" ||
        req.path == "/api/sqlite/query" || req.path == "/api/socket/open" ||
        req.path == "/api/socket/send" || req.path == "/api/socket/close" ||
        req.path == "/api/socket/starttls")
        return send_json(
            405, jerr(405, "Method GET not supported for this route (use POST)."));
    if (req.path == "/api/socket/recv") return sockws::handle_socket_recv(req);
    if (req.path == "/api/socket/ws")
        return send_json(
            400, jerr(400, "This route requires a WebSocket upgrade "
                           "(Upgrade: websocket)."));
    if (req.path == "/api/sysinfo") return handle_sysinfo();
    return send_json(404, jerr(404, "Unknown route: " + req.path));
}

HttpResponse do_post(const HttpRequest& req, const ApiState& st) {
    if (req.path == "/api/file") return handle_create_file(req, st);
    if (req.path == "/api/shell") return handle_exec_shell(req, st);
    if (req.path == "/api/sqlite/create")
        return handle_sqlite_create(req, st);
    if (req.path == "/api/sqlite/query")
        return handle_sqlite_query(req, st);
    if (req.path == "/api/socket/open") return sockws::handle_socket_open(req);
    if (req.path == "/api/socket/send") return sockws::handle_socket_send(req);
    if (req.path == "/api/socket/close") return sockws::handle_socket_close(req);
    if (req.path == "/api/socket/starttls") return sockws::handle_socket_starttls(req);
    if (req.path == "/" || req.path == "/api/help" ||
        req.path == "/api/dir" || req.path == "/api/sysinfo" ||
        req.path == "/api/socket/recv" || req.path == "/api/socket/ws")
        return send_json(
            405, jerr(405, "Method POST not supported for this route."));
    return send_json(404, jerr(404, "Unknown route: " + req.path));
}

HttpResponse do_put(const HttpRequest& req, const ApiState& st) {
    if (req.path == "/api/file") return handle_overwrite_file(req, st);
    static const char* known[] = {"/",         "/api/help", "/api/dir",
                                  "/api/shell", "/api/sqlite/create",
                                  "/api/sqlite/query", "/api/sysinfo",
                                  "/api/socket/open", "/api/socket/send",
                                  "/api/socket/recv", "/api/socket/close",
                                  "/api/socket/starttls", "/api/socket/ws"};
    for (auto k : known)
        if (req.path == k)
            return send_json(
                405, jerr(405, "Method PUT not supported for this route."));
    return send_json(404, jerr(404, "Unknown route: " + req.path));
}

HttpResponse do_delete(const HttpRequest& req, const ApiState& st) {
    if (req.path == "/api/file") return handle_delete_file(req, st);
    if (req.path == "/api/dir") return handle_delete_dir(req, st);
    static const char* known[] = {"/",         "/api/help", "/api/shell",
                                  "/api/sqlite/create", "/api/sqlite/query",
                                  "/api/sysinfo", "/api/socket/open",
                                  "/api/socket/send", "/api/socket/recv",
                                  "/api/socket/close", "/api/socket/starttls",
                                  "/api/socket/ws"};
    for (auto k : known)
        if (req.path == k)
            return send_json(
                405, jerr(405, "Method DELETE not supported for this route."));
    return send_json(404, jerr(404, "Unknown route: " + req.path));
}

}  // namespace

HttpResponse api_handle(const HttpRequest& req, const ApiState& st) {
    if (req.method == "OPTIONS") {
        auto hit = req.headers.find("host");
        if (!host_allowed(hit == req.headers.end() ? "" : hit->second))
            return send_json(403, jerr(403, kHostDeniedMsg));
        HttpResponse res;
        res.status = 204;
        add_cors(res);
        return res;
    }
    if (auto unauth = check_auth(req, st)) return *unauth;
    if (req.method == "GET") return do_get(req, st);
    if (req.method == "POST") return do_post(req, st);
    if (req.method == "PUT") return do_put(req, st);
    if (req.method == "DELETE") return do_delete(req, st);
    return send_json(
        405, jerr(405, "Method " + req.method + " not supported."));
}
