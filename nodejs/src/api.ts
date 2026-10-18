import * as http from "node:http";
import * as net from "node:net";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import * as tls from "node:tls";
import { spawn, ChildProcess } from "node:child_process";
import { timingSafeEqual, randomBytes, createHash, X509Certificate } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import { jenc, jtagStr, jdec, BadTag } from "./tags.js";
import { hostAllowed, bindAllowed, HOST_DENIED_MSG } from "./hosts.js";

export const VERSION = "0.10.0";
const SERVICE = "GnumarusPythonNativeOperationsHttpService";
const OPEN_ROUTES = new Set(["/", "/api/help"]);

const SHELL_DEFAULT_TIMEOUT = 30;
const SHELL_MAX_TIMEOUT = 300;
const SHELL_DEFAULT_MAX_OUTPUT = 1024 * 1024;
const SHELL_MAX_OUTPUT = 100 * 1024 * 1024;
const SQLITE_DEFAULT_TIMEOUT = 5;
const SQLITE_MAX_TIMEOUT = 300;

export interface Opts {
  authRequired: boolean;
  authToken: string;
  cwd: string;
}

type Json = string | Json[] | { [k: string]: Json };
type Handler = (
  req: http.IncomingMessage,
  res: http.ServerResponse,
  url: URL,
  query: URLSearchParams,
  opts: Opts
) => void | Promise<void>;

// ---------------------------------------------------------------------------
// plumbing
// ---------------------------------------------------------------------------

function cors(res: http.ServerResponse): void {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With");
  res.setHeader("Access-Control-Max-Age", "86400");
}

function sendJson(res: http.ServerResponse, status: number, obj: Json): void {
  const body = Buffer.from(JSON.stringify(obj), "utf8");
  cors(res);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": body.length,
  });
  res.end(body);
}

function sendRaw(res: http.ServerResponse, status: number, body: Buffer, contentType: string): void {
  cors(res);
  res.writeHead(status, {
    "Content-Type": contentType,
    "Content-Length": body.length,
  });
  res.end(body);
}

function label(status: number): string {
  switch (status) {
    case 400: return "bad_request";
    case 401: return "unauthorized";
    case 403: return "forbidden";
    case 404: return "not_found";
    case 405: return "method_not_allowed";
    case 409: return "conflict";
    default: return "error";
  }
}

function err(res: http.ServerResponse, status: number, message: string, path?: string): void {
  const obj: { [k: string]: Json } = { error: label(status), message };
  if (path !== undefined) obj["path"] = path;
  sendJson(res, status, obj);
}

function readBody(req: http.IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function resolvePath(rawPath: string, cwd: string): string {
  let p = rawPath;
  if (p === "~" || p.startsWith("~/")) p = os.homedir() + p.slice(1);
  const abs = path.isAbsolute(p) ? p : path.join(cwd, p);
  return path.normalize(abs);
}

function lexists(p: string): boolean {
  try {
    fs.lstatSync(p);
    return true;
  } catch {
    return false;
  }
}

function entryType(p: string): string {
  try {
    const st = fs.lstatSync(p);
    if (st.isSymbolicLink()) return "symlink";
    if (st.isDirectory()) return "dir";
    if (st.isFile()) return "file";
    return "other";
  } catch {
    return "other";
  }
}

function modeString(p: string): string {
  try {
    const mode = fs.lstatSync(p).mode & 0o777;
    return "0o" + mode.toString(8);
  } catch {
    return "0o644";
  }
}

function epochSec(ms: number): number {
  return ms / 1000;
}

const MIME_BY_EXT: Record<string, string> = {
  txt: "text/plain", html: "text/html", htm: "text/html",
  json: "application/json", xml: "text/xml", css: "text/css",
  js: "text/javascript", png: "image/png", jpg: "image/jpeg",
  jpeg: "image/jpeg", gif: "image/gif", pdf: "application/pdf",
  zip: "application/zip", csv: "text/csv", md: "text/markdown",
};

function guessMime(p: string): string {
  const dot = p.lastIndexOf(".");
  if (dot < 0) return "application/octet-stream";
  return MIME_BY_EXT[p.slice(dot + 1).toLowerCase()] ?? "application/octet-stream";
}

function decodeStrict(buf: Buffer): string | null {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    return null;
  }
}

