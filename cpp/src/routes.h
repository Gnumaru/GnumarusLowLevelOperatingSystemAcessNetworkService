#pragma once

#include <string>

#include "nlohmann/json.hpp"

using nlohmann::json;

// Service version (single source of truth).
inline const char* version_str() { return "0.10.0"; }

// Full route catalog (single source of truth for /api/help).
json all_routes();
