#include "timex.h"

#include <cstdint>
#include <cstring>
#include <string>
#include <sys/syscall.h>
#include <unistd.h>

namespace {

#pragma pack(push, 1)
struct StatxTs {
    int64_t tv_sec;
    uint32_t tv_nsec;
    int32_t reserved;
};
struct Statx {
    uint32_t stx_mask;
    uint32_t stx_blksize;
    uint64_t stx_attributes;
    uint32_t stx_nlink;
    uint32_t stx_uid;
    uint32_t stx_gid;
    uint16_t stx_mode;
    uint16_t spare0[1];
    uint64_t stx_ino;
    uint64_t stx_size;
    uint64_t stx_blocks;
    uint64_t stx_attributes_mask;
    StatxTs stx_atime;
    StatxTs stx_btime;
    StatxTs stx_ctime;
    StatxTs stx_mtime;
    uint32_t stx_rdev_major;
    uint32_t stx_rdev_minor;
    uint32_t stx_dev_major;
    uint32_t stx_dev_minor;
    uint64_t spare2[14];
};
#pragma pack(pop)

static_assert(sizeof(Statx) == 256, "struct statx must be 256 bytes");

constexpr uint32_t kBtime = 0x00000800;
constexpr uint32_t kCtime = 0x00000080;
constexpr int kFdcwd = -100;
constexpr int kNoFollow = 0x100;

double ts_to_epoch(const StatxTs& ts) {
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

// Fills ctime/btime (nullopt each when unavailable). Never throws.
void statx_times(const std::string& path, std::optional<double>& ctime,
                 std::optional<double>& btime) {
    ctime.reset();
    btime.reset();
#if defined(__linux__)
    Statx buf;
    memset(&buf, 0, sizeof(buf));
    long ret = syscall(SYS_statx, kFdcwd, path.c_str(), kNoFollow,
                       kBtime | kCtime, &buf);
    if (ret != 0) return;
    if (buf.stx_mask & kCtime) ctime = ts_to_epoch(buf.stx_ctime);
    if (buf.stx_mask & kBtime) btime = ts_to_epoch(buf.stx_btime);
#else
    (void)path;
#endif
}

}  // namespace

std::optional<double> statx_ctime(const std::string& path) {
    std::optional<double> ctime, btime;
    try {
        statx_times(path, ctime, btime);
    } catch (...) {
    }
    return ctime;
}

std::optional<double> statx_btime(const std::string& path) {
    std::optional<double> ctime, btime;
    try {
        statx_times(path, ctime, btime);
    } catch (...) {
    }
    return btime;
}