function hasBinaryControls(s: string): boolean {
  for (const ch of s) {
    const c = ch.codePointAt(0) ?? 0;
    if (c < 32 && c !== 9 && c !== 10 && c !== 13) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// SQL helpers
// ---------------------------------------------------------------------------

class BadSql extends Error {}

interface SqlScan {
  statements: string[];
  first: string;
  returning: boolean;
}

function scanSql(sql: string): SqlScan {
  const stmts: string[] = [];
  let cur = "";
  let first: string | null = null;
  let returning = false;
  let word = "";
  const flush = () => {
    if (word !== "") {
      const w = word.toUpperCase();
      if (first === null) first = w;
      if (w === "RETURNING") returning = true;
      word = "";
    }
  };
  const isWord = (c: string) => /[A-Za-z_]/.test(c);
  let state = 0;
  let i = 0;
  const n = sql.length;
  while (i < n) {
    const c = sql[i];
    if (state === 0) {
      if (c === "'") { state = 1; cur += c; i++; }
      else if (c === '"') { state = 2; cur += c; i++; }
      else if (c === "`") { state = 3; cur += c; i++; }
      else if (c === "-" && sql[i + 1] === "-") { state = 4; i += 2; }
      else if (c === "/" && sql[i + 1] === "*") { state = 5; i += 2; }
      else if (c === ";") {
        flush();
        if (cur.trim() !== "") stmts.push(cur);
        cur = ""; i++;
      }
      else if (isWord(c)) { word += c; cur += c; i++; }
      else { if (!/[A-Za-z0-9_$]/.test(c)) flush(); cur += c; i++; }
    } else if (state === 1) {
      if (c === "'" && sql[i + 1] === "'") { cur += "''"; i += 2; }
      else { cur += c; i++; if (c === "'") state = 0; }
    } else if (state === 2) {
      if (c === '"' && sql[i + 1] === '"') { cur += '""'; i += 2; }
      else { cur += c; i++; if (c === '"') state = 0; }
    } else if (state === 3) {
      cur += c; i++;
      if (c === "`") state = 0;
    } else if (state === 4) {
      if (c === "\n") state = 0;
      i++;
    } else {
      if (c === "*" && sql[i + 1] === "/") { state = 0; i += 2; }
      else i++;
    }
  }
  flush();
  if (cur.trim() !== "") stmts.push(cur);
  return { statements: stmts, first: first ?? "", returning };
}

const ROW_FIRST = new Set(["SELECT", "VALUES", "WITH", "EXPLAIN", "PRAGMA", "TABLE"]);
const DDL_FIRST = new Set(["CREATE", "DROP", "ALTER", "VACUUM", "ANALYZE", "REINDEX",
  "ATTACH", "DETACH", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "END"]);

// ---------------------------------------------------------------------------
// handler
// ---------------------------------------------------------------------------

function checkAuth(req: http.IncomingMessage, res: http.ServerResponse, url: URL, opts: Opts): boolean {
  if (!opts.authRequired) return true;
  if (req.method === "GET" && OPEN_ROUTES.has(url.pathname)) return true;
  let supplied = "";
  const h = (req.headers["authorization"] as string | undefined) ?? "";
  if (h.startsWith("Bearer ")) supplied = h.slice(7).trim();
  if (supplied === "") supplied = url.searchParams.get("token") ?? "";
  const a = Buffer.from(supplied, "utf8");
  const b = Buffer.from(opts.authToken, "utf8");
  if (opts.authToken !== "" && a.length === b.length && timingSafeEqual(a, b)) return true;
  cors(res);
  res.writeHead(401, {
    "Content-Type": "application/json; charset=utf-8",
    "WWW-Authenticate": "Bearer",
  });
  res.end(JSON.stringify({ error: "unauthorized",
    message: "Valid API token required: send Authorization: Bearer <token> or ?token=." }));
  return false;
}

function reqPath(query: URLSearchParams): string | null {
  const raw = query.get("path");
  if (raw === null || raw === "") return null;
  return raw;
}

function errPath(res: http.ServerResponse): void {
  err(res, 400, "Missing required query parameter: path");
}

async function handle(
  req: http.IncomingMessage,
  res: http.ServerResponse,
  opts: Opts
): Promise<void> {
  const url = new URL(req.url ?? "/", "http://x");
  const pathname = url.pathname;
  const query = url.searchParams;
  const method = req.method ?? "GET";
  if (!hostAllowed(req.headers["host"] as string | undefined)) {
    err(res, 403, HOST_DENIED_MSG);
    return;
  }
  if (method === "OPTIONS") {
    cors(res);
    res.writeHead(204);
    res.end();
    return;
  }
  if (!checkAuth(req, res, url, opts)) return;
  try {
    if (method === "GET") await doGet(req, res, pathname, query, opts);
    else if (method === "POST") await doPost(req, res, pathname, query, opts);
    else if (method === "PUT") await doPut(req, res, pathname, query, opts);
    else if (method === "DELETE") await doDelete(req, res, pathname, query, opts);
    else err(res, 405, `Method ${method} not supported.`);
  } catch (e) {
    try {
      err(res, 500, `Internal error: ${(e as Error).message}`);
    } catch { /* ignore */ }
  }
}

async function doGet(
  req: http.IncomingMessage, res: http.ServerResponse,
  pathname: string, query: URLSearchParams, opts: Opts
): Promise<void> {
  switch (pathname) {
    case "/api/help":
      sendJson(res, 200, {
        version: VERSION,
        count: jenc(ROUTES.length),
        routes: ROUTES,
        auth: {
          required: jenc(opts.authRequired),
          open_routes: [...OPEN_ROUTES, "OPTIONS *"],
          how: "Authorization: Bearer <token> header (preferred) or ?token= query parameter.",
        },
      });
      return;
    case "/api/dir": handleReadDir(res, query, opts); return;
    case "/api/file": handleReadFile(res, query, opts); return;
    case "/":
      sendJson(res, 200, { service: SERVICE, version: VERSION, help: "/api/help" });
      return;
    case "/api/shell":
    case "/api/sqlite/create":
    case "/api/sqlite/query":
    case "/api/socket/open":
    case "/api/socket/send":
    case "/api/socket/close":
    case "/api/socket/starttls":
      err(res, 405, "Method GET not supported for this route (use POST).");
      return;
    case "/api/socket/recv": await handleSocketRecv(res, query); return;
    case "/api/socket/ws":
      err(res, 400, "This route requires a WebSocket upgrade (Upgrade: websocket).");
      return;
    case "/api/sysinfo": handleSysinfo(res); return;
    default: err(res, 404, `Unknown route: ${pathname}`);
  }
}

async function doPost(
  req: http.IncomingMessage, res: http.ServerResponse,
  pathname: string, query: URLSearchParams, opts: Opts
): Promise<void> {
  switch (pathname) {
    case "/api/file": handleCreateFile(req, res, query, opts); return;
    case "/api/shell": await handleExecShell(req, res, opts); return;
    case "/api/sqlite/create": handleSqliteCreate(req, res, query, opts); return;
    case "/api/sqlite/query": await handleSqliteQuery(req, res, query, opts); return;
    case "/api/socket/open": await handleSocketOpen(req, res); return;
    case "/api/socket/send": await handleSocketSend(req, res, query); return;
    case "/api/socket/close": handleSocketClose(res, query); return;
    case "/api/socket/starttls": await handleSocketStarttls(req, res, query); return;
    case "/":
    case "/api/help":
    case "/api/dir":
    case "/api/sysinfo":
    case "/api/socket/recv":
    case "/api/socket/ws":
      err(res, 405, "Method POST not supported for this route.");
      return;
    default: err(res, 404, `Unknown route: ${pathname}`);
  }
}

async function doPut(
  req: http.IncomingMessage, res: http.ServerResponse,
  pathname: string, query: URLSearchParams, opts: Opts
): Promise<void> {
  if (pathname === "/api/file") {
    handleOverwriteFile(req, res, query, opts);
    return;
  }
  if (["/", "/api/help", "/api/dir", "/api/shell", "/api/sqlite/create",
    "/api/sqlite/query", "/api/sysinfo", "/api/socket/open",
    "/api/socket/send", "/api/socket/recv",
    "/api/socket/close", "/api/socket/starttls", "/api/socket/ws"].includes(pathname)) {
    err(res, 405, "Method PUT not supported for this route.");
    return;
  }
  err(res, 404, `Unknown route: ${pathname}`);
}

async function doDelete(
  req: http.IncomingMessage, res: http.ServerResponse,
  pathname: string, query: URLSearchParams, opts: Opts
): Promise<void> {
  if (pathname === "/api/file") {
    handleDeleteFile(res, query, opts);
    return;
  }
  if (pathname === "/api/dir") {
    handleDeleteDir(res, query, opts);
    return;
  }
  if (["/", "/api/help", "/api/shell", "/api/sqlite/create",
    "/api/sqlite/query", "/api/sysinfo", "/api/socket/open",
    "/api/socket/send", "/api/socket/recv",
    "/api/socket/close", "/api/socket/starttls", "/api/socket/ws"].includes(pathname)) {
    err(res, 405, "Method DELETE not supported for this route.");
    return;
  }
  err(res, 404, `Unknown route: ${pathname}`);
}

// -- read endpoints ----------------------------------------------------------

function handleReadDir(res: http.ServerResponse, query: URLSearchParams, opts: Opts): void {
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (!lexists(abs)) { err(res, 404, "Path does not exist", abs); return; }
  let stat: fs.Stats;
  try {
    stat = fs.statSync(abs);
  } catch {
    err(res, 404, "Path does not exist", abs); return;
  }
  if (!stat.isDirectory()) { err(res, 400, "Path is not a directory", abs); return; }
  let names: string[];
  try {
    names = fs.readdirSync(abs).sort();
  } catch {
    err(res, 403, "Cannot list directory", abs); return;
  }
  const entries: Json[] = [];
  for (const name of names) {
    try {
      const full = path.join(abs, name);
      const st = fs.lstatSync(full);
      entries.push({
        name, path: full,
        type: entryType(full),
        size: jenc(st.size),
        atime: jenc(epochSec(st.atimeMs)),
        mtime: jenc(epochSec(st.mtimeMs)),
        ctime: jenc(epochSec(st.ctimeMs)),
        crtime: st.birthtimeMs === 0 ? "null" : jenc(epochSec(st.birthtimeMs)),
        mode: modeString(full),
      });
    } catch {
      continue;
    }
  }
  sendJson(res, 200, {
    path: raw, absolute_path: abs,
    count: jenc(entries.length), entries,
  });
}

function handleReadFile(res: http.ServerResponse, query: URLSearchParams, opts: Opts): void {
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (!lexists(abs)) { err(res, 404, "Path does not exist", abs); return; }
  try {
    if (fs.statSync(abs).isDirectory() && !fs.lstatSync(abs).isSymbolicLink()) {
      err(res, 400, "Path is a directory, not a file", abs); return;
    }
  } catch {
    err(res, 404, "Path does not exist", abs); return;
  }
  const encoding = (query.get("encoding") ?? "auto").toLowerCase();
  if (!["auto", "text", "base64"].includes(encoding)) {
    err(res, 400, `Invalid encoding param: '${encoding}' (use auto|text|base64)`); return;
  }
  const rawMode = query.get("raw") === "1";
  let data: Buffer;
  try {
    data = fs.readFileSync(abs);
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    if (code === "ENOENT") { err(res, 404, "Path does not exist", abs); return; }
    if (code === "EACCES" || code === "EPERM") { err(res, 403, "Permission denied", abs); return; }
    if (code === "EISDIR") { err(res, 400, "Path is a directory, not a file", abs); return; }
    err(res, 403, `Cannot read file: ${(e as Error).message}`, abs); return;
  }
  let mtime: string = "null";
  try {
    mtime = jenc(epochSec(fs.lstatSync(abs).mtimeMs));
  } catch { /* keep null */ }
  if (rawMode) {
    sendRaw(res, 200, data, guessMime(abs));
    return;
  }
  const mime = guessMime(abs);
  const meta = (binary: boolean, enc: string, content: string) => ({
    path: raw, absolute_path: abs,
    size: jenc(data.length), mtime,
    mime_type: mime, is_binary: jenc(binary), encoding: enc,
    content,
  });
  if (encoding === "text") {
    sendJson(res, 200, meta(false, "utf-8", data.toString("utf8")));
    return;
  }
  if (encoding === "base64") {
    sendJson(res, 200, meta(true, "base64", data.toString("base64")));
    return;
  }
  const text = decodeStrict(data);
  if (text !== null && !hasBinaryControls(text)) {
    sendJson(res, 200, meta(false, "utf-8", text));
  } else {
    sendJson(res, 200, meta(true, "base64", data.toString("base64")));
  }
}

// -- write/delete endpoints ----------------------------------------------------

function parseWriteBody(req: http.IncomingMessage, body: Buffer): { ok: true; data: Buffer } | { ok: false } {
  const ct = (req.headers["content-type"] as string | undefined) ?? "";
  if (!ct.includes("application/json")) return { ok: true, data: body };
  let obj: unknown = {};
  if (body.length > 0) {
    try {
      obj = JSON.parse(body.toString("utf8"));
    } catch {
      return { ok: false, _e: "Invalid JSON body." } as unknown as { ok: false };
    }
  }
  if (typeof obj !== "object" || obj === null || Array.isArray(obj)) {
    (req as unknown as { _e: string })._e = "JSON body must be an object.";
    return { ok: false };
  }
  const o = obj as Record<string, unknown>;
  const content: unknown = "content" in o ? o["content"] : null;
  let encoding = "text";
  if ("encoding" in o && o["encoding"] !== null) {
    if (typeof o["encoding"] !== "string") {
      (req as unknown as { _e: string })._e = "Invalid encoding: (use text|base64).";
      return { ok: false };
    }
    encoding = (o["encoding"] as string).toLowerCase();
  }
  if (encoding === "text") {
    if (content === null || content === undefined) return { ok: true, data: Buffer.alloc(0) };
    if (typeof content !== "string") {
      (req as unknown as { _e: string })._e = "'content' must be a string for text encoding.";
      return { ok: false };
    }
    return { ok: true, data: Buffer.from(content, "utf8") };
  }
  if (encoding === "base64") {
    if (content === null || content === undefined) return { ok: true, data: Buffer.alloc(0) };
    if (typeof content !== "string") {
      (req as unknown as { _e: string })._e = "'content' must be a base64 string.";
      return { ok: false };
    }
    // Buffer.from(x, "base64") is lenient (skips bad chars); validate
    // strictly like the other ports: alphabet + correct padding.
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(content) || content.length % 4 !== 0) {
      (req as unknown as { _e: string })._e = "Invalid base64 'content'.";
      return { ok: false };
    }
    return { ok: true, data: Buffer.from(content, "base64") };
  }
  (req as unknown as { _e: string })._e = `Invalid encoding: '${encoding}' (use text|base64).`;
  return { ok: false };
}

function writeBodyError(req: http.IncomingMessage, res: http.ServerResponse): void {
  const msg = (req as unknown as { _e?: string })._e ?? "Invalid JSON body.";
  err(res, 400, msg);
}

async function handleCreateFile(
  req: http.IncomingMessage, res: http.ServerResponse,
  query: URLSearchParams, opts: Opts
): Promise<void> {
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (lexists(abs)) {
    err(res, 409, "Path already exists (use PUT to overwrite).", abs); return;
  }
  const parent = path.dirname(abs);
  try {
    if (!fs.statSync(parent).isDirectory()) throw new Error("x");
  } catch {
    err(res, 404, "Parent directory does not exist.", abs); return;
  }
  const body = await readBody(req);
  const parsed = parseWriteBody(req, body);
  if (!parsed.ok) { writeBodyError(req, res); return; }
  try {
    fs.writeFileSync(abs, parsed.data, { flag: "wx" });
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    if (code === "EEXIST") {
      err(res, 409, "Path already exists (use PUT to overwrite).", abs); return;
    }
    if (code === "ENOENT") {
      err(res, 404, "Parent directory does not exist.", abs); return;
    }
    if (code === "EACCES" || code === "EPERM") {
      err(res, 403, "Permission denied", abs); return;
    }
    err(res, 400, `Cannot create file: ${(e as Error).message}`, abs); return;
  }
  sendJson(res, 201, {
    path: raw, absolute_path: abs,
    size: jenc(parsed.data.length), mime_type: guessMime(abs),
  });
}

async function handleOverwriteFile(
  req: http.IncomingMessage, res: http.ServerResponse,
  query: URLSearchParams, opts: Opts
): Promise<void> {
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (!lexists(abs)) { err(res, 404, "Path does not exist", abs); return; }
  try {
    if (fs.statSync(abs).isDirectory() && !fs.lstatSync(abs).isSymbolicLink()) {
      err(res, 400, "Path is a directory, not a file", abs); return;
    }
  } catch {
    err(res, 404, "Path does not exist", abs); return;
  }
  const body = await readBody(req);
  const parsed = parseWriteBody(req, body);
  if (!parsed.ok) { writeBodyError(req, res); return; }
  try {
    fs.writeFileSync(abs, parsed.data);
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    if (code === "EACCES" || code === "EPERM") {
      err(res, 403, "Permission denied", abs); return;
    }
    if (code === "ENOENT") {
      err(res, 404, "Path does not exist", abs); return;
    }
    err(res, 400, `Cannot write file: ${(e as Error).message}`, abs); return;
  }
  sendJson(res, 200, {
    path: raw, absolute_path: abs,
    size: jenc(parsed.data.length), mime_type: guessMime(abs),
  });
}

function handleDeleteFile(res: http.ServerResponse, query: URLSearchParams, opts: Opts): void {
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (!lexists(abs)) { err(res, 404, "Path does not exist", abs); return; }
  try {
    if (fs.statSync(abs).isDirectory() && !fs.lstatSync(abs).isSymbolicLink()) {
      err(res, 400, "Path is a directory, not a file (use DELETE /api/dir).", abs); return;
    }
  } catch {
    err(res, 404, "Path does not exist", abs); return;
  }
  try {
    fs.unlinkSync(abs);
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    if (code === "ENOENT") { err(res, 404, "Path does not exist", abs); return; }
    if (code === "EACCES" || code === "EPERM") { err(res, 403, "Permission denied", abs); return; }
    err(res, 400, `Cannot delete file: ${(e as Error).message}`, abs); return;
  }
  sendJson(res, 200, { path: raw, absolute_path: abs, deleted: jenc(true) });
}

function handleDeleteDir(res: http.ServerResponse, query: URLSearchParams, opts: Opts): void {
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (!lexists(abs)) { err(res, 404, "Path does not exist", abs); return; }
  try {
    if (fs.lstatSync(abs).isSymbolicLink() || !fs.statSync(abs).isDirectory()) {
      err(res, 400, "Path is not a directory", abs); return;
    }
  } catch {
    err(res, 404, "Path does not exist", abs); return;
  }
  try {
    fs.rmdirSync(abs);
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    if (code === "ENOENT") { err(res, 404, "Path does not exist", abs); return; }
    if (code === "EACCES" || code === "EPERM") { err(res, 403, "Permission denied", abs); return; }
    if (code === "ENOTEMPTY" || code === "EEXIST") {
      err(res, 400, `Cannot delete directory: Directory not empty: '${abs}'`, abs); return;
    }
    err(res, 400, `Cannot delete directory: ${(e as Error).message}`, abs); return;
  }
  sendJson(res, 200, { path: raw, absolute_path: abs, deleted: jenc(true) });
}

// -- shell ---------------------------------------------------------------------

class CappedAcc {
  buf = Buffer.alloc(0);
  truncated = false;
  constructor(readonly cap: number) {}
  push(chunk: Buffer): void {
    const room = this.cap - this.buf.length;
    if (room > 0) this.buf = Buffer.concat([this.buf, chunk.subarray(0, room)]);
    if (chunk.length > Math.max(room, 0)) this.truncated = true;
  }
  text(): string {
    return this.buf.toString("utf8");
  }
}

async function handleExecShell(
  req: http.IncomingMessage, res: http.ServerResponse, opts: Opts
): Promise<void> {
  const body = await readBody(req);
  const ct = (req.headers["content-type"] as string | undefined) ?? "";
  if (!ct.includes("application/json")) {
    err(res, 400, "Content-Type must be application/json with body { command, timeout?, cwd?, max_stdout?, max_stderr? }.");
    return;
  }
  let obj: Record<string, unknown>;
  try {
    const v: unknown = body.length === 0 ? {} : JSON.parse(body.toString("utf8"));
    if (typeof v !== "object" || v === null || Array.isArray(v)) throw new Error("x");
    obj = v as Record<string, unknown>;
  } catch {
    err(res, 400, "Invalid JSON body.");
    return;
  }
  const command = obj["command"];
  if (typeof command !== "string" || command.trim() === "") {
    err(res, 400, "Missing or empty required field: command.");
    return;
  }
  let timeout = SHELL_DEFAULT_TIMEOUT;
  if ("timeout" in obj && obj["timeout"] !== null) {
    let t: unknown;
    try {
      t = jdec(obj["timeout"]);
    } catch {
      err(res, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
      return;
    }
    if (typeof t !== "number") {
      err(res, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
      return;
    }
    timeout = Math.max(1, Math.min(t, SHELL_MAX_TIMEOUT));
  }
  let cwd = opts.cwd;
  if ("cwd" in obj && obj["cwd"] !== null) {
    if (typeof obj["cwd"] !== "string") {
      err(res, 400, "Field 'cwd' must be a string.");
      return;
    }
    cwd = resolvePath(obj["cwd"] as string, opts.cwd);
    try {
      if (!fs.statSync(cwd).isDirectory()) throw new Error("x");
    } catch {
      err(res, 400, "Working directory does not exist.", cwd);
      return;
    }
  }
  function limitOf(key: string): number | null {
    if (!(key in obj) || obj[key] === null) return SHELL_DEFAULT_MAX_OUTPUT;
    let v: unknown;
    try {
      if (typeof obj[key] !== "string" || !(obj[key] as string).startsWith("i:")) {
        throw new Error("not an int tag");
      }
      v = jdec(obj[key]);
    } catch {
      err(res, 400, `Field '${key}' must be an integer >= 0 (i:<int>).`);
      return null;
    }
    if (typeof v !== "number" || !Number.isInteger(v) || v < 0) {
      err(res, 400, `Field '${key}' must be an integer >= 0 (i:<int>).`);
      return null;
    }
    if (v > SHELL_MAX_OUTPUT) {
      err(res, 400, `Field '${key}' exceeds the ${SHELL_MAX_OUTPUT} byte cap.`);
      return null;
    }
    return v;
  }
  const maxOut = limitOf("max_stdout");
  if (maxOut === null) return;
  const maxErr = limitOf("max_stderr");
  if (maxErr === null) return;
  let stdinRaw = "";
  if ("stdin" in obj && obj["stdin"] !== null) {
    if (typeof obj["stdin"] !== "string") {
      err(res, 400, "Field 'stdin' must be a string.");
      return;
    }
    stdinRaw = obj["stdin"] as string;
  }
  let stdinEnc = "text";
  if ("stdin_encoding" in obj && obj["stdin_encoding"] !== null) {
    if (typeof obj["stdin_encoding"] !== "string") {
      err(res, 400, "Field 'stdin_encoding' must be text|base64.");
      return;
    }
    stdinEnc = (obj["stdin_encoding"] as string).toLowerCase();
  }
  let stdinData: Buffer;
  if (stdinEnc === "text") {
    stdinData = Buffer.from(stdinRaw, "utf8");
  } else if (stdinEnc === "base64") {
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(stdinRaw) || stdinRaw.length % 4 !== 0) {
      err(res, 400, "Invalid base64 'stdin'.");
      return;
    }
    stdinData = Buffer.from(stdinRaw, "base64");
  } else {
    err(res, 400, "Field 'stdin_encoding' must be text|base64.");
    return;
  }

  let child: ChildProcess;
  try {
    child = spawn("bash", ["-c", command], {
      cwd,
      detached: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch (e) {
    err(res, 400, `Cannot start shell: ${(e as Error).message}`);
    return;
  }
  const outAcc = new CappedAcc(maxOut);
  const errAcc = new CappedAcc(maxErr);
  child.stdout?.on("data", (c: Buffer) => outAcc.push(c));
  child.stderr?.on("data", (c: Buffer) => errAcc.push(c));
  if (child.stdin) {
    // Queued internally by node; error listener swallows EPIPE when the
    // command exits without reading. end() closes -> child sees EOF.
    child.stdin.on("error", () => {});
    child.stdin.end(stdinData);
  }
  const result = await new Promise<{ code: number; timedOut: boolean }>((resolve) => {
    const timer = setTimeout(() => {
      try {
        if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
      } catch { /* already gone */ }
      try {
        child.kill("SIGKILL");
      } catch { /* ignore */ }
      resolve({ code: 124, timedOut: true });
    }, timeout * 1000);
    timer.unref?.();
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 0, timedOut: false });
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve({ code: 127, timedOut: false });
    });
  });
  // Let any straggler output flush (mirrors the 5s drain join).
  await new Promise((r) => setTimeout(r, 50));
  sendJson(res, 200, {
    command, cwd,
    returncode: jenc(result.code),
    stdout: outAcc.text(), stderr: errAcc.text(),
    timed_out: jenc(result.timedOut),
    stdout_truncated: jenc(outAcc.truncated),
    stderr_truncated: jenc(errAcc.truncated),
    max_stdout: jenc(maxOut), max_stderr: jenc(maxErr),
    stdin_bytes: jenc(stdinData.length),
  });
}

// -- sqlite ----------------------------------------------------------------------

let _sqliteOk: boolean | null = null;
function sqliteAvailable(): boolean {
  if (_sqliteOk !== null) return _sqliteOk;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { DatabaseSync } = require("node:sqlite") as typeof import("node:sqlite");
    const db = new DatabaseSync(":memory:");
    db.close();
    _sqliteOk = true;
  } catch {
    _sqliteOk = false;
  }
  return _sqliteOk;
}

