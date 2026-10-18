#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <variant>

// Tagged wire scalars: JSON carries only string/array/object.
// Dynamic values travel as "i:<int>", "f:<float>", "b:true|false",
// "null"; fixed vocabulary and free text stay raw; sqlite row cells
// and params values are fully tagged (strings take "s:").

struct BadTag {
    std::string msg;
};

// Decoded tag: null / bool / int / double / string.
using Tagged = std::variant<std::nullptr_t, bool, int64_t, double, std::string>;

std::string jenc_null();
std::string jenc_bool(bool b);
std::string jenc_int(int64_t n);
std::string jenc_double(double v);  // throws on non-finite
std::string jtag_str(const std::string& s);

// Parse a tagged string; throws BadTag on violation.
Tagged jdec(const std::string& text);
