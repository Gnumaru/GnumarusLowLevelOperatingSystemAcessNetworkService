#pragma once

#include <optional>
#include <string>

// Linux birth/ctime via the raw statx syscall (no libc wrapper needed).
// struct statx has a fixed 256-byte ABI; the size is asserted so a
// wrong layout fails loudly instead of corrupting memory.
// Each returns nullopt when unavailable. Never throws.
std::optional<double> statx_ctime(const std::string& path);
std::optional<double> statx_btime(const std::string& path);