function handleSqliteCreate(
  req: http.IncomingMessage, res: http.ServerResponse,
  query: URLSearchParams, opts: Opts
): void {
  if (!sqliteAvailable()) {
    err(res, 503, "SQLite support unavailable: node:sqlite failed to load.");
    return;
  }
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (lexists(abs)) {
    err(res, 409, "Database file already exists.", abs); return;
  }
  const parent = path.dirname(abs);
  try {
    if (!fs.statSync(parent).isDirectory()) throw new Error("x");
  } catch {
    err(res, 404, "Parent directory does not exist.", abs); return;
  }
  let version: string;
  try {
    const { DatabaseSync } = require("node:sqlite") as typeof import("node:sqlite");
    const db = new DatabaseSync(abs);
    try {
      const row = db.prepare("SELECT sqlite_version()").get() as Record<string, unknown>;
      version = String(Object.values(row)[0]);
    } finally {
      db.close();
    }
  } catch (e) {
    try {
      fs.unlinkSync(abs);
    } catch { /* ignore */ }
    err(res, 400, `Cannot create database: ${(e as Error).message}`, abs); return;
  }
  let size = 0;
  try {
    size = fs.statSync(abs).size;
  } catch { /* keep 0 */ }
  sendJson(res, 201, {
    path: raw, absolute_path: abs,
    size: jenc(size), sqlite_version: version,
  });
}

