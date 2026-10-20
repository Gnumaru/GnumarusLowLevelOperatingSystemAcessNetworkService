#include "config.h"
#include "hosts.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <unistd.h>
#include <vector>

namespace {

const char* kKeys[] = {"host", "port", "token", "no_auth"};
constexpr size_t kMaxBytes = 1024 * 1024;

bool is_key(const std::string& k) {
    for (auto key : kKeys)
        if (k == key) return true;
    return false;
}

std::string trim(std::string s) {
    size_t a = 0;
    while (a < s.size() && std::isspace((unsigned char)s[a])) ++a;
    size_t b = s.size();
    while (b > a && std::isspace((unsigned char)s[b - 1])) --b;
    return s.substr(a, b - a);
}

std::string lower(std::string s) {
    for (auto& c : s) c = std::tolower((unsigned char)c);
    return s;
}

// Minimal UTF-8 validation (reject surrogates/overlongs/out-of-range).
bool valid_utf8(const std::string& s) {
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

std::string mint_token() {
    // 32 random bytes -> base64url, no padding (43 chars, like the others).
    FILE* fh = fopen("/dev/urandom", "rb");
    if (!fh) throw ConfigError{"cannot read /dev/urandom"};
    unsigned char bytes[32];
    size_t got = fread(bytes, 1, sizeof(bytes), fh);
    fclose(fh);
    if (got != sizeof(bytes)) throw ConfigError{"short read from /dev/urandom"};
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

bool parse_bool(const std::string& value, const std::string& source) {
    std::string t = lower(trim(value));
    if (t == "1" || t == "true" || t == "yes" || t == "on") return true;
    if (t == "0" || t == "false" || t == "no" || t == "off") return false;
    throw ConfigError{"invalid boolean for no_auth from " + source + ": '" +
                      value + "' (use true/false)"};
}

int parse_port(const std::string& value, const std::string& source) {
    auto bad = [&]() {
        return ConfigError{"invalid port from " + source + ": '" + value +
                           "' (1-65535)"};
    };
    std::string s = trim(value);
    if (!s.empty() && s[0] == '+') s = s.substr(1);
    bool digits_only =
        !s.empty() &&
        std::all_of(s.begin(), s.end(), [](char c) {
            return std::isdigit((unsigned char)c);
        });
    long port = -1;
    if (digits_only) {
        try {
            port = std::stol(s);
        } catch (...) {
            throw bad();
        }
    } else {
        throw bad();
    }
    if (port < 1 || port > 65535)
        throw ConfigError{"port from " + source + " out of range: " +
                          std::to_string(port) + " (1-65535)"};
    return (int)port;
}

std::string exe_dir() {
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return "";
    buf[n] = 0;
    std::string p(buf);
    size_t slash = p.rfind('/');
    return slash == std::string::npos ? "" : p.substr(0, slash);
}

std::string read_capped(const std::string& path) {
    FILE* fh = fopen(path.c_str(), "rb");
    if (!fh) return "";
    std::string data;
    data.resize(kMaxBytes);
    size_t n = fread(data.data(), 1, kMaxBytes, fh);
    fclose(fh);
    data.resize(n);
    // Signal "unreadable" distinctly from empty: caller checks existence.
    return data;
}

}  // namespace

std::map<std::string, std::string> parse_kv(const std::string& data) {
    std::map<std::string, std::string> cfg;
    if (data.find('\0') != std::string::npos) return cfg;
    if (!valid_utf8(data)) return cfg;
    std::istringstream in(data);
    std::string raw;
    while (std::getline(in, raw)) {
        if (!raw.empty() && raw.back() == '\r') raw.pop_back();
        std::string line = trim(raw);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        if (line.front() == '[' && line.back() == ']') continue;
        size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        std::string key = lower(trim(line.substr(0, eq)));
        std::string value = trim(line.substr(eq + 1));
        if (value.size() >= 2 &&
            ((value.front() == '\'' && value.back() == '\'') ||
             (value.front() == '"' && value.back() == '"')))
            value = value.substr(1, value.size() - 2);
        if (is_key(key)) cfg[key] = value;
    }
    return cfg;
}

std::map<std::string, std::string> load_file_config() {
    std::vector<std::string> candidates;
    char cwd[4096];
    if (getcwd(cwd, sizeof(cwd)))
        candidates.push_back(std::string(cwd) + "/config.cfg");
    std::string ed = exe_dir();
    if (!ed.empty()) {
        candidates.push_back(ed + "/config.cfg");
        // parent of exe dir (e.g. rust/ when binary is rust/bin/GnumarusOSBridge)
        size_t slash = ed.find_last_of("/\\");
        if (slash != std::string::npos) {
            candidates.push_back(ed.substr(0, slash) + "/config.cfg");
        }
    }
    for (auto& cand : candidates) {
        FILE* fh = fopen(cand.c_str(), "rb");
        if (!fh) continue;
        fclose(fh);
        return parse_kv(read_capped(cand));
    }
    return {};
}

std::map<std::string, std::string> load_stdin_config() {
    // Never block: a terminal (or nothing piped) means absent.
    if (isatty(STDIN_FILENO)) return {};
    std::string data;
    data.resize(kMaxBytes);
    size_t total = 0;
    while (total < data.size()) {
        ssize_t n = read(STDIN_FILENO, data.data() + total, data.size() - total);
        if (n <= 0) break;
        total += (size_t)n;
    }
    if (total == 0) return {};
    data.resize(total);
    return parse_kv(data);
}

ResolvedConfig resolve_config(
    const std::map<std::string, std::string>& cli,
    const std::map<std::string, std::string>& stdin_cfg,
    const std::map<std::string, std::string>& file_cfg) {
    std::map<std::string, std::string> env_cfg;
    for (auto key : kKeys) {
        std::string name = std::string("GNUMARUS_") + key;
        for (auto& c : name) c = std::toupper((unsigned char)c);
        const char* v = getenv(name.c_str());
        if (v && *v) env_cfg[key] = v;
    }
    const std::map<std::string, std::string>* layers[] = {&cli, &env_cfg,
                                                          &stdin_cfg, &file_cfg};
    const char* layer_names[] = {"cli", "env", "stdin", "file"};
    std::map<std::string, std::string> raw, sources;
    for (auto key : kKeys) {
        sources[key] = "default";
        for (size_t i = 0; i < 4; ++i) {
            auto it = layers[i]->find(key);
            if (it != layers[i]->end()) {
                raw[key] = it->second;
                sources[key] = layer_names[i];
                break;
            }
        }
    }
    ResolvedConfig cfg;
    cfg.src_host = sources["host"];
    cfg.src_port = sources["port"];
    cfg.src_token = sources["token"];
    cfg.src_no_auth = sources["no_auth"];
    cfg.host = raw.count("host") ? trim(raw["host"]) : "127.0.0.1";
    if (cfg.host.empty())
        throw ConfigError{"host from " + cfg.src_host + " must not be empty"};
    if (!bind_allowed(cfg.host))
        throw ConfigError{"host from " + cfg.src_host +
                          " must be a loopback address (127.0.0.1, ::1 or "
                          "localhost), got '" +
                          cfg.host + "'"};
    cfg.port = raw.count("port") ? parse_port(raw["port"], cfg.src_port) : 1423;
    cfg.no_auth =
        raw.count("no_auth") ? parse_bool(raw["no_auth"], cfg.src_no_auth) : false;
    if (cfg.no_auth) {
        cfg.src_token = "disabled";
    } else if (raw.count("token")) {
        cfg.token = raw["token"];
        if (cfg.token.empty())
            throw ConfigError{"token from " + cfg.src_token + " must not be empty"};
    } else {
        cfg.token = mint_token();
        cfg.src_token = "generated";
    }
    return cfg;
}
