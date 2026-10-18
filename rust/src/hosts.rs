//! Loopback-only bind + Host allowlist.
//!
//! Only loopback destinations are ever served: the bind address must
//! be loopback (127.0.0.0/8, ::1 or localhost) and every request's
//! Host header must name one (port part ignored). Anything else is
//! refused before any side effect (config error at startup, 403 per
//! request). std::net parsing is strict (no shorthands, no leading
//! zeros, no DNS) so all ports agree bit-for-bit.

pub const HOST_DENIED_MSG: &str =
    "Host not allowed (use 127.0.0.1, localhost or ::1).";

fn split_host(header: &str) -> Option<&str> {
    let h = header.trim();
    if h.is_empty() {
        return None;
    }
    if let Some(rest) = h.strip_prefix('[') {
        let end = rest.find(']')?;
        let tail = &rest[end + 1..];
        if !tail.is_empty() {
            let port = tail.strip_prefix(':')?;
            if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
        }
        return Some(&rest[..end]);
    }
    if h.bytes().filter(|&b| b == b':').count() > 1 {
        return None; // bare IPv6 must be bracketed
    }
    match h.rfind(':') {
        Some(i) => {
            let port = &h[i + 1..];
            if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            let name = &h[..i];
            if name.is_empty() {
                return None;
            }
            Some(name)
        }
        None => Some(h),
    }
}

fn is_loopback_name(name: &str) -> bool {
    if name.is_empty() {
        return false;
    }
    if name.eq_ignore_ascii_case("localhost") {
        return true;
    }
    is_loopback_ip(name)
}

// Strict textual loopback literals: exact "::1", or strict dotted
// quads (4 parts, no shorthands, no leading zeros) in 127.0.0.0/8.
// Deliberately NOT IpAddr::is_loopback, whose accepted shapes
// (IPv4-mapped IPv6, etc.) differ per stack.
fn is_loopback_ip(name: &str) -> bool {
    if name == "::1" {
        return true;
    }
    let parts: Vec<&str> = name.split('.').collect();
    if parts.len() != 4 {
        return false;
    }
    for p in &parts {
        if p.is_empty() || (p.len() > 1 && p.starts_with('0')) {
            return false;
        }
        if !p.bytes().all(|b| b.is_ascii_digit()) {
            return false;
        }
        if p.parse::<u32>().unwrap_or(256) > 255 {
            return false;
        }
    }
    parts[0] == "127"
}

/// Whether an HTTP Host header targets this service.
pub fn host_allowed(header_value: Option<&String>) -> bool {
    match header_value {
        None => false,
        Some(h) => match split_host(h) {
            None => false,
            Some(name) => is_loopback_name(name),
        },
    }
}

/// Whether an address may be bound: loopback only.
pub fn bind_allowed(host: &str) -> bool {
    let mut h = host.trim();
    if h.starts_with('[') && h.ends_with(']') && h.len() > 2 {
        h = &h[1..h.len() - 1];
    }
    is_loopback_name(h)
}