async function handleSqliteQuery(
  req: http.IncomingMessage, res: http.ServerResponse,
  query: URLSearchParams, opts: Opts
): Promise<void> {
  if (!sqliteAvailable()) {
    err(res, 503, "SQLite support unavailable: node:sqlite failed to load.");
    return;
  }
  const raw = reqPath(query);
  if (raw === null) { errPath(res); return; }
  const abs = resolvePath(raw, opts.cwd);
  if (!lexists(abs)) {
    err(res, 404, "Database file does not exist", abs); return;
  }
  const body = await readBody(req);
  const ct = (req.headers["content-type"] as string | undefined) ?? "";
  if (!ct.includes("application/json")) {
    err(res, 400, "Content-Type must be application/json with body { sql, params?, timeout?, readonly? }.");
    return;
  }
  let obj: Record<string, unknown>;
  try {
    const v: unknown = body.length === 0 ? {} : JSON.parse(body.toString("utf8"));
    if (typeof v !== "object" || v === null || Array.isArray(v)) throw new Error("x");
    obj = v as Record<string, unknown>;
  } catch {
    err(res, 400, "Invalid JSON body.");
    return;
  }
  const sql = obj["sql"];
  if (typeof sql !== "string" || sql.trim() === "") {
    err(res, 400, "Missing or empty required field: sql.");
    return;
  }
  let params: unknown = null;
  if ("params" in obj && obj["params"] !== null) {
    const p = obj["params"];
    if (Array.isArray(p)) {
      const out: unknown[] = [];
      for (const v of p) {
        if (typeof v !== "string") {
          err(res, 400, "Array params must be tagged scalars (i:/f:/s:/b:/null).");
          return;
        }
        try {
          out.push(jdec(v));
        } catch {
          err(res, 400, "Array params must be tagged scalars (i:/f:/s:/b:/null).");
          return;
        }
      }
      params = out;
    } else if (typeof p === "object") {
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(p as Record<string, unknown>)) {
        if (typeof v !== "string") {
          err(res, 400, "Object params must be tagged scalars (i:/f:/s:/b:/null).");
          return;
        }
        try {
          out[k] = jdec(v);
        } catch {
          err(res, 400, "Object params must be tagged scalars (i:/f:/s:/b:/null).");
          return;
        }
      }
      params = out;
    } else {
      err(res, 400, "Field 'params' must be an array or an object.");
      return;
    }
  }
  let timeout = SQLITE_DEFAULT_TIMEOUT;
  if ("timeout" in obj && obj["timeout"] !== null) {
    const rawT = obj["timeout"];
    if (typeof rawT !== "string") {
      err(res, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
      return;
    }
    let t: unknown;
    try {
      t = jdec(rawT);
    } catch {
      err(res, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
      return;
    }
    if (typeof t !== "number") {
      err(res, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
      return;
    }
    if (!(t > 0 && t <= SQLITE_MAX_TIMEOUT)) {
      err(res, 400, "Field 'timeout' must be within (0, 300].");
      return;
    }
    timeout = t;
  }
  let readonly = false;
  if ("readonly" in obj && obj["readonly"] !== null) {
    const rawR = obj["readonly"];
    if (typeof rawR !== "string") {
      err(res, 400, "Field 'readonly' must be b:true or b:false.");
      return;
    }
    let r: unknown;
    try {
      r = jdec(rawR);
    } catch {
      err(res, 400, "Field 'readonly' must be b:true or b:false.");
      return;
    }
    if (typeof r !== "boolean") {
      err(res, 400, "Field 'readonly' must be b:true or b:false.");
      return;
    }
    readonly = r;
  }

  let scan: SqlScan;
  try {
    scan = scanSql(sql);
  } catch (e) {
    err(res, 400, (e as Error).message, abs); return;
  }
  if (scan.statements.length !== 1) {
    err(res, 400, "Send exactly one SQL statement per request.", abs); return;
  }
  const rowsMode = ROW_FIRST.has(scan.first) || scan.returning;
  // Native binding: node:sqlite takes ? positionally and :named via
  // object natively. Tag-decoded values above; bools become 1/0
  // (the driver rejects booleans).
  const bound = toBindParams(params);
  if (typeof bound === "string") {
    err(res, 400, bound, abs); return;
  }
  let payload: { [k: string]: Json };
  try {
    const { DatabaseSync } = require("node:sqlite") as typeof import("node:sqlite");
    const db = new DatabaseSync(abs, {
      readOnly: readonly,
      timeout: Math.round(timeout * 1000),
    });
    try {
      if (rowsMode) {
        const stmt = db.prepare(scan.statements[0]);
        const rows = (
          Array.isArray(bound)
            ? stmt.all(...bound)
            : Object.keys(bound).length === 0
              ? stmt.all()
              : stmt.all(bound)
        ) as Array<Record<string, unknown>>;
        const columns = rows.length > 0 ? Object.keys(rows[0]) : [];
        const out: string[][] = rows.map((row) =>
          columns.map((c) => cellToTag(row[c]))
        );
        payload = {
          columns, rows: out as unknown as Json,
          rowcount: jenc(out.length),
        };
      } else {
        const stmt = db.prepare(scan.statements[0]);
        const info = (
          Array.isArray(bound)
            ? stmt.run(...bound)
            : Object.keys(bound).length === 0
              ? stmt.run()
              : stmt.run(bound)
        ) as { changes: unknown; lastInsertRowid: unknown };
        const rc = DDL_FIRST.has(scan.first) ? -1 : Number(info.changes);
        payload = {
          rowcount: jenc(rc),
          lastrowid: jenc(Number(info.lastInsertRowid)),
        };
      }
    } finally {
      db.close();
    }
  } catch (e) {
    err(res, 400, `SQLite error: ${(e as Error).message}`, abs); return;
  }
  payload["path"] = raw;
  payload["absolute_path"] = abs;
  sendJson(res, 200, payload);
}

/**
 * Map tag-decoded params to node:sqlite bind values (bool -> 1/0;
 * the driver rejects booleans). Returns an error message string on
 * shape violation (params is validated as array/object by the caller,
 * values are already jdec-decoded).
 */
function toBindParams(params: unknown): (string | number | null)[] | Record<string, string | number | null> | string {
  if (params === null || params === undefined) return [];
  if (Array.isArray(params)) {
    return (params as unknown[]).map((v) =>
      typeof v === "boolean" ? (v ? 1 : 0) : (v as string | number | null));
  }
  if (typeof params === "object") {
    const out: Record<string, string | number | null> = {};
    for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
      out[k] = typeof v === "boolean" ? (v ? 1 : 0) : (v as string | number | null);
    }
    return out;
  }
  return "Field 'params' must be an array or an object.";
}

function cellToTag(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return jenc(v);
  if (typeof v === "number") return jenc(v);
  if (typeof v === "bigint") {
    const n = v <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(v) : v.toString();
    return typeof n === "number" ? jenc(n) : `i:${n}`;
  }
  if (typeof v === "string") return jtagStr(v);
  if (Buffer.isBuffer(v)) return jtagStr(v.toString("base64"));
  if (v instanceof Uint8Array) return jtagStr(Buffer.from(v).toString("base64"));
  return jtagStr(String(v));
}

// -- sysinfo -------------------------------------------------------------------

function readOsRelease(): Record<string, string> {
  const info: Record<string, string> = {};
  try {
    for (const line of fs.readFileSync("/etc/os-release", "utf8").split("\n")) {
      const t = line.trim();
      if (t === "" || t.startsWith("#") || !t.includes("=")) continue;
      const eq = t.indexOf("=");
      info[t.slice(0, eq)] = t.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
    }
  } catch { /* ignore */ }
  return info;
}

function handleSysinfo(res: http.ServerResponse): void {
  const platform = os.platform();
  const system = platform === "win32" ? "Windows" : platform === "darwin" ? "Darwin" : "Linux";
  let distro: Json = "null";
  let windows: Json = "null";
  let mac: Json = "null";
  if (system === "Linux") {
    const rel = readOsRelease();
    if (Object.keys(rel).length > 0) {
      distro = {
        id: rel["ID"] ?? "", name: rel["NAME"] ?? "",
        version: rel["VERSION"] ?? "", version_id: rel["VERSION_ID"] ?? "",
        pretty_name: rel["PRETTY_NAME"] ?? "",
      };
    }
  } else if (system === "Windows") {
    windows = {
      release: os.version(), version: os.release(),
      service_pack: "", edition: "",
    };
  } else if (system === "Darwin") {
    mac = { version: os.release() };
  }
  let hostname = "";
  try {
    hostname = os.hostname();
  } catch { /* ignore */ }
  let cpu = 1;
  if (system === "Linux") {
    try {
      const info = fs.readFileSync("/proc/cpuinfo", "utf8");
      cpu = Math.max(1, (info.match(/^processor\s*:/gm) ?? []).length);
    } catch { /* keep 1 */ }
  } else {
    cpu = Math.max(1, os.cpus().length);
  }
  sendJson(res, 200, {
    system, system_name: system === "Darwin" ? "macOS" : system,
    release: os.release(), version: os.version(),
    machine: os.arch(), processor: "",
    hostname, python_version: "null",
    node_version: process.version,
    cpu_count: jenc(cpu),
    distro, windows, mac,
  });
}

// -- socket proxy ---------------------------------------------------------------

const SOCK_DEFAULT_CONNECT_TIMEOUT = 5;
const SOCK_MAX_CONNECT_TIMEOUT = 30;
const SOCK_DEFAULT_RECV_TIMEOUT = 5;
const SOCK_MAX_RECV_TIMEOUT = 60;
const SOCK_DEFAULT_MAX_BYTES = 65536;
const SOCK_MAX_BYTES = 1048576;
const SOCK_MAX_SESSIONS = 128;
const SOCK_IDLE_TIMEOUT_MS = 300000;
const SOCK_PAUSE_BYTES = 4 * 1024 * 1024;

interface SockSession {
  sock: net.Socket;
  host: string;
  port: number;
  // "tcp" or "tls": TLS is a transport property of the session,
  // negotiated at open (implicit) or via starttls, and verified
  // against the trusted roots (+ optional ca_pem) with hostname
  // check and SNI. send/recv/close/WS see cleartext.
  kind: string;
  last: number;
  eof: boolean;
  ended: boolean;
  failed: string | null;
  buf: Buffer[];
  bufLen: number;
  waiter: (() => void) | null;
  paused: boolean;
  onData: ((chunk: Buffer) => void) | null;
  // wsAttached marks a live WebSocket bridge: the idle sweeper
  // skips it and HTTP send/recv are refused while set.
  wsAttached: boolean;
}

const SOCK_SESSIONS = new Map<string, SockSession>();
let sockSweeper: ReturnType<typeof setInterval> | null = null;

function sockSweep(): void {
  const now = Date.now();
  for (const [sid, sess] of SOCK_SESSIONS) {
    if (sess.wsAttached) continue;
    if (now - sess.last > SOCK_IDLE_TIMEOUT_MS) {
      SOCK_SESSIONS.delete(sid);
      try { sess.sock.destroy(); } catch { /* ignore */ }
    }
  }
}

function sockEnsureSweeper(): void {
  if (sockSweeper === null) {
    sockSweeper = setInterval(sockSweep, 60000);
    (sockSweeper as unknown as { unref?: () => void }).unref?.();
  }
}

function sockNotify(sess: SockSession): void {
  const w = sess.waiter;
  sess.waiter = null;
  if (w) w();
}

function sockLookup(
  res: http.ServerResponse, query: URLSearchParams
): { sid: string; sess: SockSession } | null {
  const sid = query.get("id");
  if (!sid) { err(res, 400, "Missing required query parameter: id"); return null; }
  const sess = SOCK_SESSIONS.get(sid);
  if (!sess) { err(res, 404, `Unknown socket session: ${sid}`); return null; }
  sess.last = Date.now();
  return { sid, sess };
}

function sockTake(sess: SockSession, maxBytes: number): Buffer | null {
  if (sess.bufLen === 0) return null;
  const parts: Buffer[] = [];
  let need = Math.min(maxBytes, sess.bufLen);
  while (need > 0 && sess.buf.length > 0) {
    const head = sess.buf[0];
    if (head.length <= need) {
      parts.push(head);
      sess.buf.shift();
      sess.bufLen -= head.length;
      need -= head.length;
    } else {
      parts.push(head.subarray(0, need));
      sess.buf[0] = head.subarray(need);
      sess.bufLen -= need;
      need = 0;
    }
  }
  if (sess.paused && sess.bufLen < SOCK_PAUSE_BYTES / 4) {
    sess.paused = false;
    try { sess.sock.resume(); } catch { /* ignore */ }
  }
  return Buffer.concat(parts);
}

function attachSession(sock: net.Socket, sess: SockSession): void {
  const onData = (chunk: Buffer): void => {
    sess.buf.push(chunk);
    sess.bufLen += chunk.length;
    sess.last = Date.now();
    if (!sess.paused && sess.bufLen > SOCK_PAUSE_BYTES) {
      sess.paused = true;
      try { sock.pause(); } catch { /* ignore */ }
    }
    sockNotify(sess);
  };
  sess.onData = onData;
  sock.on("data", onData);
  sock.on("end", () => { sess.ended = true; sockNotify(sess); });
  sock.on("close", () => { sess.ended = true; sockNotify(sess); });
  sock.on("error", (e: Error) => {
    if (!sess.failed) sess.failed = e.message;
    try { sock.destroy(); } catch { /* ignore */ }
    sockNotify(sess);
  });
}

function parseTlsIdentity(obj: Record<string, unknown>): { sni: string; caPem: string } | { error: string } {
  let sni = "";
  if ("sni" in obj && obj["sni"] !== null && obj["sni"] !== undefined) {
    if (typeof obj["sni"] !== "string" || (obj["sni"] as string).trim() === "") {
      return { error: "Field 'sni' must be a non-empty string." };
    }
    sni = (obj["sni"] as string).trim();
  }
  let caPem = "";
  if ("ca_pem" in obj && obj["ca_pem"] !== null && obj["ca_pem"] !== undefined) {
    if (typeof obj["ca_pem"] !== "string" || (obj["ca_pem"] as string).trim() === "") {
      return { error: "Field 'ca_pem' must be a PEM CA bundle string." };
    }
    caPem = obj["ca_pem"] as string;
  }
  return { sni, caPem };
}

function validateCaPem(caPem: string): string | null {
  try {
    new X509Certificate(caPem);
    return null;
  } catch {
    return "Field 'ca_pem' must be a valid PEM CA bundle.";
  }
}

function tlsHandshake(
  sock: net.Socket, sni: string, caPem: string, timeoutSec: number
): Promise<tls.TLSSocket> {
  return new Promise<tls.TLSSocket>((resolve, reject) => {
    let tlsSock: tls.TLSSocket;
    try {
      tlsSock = tls.connect({
        socket: sock,
        host: sni,
        servername: sni,
        ca: caPem ? [...tls.rootCertificates, caPem] : undefined,
        rejectUnauthorized: true,
        timeout: timeoutSec * 1000,
      });
    } catch (e) {
      reject(e);
      return;
    }
    const timer = setTimeout(() => {
      try { tlsSock.destroy(); } catch { /* ignore */ }
      reject(new Error("TLS handshake timed out"));
    }, timeoutSec * 1000);
    const onError = (e: Error): void => {
      // No destroy here: on a fast failure (e.g. plaintext peer) the
      // underlying socket stays usable and the session survives, like
      // the reference. Callers destroy on open-failure paths.
      clearTimeout(timer);
      reject(e);
    };
    tlsSock.once("secureConnect", () => {
      clearTimeout(timer);
      tlsSock.removeListener("error", onError);
      resolve(tlsSock);
    });
    tlsSock.once("error", onError);
  });
}

async function handleSocketOpen(
  req: http.IncomingMessage, res: http.ServerResponse
): Promise<void> {
  sockEnsureSweeper();
  const body = await readBody(req);
  const ct = (req.headers["content-type"] as string | undefined) ?? "";
  if (!ct.includes("application/json")) {
    err(res, 400, "Content-Type must be application/json with body { host, port, timeout? }.");
    return;
  }
  let obj: Record<string, unknown>;
  try {
    const v: unknown = body.length === 0 ? {} : JSON.parse(body.toString("utf8"));
    if (typeof v !== "object" || v === null || Array.isArray(v)) throw new Error("x");
    obj = v as Record<string, unknown>;
  } catch {
    err(res, 400, "Invalid JSON body.");
    return;
  }
  const host = obj["host"];
  if (typeof host !== "string" || host.trim() === "") {
    err(res, 400, "Missing or empty required field: host.");
    return;
  }
  if (!("port" in obj) || obj["port"] === null) {
    err(res, 400, "Missing required field: port (i:<1-65535>).");
    return;
  }
  let portVal: unknown;
  try {
    if (typeof obj["port"] !== "string" || !(obj["port"] as string).startsWith("i:")) {
      throw new Error("not an int tag");
    }
    portVal = jdec(obj["port"]);
  } catch {
    err(res, 400, "Field 'port' must be an integer 1-65535 (i:<int>).");
    return;
  }
  if (typeof portVal !== "number" || !Number.isInteger(portVal) || portVal < 1 || portVal > 65535) {
    err(res, 400, "Field 'port' must be an integer 1-65535 (i:<int>).");
    return;
  }
  let timeout = SOCK_DEFAULT_CONNECT_TIMEOUT;
  if ("timeout" in obj && obj["timeout"] !== null) {
    let t: unknown;
    try {
      t = jdec(obj["timeout"]);
    } catch {
      err(res, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
      return;
    }
    if (typeof t !== "number") {
      err(res, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
      return;
    }
    timeout = t;
  }
  if (!(timeout > 0 && timeout <= SOCK_MAX_CONNECT_TIMEOUT)) {
    err(res, 400, `Field 'timeout' must be within (0, ${SOCK_MAX_CONNECT_TIMEOUT}].`);
    return;
  }
  let tlsMode = false;
  if ("tls" in obj && obj["tls"] !== null && obj["tls"] !== undefined) {
    if (typeof obj["tls"] !== "string") {
      err(res, 400, "Field 'tls' must be b:true or b:false.");
      return;
    }
    let t: unknown;
    try {
      t = jdec(obj["tls"]);
    } catch {
      err(res, 400, "Field 'tls' must be b:true or b:false.");
      return;
    }
    if (typeof t !== "boolean") {
      err(res, 400, "Field 'tls' must be b:true or b:false.");
      return;
    }
    tlsMode = t;
  }
  const ident = parseTlsIdentity(obj);
  if ("error" in ident) { err(res, 400, ident.error); return; }
  const sni = ident.sni || (host as string);
  if (ident.caPem) {
    const caErr = validateCaPem(ident.caPem);
    if (caErr) { err(res, 400, caErr); return; }
  }
  sockSweep();
  if (SOCK_SESSIONS.size >= SOCK_MAX_SESSIONS) {
    err(res, 400, `Too many open socket sessions (${SOCK_MAX_SESSIONS}).`);
    return;
  }
  const sock = new net.Socket();
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        sock.destroy();
        reject(new Error("connect timed out"));
      }, timeout * 1000);
      const onConnect = (): void => {
        clearTimeout(timer);
        sock.removeListener("error", onError);
        resolve();
      };
      const onError = (e: Error): void => {
        clearTimeout(timer);
        reject(e);
      };
      sock.once("connect", onConnect);
      sock.once("error", onError);
      sock.connect(portVal as number, host as string);
    });
  } catch (e) {
    err(res, 400, `Cannot connect to ${host}:${portVal}: ${(e as Error).message}`);
    return;
  }
  let peer: net.Socket = sock;
  let kind = "tcp";
  if (tlsMode) {
    try {
      peer = await tlsHandshake(sock, sni, ident.caPem, timeout);
    } catch (e) {
      try { sock.destroy(); } catch { /* ignore */ }
      err(res, 400, `TLS handshake with ${host}:${portVal} failed: ${(e as Error).message}`);
      return;
    }
    kind = "tls";
  }
  const sid = randomBytes(16).toString("base64url");
  const sess: SockSession = {
    sock: peer, host: host as string, port: portVal as number, kind,
    last: Date.now(), eof: false, ended: false, failed: null,
    buf: [], bufLen: 0, waiter: null, paused: false, onData: null, wsAttached: false,
  };
  attachSession(peer, sess);
  SOCK_SESSIONS.set(sid, sess);
  sendJson(res, 201, { id: sid, kind, host, port: jenc(portVal as number) });
}

