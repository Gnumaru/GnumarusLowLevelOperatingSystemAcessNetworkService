#include "hosts.h"

#include <cctype>
#include <vector>

namespace {

std::string trim_copy(const std::string& s) {
    size_t a = s.find_first_not_of(" \t");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t");
    return s.substr(a, b - a + 1);
}

bool all_digits(const std::string& s) {
    if (s.empty()) return false;
    for (char c : s)
        if (!std::isdigit((unsigned char)c)) return false;
    return true;
}

bool is_loopback_ip(const std::string& name) {
    if (name == "::1") return true;
    std::vector<std::string> parts;
    size_t i = 0;
    while (i <= name.size()) {
        size_t dot = name.find('.', i);
        if (dot == std::string::npos) dot = name.size();
        parts.push_back(name.substr(i, dot - i));
        i = dot + 1;
    }
    if (parts.size() != 4) return false;
    for (auto& p : parts) {
        if (p.empty() || (p.size() > 1 && p[0] == '0')) return false;
        if (!all_digits(p) || p.size() > 3) return false;
        if (std::stoi(p) > 255) return false;
    }
    return std::stoi(parts[0]) == 127;
}

bool is_loopback_name(const std::string& name) {
    if (name.empty()) return false;
    std::string lower = name;
    for (auto& c : lower) c = std::tolower((unsigned char)c);
    if (lower == "localhost") return true;
    return is_loopback_ip(name);
}

}  // namespace

bool host_allowed(const std::string& header_value) {
    std::string h;
    {
        size_t a = header_value.find_first_not_of(" \t");
        if (a == std::string::npos) return false;
        size_t b = header_value.find_last_not_of(" \t");
        h = header_value.substr(a, b - a + 1);
    }
    std::string name;
    if (!h.empty() && h[0] == '[') {
        size_t end = h.find(']');
        if (end == std::string::npos) return false;
        std::string rest = h.substr(end + 1);
        if (!rest.empty()) {
            if (rest[0] != ':' || !all_digits(rest.substr(1))) return false;
        }
        name = h.substr(1, end - 1);
    } else {
        size_t colons = 0;
        for (char c : h)
            if (c == ':') ++colons;
        if (colons > 1) return false;  // bare IPv6 must be bracketed
        size_t colon = h.rfind(':');
        if (colon != std::string::npos) {
            if (!all_digits(h.substr(colon + 1))) return false;
            name = h.substr(0, colon);
        } else {
            name = h;
        }
    }
    return is_loopback_name(name);
}

bool bind_allowed(const std::string& host) {
    std::string h = trim_copy(host);
    if (h.size() > 2 && h.front() == '[' && h.back() == ']')
        h = h.substr(1, h.size() - 2);
    return is_loopback_name(h);
}
