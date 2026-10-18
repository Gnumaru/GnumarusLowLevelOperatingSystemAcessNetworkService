#include "tags.h"

#include <cmath>
#include <cstdio>
#include <regex>
#include <stdexcept>

namespace {

const std::regex kIntRe("^-?(0|[1-9][0-9]*)$");
const std::regex kFloatRe(
    "^-?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][+-]?\\d+)?$");

bool valid_int(const std::string& body) {
    // Strict: regex above already anchors; reject leading '+' (jdec never
    // emits it) to match the other ports' grammar.
    if (body.empty() || body[0] == '+') return false;
    return std::regex_match(body, kIntRe);
}

bool valid_float(const std::string& body) {
    if (body.empty() || body[0] == '+') return false;
    if (!std::regex_match(body, kFloatRe)) return false;
    try {
        double v = std::stod(body);
        return std::isfinite(v);
    } catch (...) {
        return false;
    }
}

// Shortest round-trip formatting (matches Python repr / Rust {:?}).
std::string shortest(double v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.17g", v);
    return buf;
}

}  // namespace

std::string jenc_null() { return "null"; }

std::string jenc_bool(bool b) { return b ? "b:true" : "b:false"; }

std::string jenc_int(int64_t n) { return "i:" + std::to_string(n); }

std::string jenc_double(double v) {
    if (!std::isfinite(v)) throw std::runtime_error("non-finite float");
    return "f:" + shortest(v);
}

std::string jtag_str(const std::string& s) { return "s:" + s; }

Tagged jdec(const std::string& text) {
    if (text == "null") return nullptr;
    if (text.rfind("i:", 0) == 0) {
        std::string body = text.substr(2);
        if (!valid_int(body)) throw BadTag{"bad int tag: " + text};
        try {
            size_t pos = 0;
            long long n = std::stoll(body, &pos);
            if (pos != body.size()) throw BadTag{"bad int tag: " + text};
            return static_cast<int64_t>(n);
        } catch (const BadTag&) {
            throw;
        } catch (...) {
            throw BadTag{"bad int tag: " + text};
        }
    }
    if (text.rfind("f:", 0) == 0) {
        std::string body = text.substr(2);
        if (!valid_float(body)) throw BadTag{"bad float tag: " + text};
        try {
            return std::stod(body);
        } catch (...) {
            throw BadTag{"bad float tag: " + text};
        }
    }
    if (text == "b:true") return true;
    if (text == "b:false") return false;
    if (text.rfind("s:", 0) == 0) return text.substr(2);
    throw BadTag{"untagged scalar: " + text};
}