function parseSendBody(body: Buffer, ct: string): { data: Buffer } | { error: string } {
  if (ct.includes("application/json")) {
    let obj: Record<string, unknown>;
    try {
      const v: unknown = body.length === 0 ? {} : JSON.parse(body.toString("utf8"));
      if (typeof v !== "object" || v === null || Array.isArray(v)) throw new Error("x");
      obj = v as Record<string, unknown>;
    } catch {
      return { error: "Invalid JSON body." };
    }
    let content = "";
    if ("content" in obj && obj["content"] !== null) {
      if (typeof obj["content"] !== "string") return { error: "'content' must be a string." };
      content = obj["content"] as string;
    }
    let encoding = "text";
    if ("encoding" in obj && obj["encoding"] !== null) {
      if (typeof obj["encoding"] !== "string") return { error: "Invalid encoding: (use text|base64)." };
      encoding = (obj["encoding"] as string).toLowerCase();
    }
    if (encoding === "text") return { data: Buffer.from(content, "utf8") };
    if (encoding === "base64") {
      if (!/^[A-Za-z0-9+/]*={0,2}$/.test(content) || content.length % 4 !== 0) {
        return { error: "Invalid base64 'content'." };
      }
      return { data: Buffer.from(content, "base64") };
    }
    return { error: `Invalid encoding: '${encoding}' (use text|base64).` };
  }
  return { data: body };
}

async function handleSocketSend(
  req: http.IncomingMessage, res: http.ServerResponse, query: URLSearchParams
): Promise<void> {
  const found = sockLookup(res, query);
  if (!found) return;
  const { sid, sess } = found;
  if (sess.wsAttached) {
    err(res, 400, "Socket session is attached to a websocket (HTTP send refused while attached).");
    return;
  }
  const body = await readBody(req);
  const ct = (req.headers["content-type"] as string | undefined) ?? "";
  const parsed = parseSendBody(body, ct);
  if ("error" in parsed) { err(res, 400, parsed.error); return; }
  if (sess.failed) { err(res, 400, `Socket error: ${sess.failed}`); return; }
  try {
    await new Promise<void>((resolve, reject) => {
      sess.sock.write(parsed.data, (e) => (e ? reject(e) : resolve()));
    });
  } catch (e) {
    err(res, 400, `Socket error: ${(e as Error).message}`);
    return;
  }
  sess.last = Date.now();
  sendJson(res, 200, { id: sid, bytes: jenc(parsed.data.length) });
}

