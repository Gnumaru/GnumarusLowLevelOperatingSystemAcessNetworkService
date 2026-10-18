"""Loopback-only bind + Host allowlist.

Only loopback destinations are ever served: the bind address must
be loopback and every request's Host header must name one (port
part ignored). Anything else is refused before any side effect
(config error at startup, 403 per request).

Loopback literals match textually: "localhost" (case-insensitive),
strict dotted quads (4 parts, no shorthands, no leading zeros) in
127.0.0.0/8, or exactly "::1". Deliberately NOT ipaddress
semantics, whose accepted shapes differ per stack.
"""

HOST_DENIED_MSG = ("Host not allowed "
                   "(use 127.0.0.1, localhost or ::1).")


def _is_digits(text):
    return bool(text) and all("0" <= ch <= "9" for ch in text)


def _split_host(value):
    """Split a Host header into (name, ok). Port part must be numeric."""
    h = (value or "").strip()
    if not h:
        return "", False
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return "", False
        rest = h[end + 1:]
        if rest:
            if not rest.startswith(":") or not _is_digits(rest[1:]):
                return "", False
        return h[1:end], True
    if h.count(":") > 1:
        return "", False  # bare IPv6 must be bracketed
    if ":" in h:
        name, _, port = h.rpartition(":")
        if not _is_digits(port):
            return "", False
    else:
        name = h
    return name, True


def _is_loopback_ip(name):
    if name == "::1":
        return True
    parts = name.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part or (len(part) > 1 and part[0] == "0"):
            return False
        if not _is_digits(part):
            return False
        if int(part) > 255:
            return False
    return parts[0] == "127"


def _is_loopback_name(name):
    if not name:
        return False
    if name.lower() == "localhost":
        return True
    return _is_loopback_ip(name)


def host_allowed(header_value):
    """Whether an HTTP Host header targets this service."""
    name, ok = _split_host(header_value)
    if not ok:
        return False
    return _is_loopback_name(name)


def bind_allowed(host):
    """Whether an address may be bound: loopback only."""
    h = host.strip()
    if h.startswith("[") and h.endswith("]") and len(h) > 2:
        h = h[1:-1]
    return _is_loopback_name(h)
