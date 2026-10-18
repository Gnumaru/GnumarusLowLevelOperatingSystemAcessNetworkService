#pragma once

#include <string>

// Loopback-only bind + Host allowlist.
//
// Only loopback destinations are ever served: the bind address must
// be loopback (127.0.0.0/8, ::1 or localhost) and every request's
// Host header must name one (port part ignored). Anything else is
// refused before any side effect (config error at startup, 403 per
// request). Manual dotted-quad parsing (no shorthands, no leading
// zeros) so all ports agree bit-for-bit.

inline const char* kHostDeniedMsg =
    "Host not allowed (use 127.0.0.1, localhost or ::1).";

bool host_allowed(const std::string& header_value);
bool bind_allowed(const std::string& host);
