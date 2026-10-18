import * as fs from "node:fs";
import * as path from "node:path";
import { randomBytes } from "node:crypto";
import { bindAllowed } from "./hosts.js";

/** Multi-source configuration: CLI flag > env var > stdin > config.cfg > default. */

export const CONFIG_KEYS = ["host", "port", "token", "no_auth"];
export const CONFIG_FILE_NAME = "config.cfg";
export const CONFIG_MAX_BYTES = 1024 * 1024;
export const CONFIG_ENV_PREFIX = "GNUMARUS_";

export class ConfigError extends Error {}

export interface ResolvedConfig {
  host: string;
  port: number;
  token: string | null;
  noAuth: boolean;
  sources: Record<string, string>;
}

export function parseKvBytes(data: Buffer): Record<string, string> {
  if (data.includes(0)) return {};
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(data);
  } catch {
    return {};
  }
  const cfg: Record<string, string> = {};
  for (const rawLine of text.split(/\r\n|\n|\r/)) {
    const line = rawLine.trim();
    if (line === "" || line.startsWith("#") || line.startsWith(";")) continue;
    if (line.startsWith("[") && line.endsWith("]")) continue;
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim().toLowerCase();
    let value = line.slice(eq + 1).trim();
    if (
      value.length >= 2 &&
      ((value.startsWith("'") && value.endsWith("'")) ||
        (value.startsWith('"') && value.endsWith('"')))
    ) {
      value = value.slice(1, -1);
    }
    if (CONFIG_KEYS.includes(key)) cfg[key] = value;
  }
  return cfg;
}

/** Candidate config.cfg locations: working dir, dist dir, project root. */
export function configCandidates(): string[] {
  const out: string[] = [path.join(process.cwd(), CONFIG_FILE_NAME)];
  const distDir = __dirname;
  out.push(path.join(distDir, CONFIG_FILE_NAME));
  if (path.basename(distDir) === "dist") {
    out.push(path.join(path.dirname(distDir), CONFIG_FILE_NAME));
  }
  return out;
}

export function loadFileConfig(): Record<string, string> {
  for (const cand of configCandidates()) {
    try {
      if (!fs.statSync(cand).isFile()) continue;
      const fd = fs.openSync(cand, "r");
      try {
        const buf = Buffer.alloc(CONFIG_MAX_BYTES);
        const n = fs.readSync(fd, buf, 0, CONFIG_MAX_BYTES, 0);
        return parseKvBytes(buf.subarray(0, n));
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      continue;
    }
  }
  return {};
}

export function loadStdinConfig(): Record<string, string> {
  // Never block: stdin TTY (or nothing piped) means absent.
  if (process.stdin.isTTY) return {};
  try {
    const chunks: Buffer[] = [];
    let left = CONFIG_MAX_BYTES;
    while (left > 0) {
      const buf = Buffer.alloc(Math.min(65536, left));
      const n = fs.readSync(0, buf, 0, buf.length, null);
      if (n <= 0) break;
      chunks.push(buf.subarray(0, n));
      left -= n;
    }
    const data = Buffer.concat(chunks);
    if (data.length === 0) return {};
    return parseKvBytes(data);
  } catch {
    return {};
  }
}

export function parseBool(value: unknown, source: string): boolean {
  if (typeof value === "boolean") return value;
  const t = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(t)) return true;
  if (["0", "false", "no", "off"].includes(t)) return false;
  throw new ConfigError(`invalid boolean for no_auth from ${source}: '${value}' (use true/false)`);
}

export function parsePort(value: unknown, source: string): number {
  if (typeof value === "boolean") {
    throw new ConfigError(`invalid port from ${source}: '${value}' (1-65535)`);
  }
  let port: number;
  if (typeof value === "number") {
    if (!Number.isInteger(value)) {
      throw new ConfigError(`invalid port from ${source}: '${value}' (1-65535)`);
    }
    port = value;
  } else {
    const s = String(value).trim();
    if (!/^[+-]?\d+$/.test(s)) {
      throw new ConfigError(`invalid port from ${source}: '${value}' (1-65535)`);
    }
    port = parseInt(s, 10);
  }
  if (port < 1 || port > 65535) {
    throw new ConfigError(`port from ${source} out of range: ${port} (1-65535)`);
  }
  return port;
}

export function resolveConfig(
  cli: Record<string, unknown>,
  stdinCfg: Record<string, string>,
  fileCfg: Record<string, string>
): ResolvedConfig {
  const envCfg: Record<string, string> = {};
  for (const key of CONFIG_KEYS) {
    const v = process.env[CONFIG_ENV_PREFIX + key.toUpperCase()];
    if (v !== undefined && v !== "") envCfg[key] = v;
  }
  const layers: Array<[string, Record<string, unknown>]> = [
    ["cli", cli],
    ["env", envCfg],
    ["stdin", stdinCfg],
    ["file", fileCfg],
  ];
  const raw: Record<string, unknown> = {};
  const sources: Record<string, string> = {};
  for (const key of CONFIG_KEYS) {
    sources[key] = "default";
    for (const [source, layer] of layers) {
      if (Object.prototype.hasOwnProperty.call(layer, key) && layer[key] !== null) {
        raw[key] = layer[key];
        sources[key] = source;
        break;
      }
    }
  }
  // Note: explicit null is treated as absent everywhere (falls through),
  // except token/no_auth handling below which mirrors the other ports.
  const host = ((raw["host"] as string | undefined) ?? "127.0.0.1").trim();
  if (host === "") throw new ConfigError(`host from ${sources["host"]} must not be empty`);
  if (!bindAllowed(host)) {
    throw new ConfigError(
      `host from ${sources["host"]} must be a loopback address (127.0.0.1, ::1 or localhost), got '${host}'`);
  }
  const port = "port" in raw ? parsePort(raw["port"], sources["port"]) : 1423;
  const noAuth = "no_auth" in raw ? parseBool(raw["no_auth"], sources["no_auth"]) : false;
  let token: string | null = null;
  if (noAuth) {
    sources["token"] = "disabled";
  } else if ("token" in raw) {
    token = String(raw["token"]);
    if (token === "") throw new ConfigError(`token from ${sources["token"]} must not be empty`);
  } else {
    token = randomBytes(32).toString("base64url");
    sources["token"] = "generated";
  }
  return { host, port, token, noAuth, sources };
}
