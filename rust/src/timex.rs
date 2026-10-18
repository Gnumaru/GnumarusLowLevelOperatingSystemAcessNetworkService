//! Linux birth/ctime via raw statx (no libc wrapper needed).
//!
//! struct statx has a fixed 256-byte ABI; the size is asserted so a
//! wrong layout fails loudly instead of corrupting memory.

#[repr(C)]
#[derive(Default)]
struct StatxTs {
    tv_sec: i64,
    tv_nsec: u32,
    _reserved: i32,
}

#[repr(C)]
struct Statx {
    stx_mask: u32,
    stx_blksize: u32,
    stx_attributes: u64,
    stx_nlink: u32,
    stx_uid: u32,
    stx_gid: u32,
    stx_mode: u16,
    spare0: [u16; 1],
    stx_ino: u64,
    stx_size: u64,
    stx_blocks: u64,
    stx_attributes_mask: u64,
    stx_atime: StatxTs,
    stx_btime: StatxTs,
    stx_ctime: StatxTs,
    stx_mtime: StatxTs,
    stx_rdev_major: u32,
    stx_rdev_minor: u32,
    stx_dev_major: u32,
    stx_dev_minor: u32,
    spare2: [u64; 14],
}

unsafe extern "C" {
    fn statx(
        dirfd: i32,
        path: *const libc::c_char,
        flags: i32,
        mask: u32,
        buf: *mut Statx,
    ) -> i32;
}

const STATX_BTIME: u32 = 0x0000_0800;
const STATX_CTIME: u32 = 0x0000_0080;
const AT_FDCWD: i32 = -100;
const AT_SYMLINK_NOFOLLOW: i32 = 0x100;

fn statx_ok() -> bool {
    cfg!(target_os = "linux")
        && std::mem::size_of::<Statx>() == 256
        && (cfg!(target_arch = "x86_64") || cfg!(target_arch = "aarch64"))
}

/// (ctime, btime) epoch seconds; each None when unavailable. Never panics.
pub fn statx_times(path: &str) -> (Option<f64>, Option<f64>) {
    if !statx_ok() {
        return (None, None);
    }
    let bytes = match std::ffi::CString::new(path) {
        Ok(b) => b,
        Err(_) => return (None, None),
    };
    let mut buf = Statx {
        stx_mask: 0,
        stx_blksize: 0,
        stx_attributes: 0,
        stx_nlink: 0,
        stx_uid: 0,
        stx_gid: 0,
        stx_mode: 0,
        spare0: [0; 1],
        stx_ino: 0,
        stx_size: 0,
        stx_blocks: 0,
        stx_attributes_mask: 0,
        stx_atime: StatxTs::default(),
        stx_btime: StatxTs::default(),
        stx_ctime: StatxTs::default(),
        stx_mtime: StatxTs::default(),
        stx_rdev_major: 0,
        stx_rdev_minor: 0,
        stx_dev_major: 0,
        stx_dev_minor: 0,
        spare2: [0; 14],
    };
    // SAFETY: fixed 256-byte buffer, NUL-terminated path, size asserted.
    let ret = unsafe {
        statx(
            AT_FDCWD,
            bytes.as_ptr() as *const libc::c_char,
            AT_SYMLINK_NOFOLLOW,
            STATX_BTIME | STATX_CTIME,
            &mut buf,
        )
    };
    if ret != 0 {
        return (None, None);
    }
    let ctime = buf.stx_ctime.tv_sec as f64 + buf.stx_ctime.tv_nsec as f64 / 1e9;
    let btime = if buf.stx_mask & STATX_BTIME != 0 {
        Some(buf.stx_btime.tv_sec as f64 + buf.stx_btime.tv_nsec as f64 / 1e9)
    } else {
        None
    };
    (Some(ctime), btime)
}
