"""OS timestamps: /etc/os-release parsing and file birth time
(st_birthtime where exposed, else Linux-only statx)."""

import ctypes
import ctypes.util
import os
import sys


def _read_os_release():
    """Parse /etc/os-release into a dict (empty when unavailable)."""
    info = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                info[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return info


try:
    _LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    class _StatxTimestamp(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_int64),
                    ("tv_nsec", ctypes.c_uint32),
                    ("__reserved", ctypes.c_int32)]

    class _Statx(ctypes.Structure):
        _fields_ = [
            ("stx_mask", ctypes.c_uint32),
            ("stx_blksize", ctypes.c_uint32),
            ("stx_attributes", ctypes.c_uint64),
            ("stx_nlink", ctypes.c_uint32),
            ("stx_uid", ctypes.c_uint32),
            ("stx_gid", ctypes.c_uint32),
            ("stx_mode", ctypes.c_uint16),
            ("__spare0", ctypes.c_uint16 * 1),
            ("stx_ino", ctypes.c_uint64),
            ("stx_size", ctypes.c_uint64),
            ("stx_blocks", ctypes.c_uint64),
            ("stx_attributes_mask", ctypes.c_uint64),
            ("stx_atime", _StatxTimestamp),
            ("stx_btime", _StatxTimestamp),
            ("stx_ctime", _StatxTimestamp),
            ("stx_mtime", _StatxTimestamp),
            ("stx_rdev_major", ctypes.c_uint32),
            ("stx_rdev_minor", ctypes.c_uint32),
            ("stx_dev_major", ctypes.c_uint32),
            ("stx_dev_minor", ctypes.c_uint32),
            ("__spare2", ctypes.c_uint64 * 14),
        ]

    _LIBC.statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                            ctypes.c_uint, ctypes.POINTER(_Statx)]
    _LIBC.statx.restype = ctypes.c_int
    # Kernel ABI: struct statx is always 256 bytes. Anything else means
    # our layout is wrong for this platform -> disable.
    _STATX_OK = ctypes.sizeof(_Statx) == 256
except Exception:
    _LIBC = None
    _STATX_OK = False

_STATX_BTIME = 0x00000800
_AT_FDCWD = -100
_AT_SYMLINK_NOFOLLOW = 0x100


def _birthtime(path, st):
    """File creation time as epoch float, or None when unavailable.

    Uses st_birthtime where the OS exposes it (macOS, Windows);
    otherwise a Linux-only statx(STATX_BTIME) syscall with a stx_mask
    check. Never raises: filesystems/kernels without birth time
    (or any error, including statx absence) yield None.
    """
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return birth
    if not sys.platform.startswith("linux") or not _STATX_OK:
        return None
    try:
        buf = _Statx()
        ret = _LIBC.statx(_AT_FDCWD, os.fsencode(path),
                          _AT_SYMLINK_NOFOLLOW, _STATX_BTIME,
                          ctypes.byref(buf))
        if ret == 0 and (buf.stx_mask & _STATX_BTIME):
            return buf.stx_btime.tv_sec + buf.stx_btime.tv_nsec / 1e9
    except (OSError, ValueError, ctypes.ArgumentError):
        pass
    return None
