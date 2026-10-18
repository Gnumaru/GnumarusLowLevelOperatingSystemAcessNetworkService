#pragma once

#include <cstdint>
#include <string>
#include <vector>

// Strict base64 (alphabet + padding checked, like Python validate=True).
// Returns empty optional on violation (note: empty input -> empty output).
std::string b64_encode(const std::string& data);
std::string b64_encode(const std::vector<uint8_t>& data);
bool b64_decode(const std::string& text, std::string& out);

// Percent-decode application/x-www-form-urlencoded style ('+' -> space).
std::string percent_decode(const std::string& s);