function parsePlainTimeout(text: string | null, dflt: number, max: number): number | null {
  if (text === null || text === "") return dflt;
  const v = Number(text);
  if (!Number.isFinite(v) || !(v > 0 && v <= max)) return null;
  return v;
}

async function handleSocketRecv(
  res: http.ServerResponse, query: URLSearchParams
): Promise<void> {
  const timeout = parsePlainTimeout(query.get("timeout"), SOCK_DEFAULT_RECV_TIMEOUT, SOCK_MAX_RECV_TIMEOUT);
  if (timeout === null) {
    err(res, 400, `Invalid timeout param (seconds within (0, ${SOCK_MAX_RECV_TIMEOUT}]).`);
    return;
  }
  let maxBytes = SOCK_DEFAULT_MAX_BYTES;
  const maxRaw = query.get("max_bytes");
  if (maxRaw !== null && maxRaw !== "") {
    if (!/^\d+$/.test(maxRaw)) { err(res, 400, `Invalid max_bytes param (1-${SOCK_MAX_BYTES}).`); return; }
    maxBytes = parseInt(maxRaw, 10);
    if (maxBytes < 1 || maxBytes > SOCK_MAX_BYTES) {
      err(res, 400, `Invalid max_bytes param (1-${SOCK_MAX_BYTES}).`);
      return;
    }
  }
  const found = sockLookup(res, query);
  if (!found) return;
  const { sid, sess } = found;
  if (sess.wsAttached) {
    err(res, 400, "Socket session is attached to a websocket (HTTP recv refused while attached).");
    return;
  }
  if (sess.failed) { err(res, 400, `Socket error: ${sess.failed}`); return; }
  if (sess.eof) {
    sendJson(res, 200, {
      id: sid, encoding: "base64", content: "",
      bytes: jenc(0), eof: jenc(true), timed_out: jenc(false),
    });
    return;
  }
  if (sess.waiter) { err(res, 400, "Another recv is already pending on this session."); return; }
  const deadline = Date.now() + timeout * 1000;
  while (true) {
    if (sess.failed) { err(res, 400, `Socket error: ${sess.failed}`); return; }
    const chunk = sockTake(sess, maxBytes);
    if (chunk) {
      sess.last = Date.now();
      sendJson(res, 200, {
        id: sid, encoding: "base64", content: chunk.toString("base64"),
        bytes: jenc(chunk.length), eof: jenc(false), timed_out: jenc(false),
      });
      return;
    }
    if (sess.ended) {
      sess.eof = true;
      sess.last = Date.now();
      sendJson(res, 200, {
        id: sid, encoding: "base64", content: "",
        bytes: jenc(0), eof: jenc(true), timed_out: jenc(false),
      });
      return;
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      sess.last = Date.now();
      sendJson(res, 200, {
        id: sid, encoding: "base64", content: "",
        bytes: jenc(0), eof: jenc(false), timed_out: jenc(true),
      });
      return;
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => { sess.waiter = null; resolve(); }, remaining);
      sess.waiter = () => { clearTimeout(timer); resolve(); };
    });
    sess.waiter = null;
  }
}

function handleSocketClose(res: http.ServerResponse, query: URLSearchParams): void {
  const found = sockLookup(res, query);
  if (!found) return;
  const { sid, sess } = found;
  SOCK_SESSIONS.delete(sid);
  try { sess.sock.destroy(); } catch { /* ignore */ }
  sendJson(res, 200, { id: sid, closed: jenc(true) });
}

async function handleSocketStarttls(
  req: http.IncomingMessage, res: http.ServerResponse, query: URLSearchParams
): Promise<void> {
  const found = sockLookup(res, query);
  if (!found) return;
  const { sid, sess } = found;
  if (sess.wsAttached) {
    err(res, 400, "Socket session is attached to a websocket (STARTTLS refused while attached).");
    return;
  }
  if (sess.kind === "tls") {
    err(res, 400, "Socket session is already TLS.");
    return;
  }
  if (sess.waiter) {
    err(res, 400, "Another recv is already pending on this session.");
    return;
  }
  const body = await readBody(req);
  let obj: Record<string, unknown> = {};
  if (body.length > 0) {
    const ct = (req.headers["content-type"] as string | undefined) ?? "";
    if (!ct.includes("application/json")) {
      err(res, 400, "Content-Type must be application/json with body { sni?, ca_pem? }.");
      return;
    }
    try {
      const v: unknown = JSON.parse(body.toString("utf8"));
      if (typeof v !== "object" || v === null || Array.isArray(v)) throw new Error("x");
      obj = v as Record<string, unknown>;
    } catch {
      err(res, 400, "Invalid JSON body.");
      return;
    }
  }
  const ident = parseTlsIdentity(obj);
  if ("error" in ident) { err(res, 400, ident.error); return; }
  const sni = ident.sni || sess.host;
  if (ident.caPem) {
    const caErr = validateCaPem(ident.caPem);
    if (caErr) { err(res, 400, caErr); return; }
  }
  // Detach the plaintext data listener so handshake bytes are not
  // buffered as application data; the socket buffer itself keeps them
  // for the TLS layer. Unpause: a paused socket would starve it.
  if (sess.onData) sess.sock.removeListener("data", sess.onData);
  try { sess.sock.resume(); } catch { /* ignore */ }
  sess.paused = false;
  let tlsSock: tls.TLSSocket;
  try {
    tlsSock = await tlsHandshake(sess.sock, sni, ident.caPem, SOCK_MAX_CONNECT_TIMEOUT);
  } catch (e) {
    // Failed handshake leaves the plaintext session untouched.
    if (sess.onData) sess.sock.on("data", sess.onData);
    err(res, 400, `TLS handshake with ${sess.host}:${sess.port} failed: ${(e as Error).message}`);
    return;
  }
  sess.sock = tlsSock;
  sess.kind = "tls";
  sess.buf = [];
  sess.bufLen = 0;
  sess.last = Date.now();
  attachSession(tlsSock, sess);
  sendJson(res, 200, { id: sid, kind: "tls" });
}

function wsCloseSession(sid: string, sess: SockSession): void {
  if (SOCK_SESSIONS.get(sid) === sess) SOCK_SESSIONS.delete(sid);
  try { sess.sock.destroy(); } catch { /* ignore */ }
}

// -- websocket transport -----------------------------------------------------

const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const WS_MAX_MESSAGE = 4 * 1024 * 1024;

const WS_CONT = 0x0, WS_TEXT = 0x1, WS_BIN = 0x2;
const WS_CLOSE = 0x8, WS_PING = 0x9, WS_PONG = 0xA;

function wsAcceptKey(key: string): string {
  return createHash("sha1").update(key + WS_GUID).digest("base64");
}

function wsHasToken(headerVal: string | undefined, token: string): boolean {
  if (!headerVal) return false;
  return headerVal.split(",").some((p) => p.trim().toLowerCase() === token);
}

interface WsBridge { sock: net.Socket; dead: boolean; closed: boolean }

function wsSendFrame(br: WsBridge, opcode: number, payload: Buffer): boolean {
  if (br.dead || br.closed) return false;
  let hdr: Buffer;
  if (payload.length < 126) {
    hdr = Buffer.from([0x80 | opcode, payload.length]);
  } else if (payload.length < 65536) {
    hdr = Buffer.alloc(4);
    hdr[0] = 0x80 | opcode; hdr[1] = 126;
    hdr.writeUInt16BE(payload.length, 2);
  } else {
    hdr = Buffer.alloc(10);
    hdr[0] = 0x80 | opcode; hdr[1] = 127;
    hdr.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  try {
    br.sock.write(Buffer.concat([hdr, payload]));
    return true;
  } catch {
    return false;
  }
}

function wsSendClose(br: WsBridge, code: number, reason: Buffer): void {
  if (br.dead || br.closed) return;
  br.closed = true;
  const payload = Buffer.alloc(2 + reason.length);
  payload.writeUInt16BE(code, 0);
  reason.copy(payload, 2);
  try {
    br.sock.write(Buffer.concat([Buffer.from([0x88, payload.length]), payload]));
  } catch { /* ignore */ }
}

interface WsParsed { fin: boolean; opcode: number; payload: Buffer; isCtl: boolean }

function wsTryParse(buf: Buffer): { frame: WsParsed; rest: Buffer } | { error: number } | null {
  if (buf.length < 2) return null;
  if (buf[0] & 0x70) return { error: 1002 };
  const fin = !!(buf[0] & 0x80);
  const opcode = buf[0] & 0x0F;
  if (!(buf[1] & 0x80)) return { error: 1002 }; // clients MUST mask
  let len = buf[1] & 0x7F;
  let off = 2;
  if (len === 126) {
    if (buf.length < 4) return null;
    len = buf.readUInt16BE(2);
    off = 4;
  } else if (len === 127) {
    if (buf.length < 10) return null;
    if (buf.readUInt32BE(2) !== 0) return { error: 1009 };
    len = buf.readUInt32BE(6);
    off = 10;
  }
  const isCtl = opcode >= 0x8;
  if (isCtl && (!fin || len > 125)) return { error: 1002 };
  if (len > WS_MAX_MESSAGE) return { error: 1009 };
  if (buf.length < off + 4 + len) return null;
  const key = buf.subarray(off, off + 4);
  const payload = Buffer.from(buf.subarray(off + 4, off + 4 + len));
  for (let i = 0; i < payload.length; i++) payload[i] ^= key[i % 4];
  return { frame: { fin, opcode, payload, isCtl }, rest: buf.subarray(off + 4 + len) };
}

function wsReason(status: number): string {
  if (status === 401) return "Unauthorized";
  if (status === 403) return "Forbidden";
  if (status === 404) return "Not Found";
  return "Bad Request";
}

function wsFailLabel(status: number): string {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  return "bad_request";
}

function checkAuthUpgrade(
  req: http.IncomingMessage, query: URLSearchParams, opts: Opts
): boolean {
  if (!opts.authRequired) return true;
  let supplied = "";
  const h = (req.headers["authorization"] as string | undefined) ?? "";
  if (h.startsWith("Bearer ")) supplied = h.slice(7).trim();
  if (supplied === "") supplied = query.get("token") ?? "";
  const a = Buffer.from(supplied, "utf8");
  const b = Buffer.from(opts.authToken, "utf8");
  return opts.authToken !== "" && a.length === b.length && timingSafeEqual(a, b);
}

async function handleSocketWsUpgrade(
  req: http.IncomingMessage, socket: net.Socket, head: Buffer, opts: Opts
): Promise<void> {
  const url = new URL(req.url ?? "/", "http://x");
  const query = url.searchParams;
  const fail = (status: number, message: string): void => {
    const body = Buffer.from(JSON.stringify(
      { error: wsFailLabel(status), message }), "utf8");
    const extra = status === 401 ? "WWW-Authenticate: Bearer\r\n" : "";
    try {
      socket.write(
        `HTTP/1.1 ${status} ${wsReason(status)}\r\n` +
        "Content-Type: application/json; charset=utf-8\r\n" +
        `Content-Length: ${body.length}\r\n` +
        "Connection: close\r\nAccess-Control-Allow-Origin: *\r\n" +
        `${extra}\r\n`);
      socket.write(body);
    } catch { /* ignore */ }
    socket.destroy();
  };
  if (url.pathname !== "/api/socket/ws") {
    fail(404, `Unknown route: ${url.pathname}`);
    return;
  }
  if (!hostAllowed(req.headers["host"] as string | undefined)) {
    fail(403, HOST_DENIED_MSG);
    return;
  }
  if (!checkAuthUpgrade(req, query, opts)) {
    fail(401, "Valid API token required: send Authorization: Bearer <token> or ?token=.");
    return;
  }
  if (!wsHasToken(req.headers["connection"] as string | undefined, "upgrade") ||
      ((req.headers["upgrade"] as string | undefined) ?? "").trim().toLowerCase() !== "websocket") {
    fail(400, "This route requires a WebSocket upgrade (Upgrade: websocket).");
    return;
  }
  if ((((req.headers["sec-websocket-version"] as string | undefined) ?? "").trim()) !== "13") {
    fail(400, "Unsupported WebSocket version (need 13).");
    return;
  }
  const key = ((req.headers["sec-websocket-key"] as string | undefined) ?? "").trim();
  if (key === "") {
    fail(400, "Missing Sec-WebSocket-Key.");
    return;
  }
  const sid = query.get("id");
  if (!sid) {
    fail(400, "Missing required query parameter: id");
    return;
  }
  const sess = SOCK_SESSIONS.get(sid);
  if (!sess) {
    fail(404, `Unknown socket session: ${sid}`);
    return;
  }
  sess.last = Date.now();
  if (sess.wsAttached) {
    fail(400, "Socket session is already attached to a websocket.");
    return;
  }
  if (sess.waiter) {
    fail(400, "Another recv is already pending on this session.");
    return;
  }
  sess.wsAttached = true;
  try {
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\n" +
      "Upgrade: websocket\r\nConnection: Upgrade\r\n" +
      `Sec-WebSocket-Accept: ${wsAcceptKey(key)}\r\n\r\n`);
  } catch {
    sess.wsAttached = false;
    socket.destroy();
    return;
  }
  wsBridge(socket, head, sid, sess);
}

