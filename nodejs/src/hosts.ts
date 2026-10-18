// Loopback-only bind + Host allowlist.
//
// Only loopback destinations are ever served: the bind address must
// be loopback (127.0.0.0/8, ::1 or localhost) and every request's
// Host header must name one (port part ignored). Anything else is
// refused before any side effect (config error at startup, 403 per
// request). Strict dotted quads only (no shorthands, no leading
// zeros) so all ports agree bit-for-bit.

export const HOST_DENIED_MSG =
  "Host not allowed (use 127.0.0.1, localhost or ::1).";

function isLoopbackIp(name: string): boolean {
  if (name === "::1") return true;
  const parts = name.split(".");
  if (parts.length !== 4) return false;
  for (const p of parts) {
    if (!/^(0|[1-9][0-9]*)$/.test(p)) return false;
    if (parseInt(p, 10) > 255) return false;
  }
  return parseInt(parts[0], 10) === 127;
}

function isLoopbackName(name: string): boolean {
  if (name === "") return false;
  if (name.toLowerCase() === "localhost") return true;
  return isLoopbackIp(name);
}

function splitHost(headerValue: string | undefined): [string, boolean] {
  const h = (headerValue ?? "").trim();
  if (h === "") return ["", false];
  if (h[0] === "[") {
    const end = h.indexOf("]");
    if (end < 0) return ["", false];
    const rest = h.slice(end + 1);
    if (rest !== "") {
      if (!rest.startsWith(":") || !/^\d+$/.test(rest.slice(1))) {
        return ["", false];
      }
    }
    return [h.slice(1, end), true];
  }
  if ((h.match(/:/g) || []).length > 1) return ["", false];
  const i = h.lastIndexOf(":");
  if (i >= 0) {
    if (!/^\d+$/.test(h.slice(i + 1))) return ["", false];
    return [h.slice(0, i), true];
  }
  return [h, true];
}

export function hostAllowed(headerValue: string | undefined): boolean {
  const [name, ok] = splitHost(headerValue);
  if (!ok) return false;
  return isLoopbackName(name);
}

export function bindAllowed(host: string): boolean {
  let h = host.trim();
  if (h.startsWith("[") && h.endsWith("]") && h.length > 2) {
    h = h.slice(1, -1);
  }
  return isLoopbackName(h);
}
