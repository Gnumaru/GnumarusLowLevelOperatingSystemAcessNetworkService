#include "codec.h"

namespace {

const char* kB64 =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int b64_val(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

std::string encode_bytes(const uint8_t* data, size_t len) {
    std::string out;
    out.reserve((len + 2) / 3 * 4);
    for (size_t i = 0; i < len; i += 3) {
        uint32_t n = 0;
        size_t chunk = 0;
        for (size_t j = 0; j < 3 && i + j < len; ++j, ++chunk)
            n |= static_cast<uint32_t>(data[i + j]) << (16 - 8 * j);
        size_t chars = chunk == 3 ? 4 : chunk == 2 ? 3 : 2;
        for (size_t k = 0; k < chars; ++k)
            out.push_back(kB64[(n >> (18 - 6 * k)) & 63]);
        for (size_t k = chars; k < 4; ++k) out.push_back('=');
    }
    return out;
}

int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

}  // namespace

std::string b64_encode(const std::string& data) {
    return encode_bytes(reinterpret_cast<const uint8_t*>(data.data()),
                        data.size());
}

std::string b64_encode(const std::vector<uint8_t>& data) {
    return encode_bytes(data.data(), data.size());
}

bool b64_decode(const std::string& text, std::string& out) {
    out.clear();
    if (text.empty()) return true;
    if (text.size() % 4 != 0) return false;
    out.reserve(text.size() / 4 * 3);
    for (size_t i = 0; i < text.size(); i += 4) {
        int pad = 0;
        uint32_t n = 0;
        for (size_t k = 0; k < 4; ++k) {
            char c = text[i + k];
            if (c == '=') {
                if (k < 2) return false;
                ++pad;
                n <<= 6;
            } else {
                if (pad > 0) return false;
                int v = b64_val(c);
                if (v < 0) return false;
                n = (n << 6) | static_cast<uint32_t>(v);
            }
        }
        if (pad > 2) return false;
        out.push_back(static_cast<char>((n >> 16) & 0xFF));
        if (pad < 2) out.push_back(static_cast<char>((n >> 8) & 0xFF));
        if (pad < 1) out.push_back(static_cast<char>(n & 0xFF));
    }
    return true;
}

std::string percent_decode(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size();) {
        if (s[i] == '%' && i + 2 < s.size()) {
            int h = hex_val(s[i + 1]);
            int l = hex_val(s[i + 2]);
            if (h >= 0 && l >= 0) {
                out.push_back(static_cast<char>(h * 16 + l));
                i += 3;
                continue;
            }
        }
        out.push_back(s[i] == '+' ? ' ' : s[i]);
        ++i;
    }
    return out;
}