function wsBridge(sock: net.Socket, head: Buffer, sid: string, sess: SockSession): void {
  const br: WsBridge = { sock, dead: false, closed: false };
  const endWs = (): void => {
    if (br.dead) return;
    br.dead = true;
    try { sock.end(); } catch { /* ignore */ }
    setTimeout(() => { try { sock.destroy(); } catch { /* ignore */ } }, 2000).unref?.();
  };
  const endBoth = (): void => {
    // Close-wins: ending the bridge ends the session (idempotent).
    // If nobody sent a close frame yet (e.g. HTTP close raced the
    // pump), report 1011 so the peer never hangs on a bare FIN.
    if (!br.closed) wsSendClose(br, 1011, Buffer.alloc(0));
    wsCloseSession(sid, sess);
    endWs();
  };
  const fail = (code: number): void => {
    wsSendClose(br, code, Buffer.alloc(0));
    endBoth();
  };

  // --- TCP -> WS pump (reuses the event buffer + waiter slot) ---
  if (sess.eof) {
    wsSendClose(br, 1000, Buffer.alloc(0));
    endBoth();
  } else {
    (async () => {
      while (SOCK_SESSIONS.get(sid) === sess && sess.wsAttached) {
        if (sess.failed) {
          wsSendClose(br, 1011, Buffer.alloc(0));
          break;
        }
        const chunk = sockTake(sess, 32768);
        if (chunk) {
          sess.last = Date.now();
          if (!wsSendFrame(br, WS_BIN, chunk)) break;
          continue;
        }
        if (sess.ended && sess.bufLen === 0) {
          sess.eof = true;
          sess.last = Date.now();
          wsSendClose(br, 1000, Buffer.alloc(0));
          break;
        }
        await new Promise<void>((resolve) => { sess.waiter = resolve; });
        sess.waiter = null;
      }
      endBoth();
    })();
  }

  // --- WS -> TCP reader (incremental parser over data events) ---
  let pending: Buffer = head;
  let rOp: number | null = null;
  let rParts: Buffer[] = [];
  let rLen = 0;
  const resetMsg = (): void => { rOp = null; rParts = []; rLen = 0; };
  sock.on("data", (c: Buffer) => {
    if (br.dead) return;
    pending = Buffer.concat([pending, c]);
    while (true) {
      const parsed = wsTryParse(pending);
      if (parsed === null) break;
      if ("error" in parsed) { fail(parsed.error); return; }
      pending = parsed.rest;
      const { fin, opcode, payload, isCtl } = parsed.frame;
      if (isCtl) {
        if (opcode === WS_CLOSE) {
          if (payload.length === 1) { fail(1002); return; }
          let code = 1000;
          let reason = Buffer.alloc(0);
          if (payload.length >= 2) {
            code = payload.readUInt16BE(0);
            reason = Buffer.from(payload.subarray(2));
          }
          // Delete BEFORE echo: the echo implies "gone".
          wsCloseSession(sid, sess);
          wsSendClose(br, code, reason);
          endWs();
          return;
        }
        if (opcode === WS_PING) { wsSendFrame(br, WS_PONG, payload); continue; }
        if (opcode === WS_PONG) continue;
        fail(1002);
        return;
      }
      if (opcode === WS_CONT) {
        if (rOp === null) { fail(1002); return; }
      } else if (opcode === WS_TEXT || opcode === WS_BIN) {
        if (rOp !== null) { fail(1002); return; }
        rOp = opcode;
      } else {
        fail(1003);
        return;
      }
      rParts.push(payload);
      rLen += payload.length;
      if (rLen > WS_MAX_MESSAGE) { fail(1009); return; }
      if (!fin) continue;
      if (rOp === WS_TEXT) {
        try {
          new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(Buffer.concat(rParts)));
        } catch {
          fail(1007);
          return;
        }
      }
      const msg = Buffer.concat(rParts);
      resetMsg();
      if (SOCK_SESSIONS.get(sid) !== sess) { endWs(); return; }
      sess.sock.write(msg, (e) => {
        if (e) { endBoth(); return; }
        sess.last = Date.now();
      });
    }
  });
  sock.on("error", () => { endBoth(); });
  sock.on("close", () => { endBoth(); });
}

// -- route catalog ---------------------------------------------------------------

interface Param { [k: string]: Json }
interface Route { [k: string]: Json }

function param(
  name: string, location: string, required: boolean, type: string,
  desc: string, def?: string, allowed?: string[]
): Param {
  const p: Param = { name, location, required: jenc(required), type, description: desc };
  if (def !== undefined) p["default"] = def;
  if (allowed !== undefined) p["allowed"] = allowed;
  return p;
}

function route(
  method: string, url: string, name: string, desc: string,
  params: Param[], response: string, errors?: string
): Route {
  const r: Route = { method, url, name, description: desc, params, response };
  if (errors !== undefined) r["errors"] = errors;
  return r;
}

export const ROUTES: Route[] = [
  route("GET", "/api/help", "help",
    "Return the full list of implemented routes with URL, name, description and parameter formats. Wire convention: JSON carries only string/array/object - numbers, booleans and nulls travel as tagged strings (i:<int>, f:<float>, b:true|false, null). Fixed vocabulary and free text stay raw; sqlite row cells and params values are fully tagged (strings take an s: prefix there).",
    [], "JSON object { version, count, routes: [...] }."),
  route("GET", "/api/dir?path=<path>", "read_directory",
    "List the contents of a directory.",
    [param("path", "query", true, "string",
      "Absolute or relative path of the directory. Relative paths resolve against the server working directory. Paths starting with ~/ expand to the server user's home directory.")],
    "JSON { path, absolute_path, count, entries: [{ name, path, type, size, atime, mtime, ctime, crtime, mode }] }. type is one of file|dir|symlink|other. Times are epoch seconds: atime (last access), mtime (last modification), ctime (last status change; creation on Windows), crtime (creation/birth, null where unavailable).",
    "400 missing path / not-a-directory, 404 not found, 403 forbidden."),
  route("GET", "/api/file?path=<path>[&encoding=text|base64|auto][&raw=1]", "read_file",
    "Read a file. Supports text and binary files. Default JSON mode auto-detects: UTF-8-decodable content is returned as text, otherwise as base64. With raw=1 the raw bytes are returned with a guessed Content-Type.",
    [param("path", "query", true, "string",
      "Absolute or relative path of the file. Relative paths resolve against the server working directory; ~/ expands to the user's home directory."),
    param("encoding", "query", false, "string",
      "auto: try UTF-8 text, fall back to base64. text: force UTF-8 decode with replacement of invalid sequences. base64: force base64.",
      "auto", ["auto", "text", "base64"]),
    param("raw", "query", false, "string",
      "If '1', respond with raw file bytes and a guessed Content-Type instead of JSON.",
      "0", ["0", "1"])],
    "JSON mode: { path, absolute_path, size, mtime, mime_type, is_binary, encoding, content }. Raw mode: file bytes with Content-Type + Content-Length headers.",
    "400 missing path / is-a-directory, 404 not found, 403 forbidden."),
  route("POST", "/api/file?path=<path>", "create_file",
    "Create a file, empty when no body is sent or with initial data (text or binary). Fails if the path already exists (use PUT to overwrite).",
    [param("path", "query", true, "string",
      "Absolute or relative path of the file to create. ~/ expands to the server user's home directory."),
    param("content", "body (JSON)", false, "string",
      "Initial content. Plain string for encoding=text, base64 string for encoding=base64. Omitted means empty file. Alternatively send raw bytes with a non-JSON Content-Type."),
    param("encoding", "body (JSON)", false, "string",
      "How to interpret the JSON body content.", "text", ["text", "base64"])],
    "201 JSON { path, absolute_path, size, mime_type }.",
    "400 missing path / bad body, 404 parent directory missing, 409 already exists, 403 forbidden."),
  route("PUT", "/api/file?path=<path>", "overwrite_file",
    "Overwrite the content of an existing file (text or binary). Same body rules as create_file.",
    [param("path", "query", true, "string",
      "Absolute or relative path of the file to overwrite. ~/ expands to the server user's home directory."),
    param("content", "body (JSON)", false, "string",
      "New content (see create_file). Omitted truncates the file to zero bytes."),
    param("encoding", "body (JSON)", false, "string",
      "How to interpret the JSON body content.", "text", ["text", "base64"])],
    "200 JSON { path, absolute_path, size, mime_type }.",
    "400 missing path / is-a-directory / bad body, 404 not found, 403 forbidden."),
  route("DELETE", "/api/file?path=<path>", "delete_file",
    "Delete a file.",
    [param("path", "query", true, "string",
      "Absolute or relative path of the file to delete. ~/ expands to the server user's home directory.")],
    "200 JSON { path, absolute_path, deleted: true }.",
    "400 missing path / is-a-directory, 404 not found, 403 forbidden."),
  route("DELETE", "/api/dir?path=<path>", "delete_directory",
    "Delete an empty directory (rmdir equivalent). Fails when the directory is not empty.",
    [param("path", "query", true, "string",
      "Absolute or relative path of the empty directory to delete. ~/ expands to the server user's home directory.")],
    "200 JSON { path, absolute_path, deleted: true }.",
    "400 missing path / not-a-directory / not-empty, 404 not found, 403 forbidden."),
  route("POST", "/api/shell", "exec_shell",
    "Execute a bash command string (pipes, redirections and all bash syntax allowed) and return the exit code plus separately captured stdout and stderr. WARNING: this is arbitrary remote code execution on the host; the server binds localhost by default and relies on that for protection.",
    [param("command", "body (JSON)", true, "string",
      'Exact string as typed in bash, e.g. "cat < a.txt | sort > b.txt".'),
    param("timeout", "body (JSON)", false, "number",
      "Seconds before the process group is killed (max 300). Timed-out calls return 124 with timed_out=true plus partial output.",
      "i:30"),
    param("cwd", "body (JSON)", false, "string",
      "Working directory for the command (default: server working directory)."),
    param("max_stdout", "body (JSON)", false, "integer",
      "Max captured stdout bytes sent back (1 MB default, 100 MB hard cap). The command runs fully; only the response is truncated.",
      "i:1048576"),
    param("max_stderr", "body (JSON)", false, "integer",
      "Max captured stderr bytes sent back (same rules as max_stdout).",
      "i:1048576"),
    param("stdin", "body (JSON)", false, "string",
      "Input piped to the command's stdin (as if from a previous pipe stage). Plain string for stdin_encoding=text, base64 for stdin_encoding=base64. Omitted means empty stdin (EOF at once)."),
    param("stdin_encoding", "body (JSON)", false, "string",
      "How to interpret the stdin field.",
      "text", ["text", "base64"])],
    "200 JSON { command, cwd, returncode, stdout, stderr, timed_out, stdout_truncated, stderr_truncated, max_stdout, max_stderr, stdin_bytes }. returncode is the exit status of the last pipeline element (plain bash semantics). stdout/stderr are what the shell process emitted on its own fds, so in-command redirections apply: '> f' leaves captured stdout empty, '2>&1' merges into captured stdout. stdin_bytes is how many input bytes were fed.",
    "400 missing/empty command / bad JSON / bad timeout / bad cwd / bad or over-cap output limits, 405 wrong method."),
  route("POST", "/api/sqlite/create?path=<path>", "sqlite_create",
    "Create a new SQLite database file. The file is permanent; only the per-request connection is closed afterwards.",
    [param("path", "query", true, "string", "Full path of the database file to create.")],
    "201 JSON { path, absolute_path, size, sqlite_version }.",
    "400 missing path, 404 parent directory missing, 409 file already exists, 403 forbidden."),
  route("POST", "/api/sqlite/query?path=<db-path>", "sqlite_query",
    "Execute one arbitrary SQL statement on a SQLite database. Stateless: a fresh connection is opened per request and closed right after. SELECT-style statements return rows; anything else is committed and returns the change counters.",
    [param("path", "query", true, "string", "Path of the database file."),
    param("sql", "body (JSON)", true, "string",
      "Exactly one SQL statement. Multi-statement strings are rejected (send one per request)."),
    param("params", "body (JSON)", false, "array | object",
      "Bound parameters: array for ? placeholders, object for :named placeholders. Values must be tagged scalars (i:/f:/s:/b:/null); BLOB input is not supported.",
      "[]"),
    param("timeout", "body (JSON)", false, "number",
      "Busy timeout in seconds while waiting on a locked database (max 300).", "i:5"),
    param("readonly", "body (JSON)", false, "boolean",
      "Open the database read-only.", "b:false")],
    "200 JSON, SELECT-style: { path, absolute_path, columns, rows, rowcount } with BLOB values as base64 strings. Writes: { path, absolute_path, rowcount, lastrowid }.",
    "400 missing path/sql, bad JSON/params/timeout, multi-statement, or the SQLite error text itself (e.g. no such table). 404 database file not found, 403 forbidden, 405 wrong method."),
  route("GET", "/api/sysinfo", "system_info",
    "Return operating-system information about the host running the server. Takes no parameters.",
    [],
    "200 JSON { system, system_name, release, version, machine, processor, hostname, python_version, cpu_count, distro, windows, mac }. system is the raw platform name (Linux/Windows/Darwin); system_name is the friendly one (Darwin -> macOS). distro is parsed from /etc/os-release on Linux and null elsewhere; windows/mac objects appear only on those systems.",
    "401 missing/invalid token, 405 wrong method."),
  route("POST", "/api/socket/open", "socket_open",
    "Open a TCP connection to a network peer and hold it as a session, so browser pages (which cannot open raw sockets) can speak arbitrary TCP protocols through the local service (e.g. SMTP/IMAP for a browser email client). With tls:b:true the handshake runs immediately (implicit TLS, e.g. IMAPS on 993), verified against the system roots (+ optional ca_pem) with hostname check and SNI. Returns an opaque session id for send/recv/close/starttls. WARNING: this is an open network proxy on the host; the server binds localhost by default and relies on that for protection.",
    [param("host", "body (JSON)", true, "string",
      "Peer hostname or IP address to connect to."),
    param("port", "body (JSON)", true, "integer",
      "Peer TCP port, 1-65535 (i:<int>)."),
    param("timeout", "body (JSON)", false, "number",
      "Connect timeout in seconds (max 30).", "i:5"),
    param("tls", "body (JSON)", false, "boolean",
      "Handshake TLS immediately and verify the peer (implicit TLS).", "b:false"),
    param("sni", "body (JSON)", false, "string",
      "Server name for SNI and certificate verification (default: host)."),
    param("ca_pem", "body (JSON)", false, "string",
      "Extra PEM CA bundle appended to the system roots (corporate intercepts, test PKIs).")],
    "201 JSON { id, kind (tcp|tls), host, port }. The id is an opaque session token for the other socket routes.",
    "400 missing/invalid host/port/timeout/tls/sni/ca_pem, bad JSON, connect or TLS-handshake failure (engine error text), or too many open sessions. 405 wrong method."),
  route("POST", "/api/socket/send?id=<id>", "socket_send",
    "Write bytes to an open socket session (transparent: the peer receives exactly these bytes).",
    [param("id", "query", true, "string",
      "Session id from socket_open."),
    param("content", "body (JSON)", false, "string",
      "Bytes to write. Plain string for encoding=text, base64 string for encoding=base64. Alternatively send raw bytes with a non-JSON Content-Type."),
    param("encoding", "body (JSON)", false, "string",
      "How to interpret the JSON body content.", "text", ["text", "base64"])],
    "200 JSON { id, bytes } with the written byte count.",
    "400 missing id / bad body / socket error, 404 unknown session, 405 wrong method."),
  route("GET", "/api/socket/recv?id=<id>[&timeout=][&max_bytes=]", "socket_recv",
    "Read bytes from an open socket session (transparent: whatever the peer sent). Long-polls up to timeout seconds; returns whatever arrived, an EOF flag when the peer closed, or a timeout flag when nothing arrived in time.",
    [param("id", "query", true, "string",
      "Session id from socket_open."),
    param("timeout", "query", false, "string",
      "Seconds to wait for data (max 60).", "5"),
    param("max_bytes", "query", false, "string",
      "Max bytes per call (1-1048576).", "65536")],
    "200 JSON { id, encoding (base64), content, bytes, eof, timed_out }. content carries base64 bytes (may be empty); eof is true only with empty content after the peer closed; timed_out is true only with empty content when nothing arrived in time.",
    "400 missing id / bad timeout/max_bytes / socket error, 404 unknown session, 405 wrong method."),
  route("POST", "/api/socket/close?id=<id>", "socket_close",
    "Close an open socket session and release it. Idle sessions also expire automatically.",
    [param("id", "query", true, "string",
      "Session id from socket_open.")],
    "200 JSON { id, closed: true }.",
    "400 missing id, 404 unknown session, 405 wrong method."),
  route("POST", "/api/socket/starttls?id=<id>", "socket_starttls",
    "Upgrade a plaintext socket session to TLS in place (STARTTLS-style protocols: drive the cleartext prelude over the session first, then call this before sending any more application bytes). Verified like implicit TLS (system roots + optional ca_pem, hostname check, SNI). A failed handshake leaves the plaintext session untouched; refused while a WebSocket owns the session, while a recv is pending, or when already TLS.",
    [param("id", "query", true, "string",
      "Session id from socket_open."),
    param("sni", "body (JSON)", false, "string",
      "Server name for SNI and certificate verification (default: the session host)."),
    param("ca_pem", "body (JSON)", false, "string",
      "Extra PEM CA bundle appended to the system roots.")],
    "200 JSON { id, kind (tls) }.",
    "400 missing id / bad body / already TLS / recv pending / attached / handshake failure (engine error text), 404 unknown session, 405 wrong method."),
  route("GET", "/api/socket/ws?id=<id>", "socket_ws",
    "Attach a WebSocket to an open socket session (the browser-native byte transport). Upgrade with `Upgrade: websocket` (token via `?token=`); after the 101 handshake, WS message payloads flow transparently client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back as binary frames; ping gets an auto-pong. A close from either side, or any TCP/protocol error, ends the whole session (a WS close is a session close, like HTTP close). HTTP send/recv are refused while attached; HTTP close kills the bridge too.",
    [param("id", "query", true, "string",
      "Session id from socket_open.")],
    "101 Switching Protocols, then transparent bidirectional bytes (see description).",
    "400 not-an-upgrade / bad handshake / missing id / already attached, 404 unknown session, 405 wrong method."),
  route("GET", "/", "index",
    "Service index with version and pointer to /api/help.",
    [], "JSON { service, version, help }."),
];

// -- server ------------------------------------------------------------

export function startServer(opts: Opts, host: string, port: number): http.Server {
  const server = http.createServer((req, res) => {
    handle(req, res, opts).catch((e) => {
      try {
        err(res, 500, `Internal error: ${(e as Error).message}`);
      } catch { /* ignore */ }
    });
  });
  server.on("upgrade",
    (req: http.IncomingMessage, socket: net.Socket, head: Buffer) => {
      handleSocketWsUpgrade(req, socket, head, opts).catch(() => {
        try { socket.destroy(); } catch { /* ignore */ }
      });
    });
  server.listen(port, host);
  return server;
}

