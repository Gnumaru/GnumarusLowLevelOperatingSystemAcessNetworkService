import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import java.io.File
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.net.URLDecoder
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.nio.file.DirectoryNotEmptyException
import java.nio.file.FileAlreadyExistsException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.NoSuchFileException
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.AccessDeniedException
import java.nio.file.attribute.BasicFileAttributes
import java.nio.file.attribute.PosixFilePermission
import java.security.MessageDigest
import java.sql.DriverManager
import java.util.Base64
import java.util.concurrent.TimeUnit
import org.sqlite.SQLiteConfig

const val VERSION = "0.10.0"
const val SERVICE = "GnumarusPythonNativeOperationsHttpService"
val OPEN_ROUTES = setOf("/", "/api/help")
val GSON = com.google.gson.GsonBuilder().serializeNulls().create()

const val SHELL_DEFAULT_TIMEOUT = 30.0
const val SHELL_MAX_TIMEOUT = 300.0
const val SHELL_DEFAULT_MAX_OUTPUT = 1024 * 1024
const val SHELL_MAX_OUTPUT = 100 * 1024 * 1024
const val SQLITE_DEFAULT_TIMEOUT = 5.0
const val SQLITE_MAX_TIMEOUT = 300.0

var CWD: String = System.getProperty("user.dir")

// ---------------------------------------------------------------------------
// tiny JSON builders
// ---------------------------------------------------------------------------

fun jo(vararg p: Pair<String, Any?>): JsonObject {
    val o = JsonObject()
    for ((k, v) in p) o.add(k, anyToJson(v))
    return o
}

fun anyToJson(v: Any?): JsonElement = when (v) {
    null -> JsonNull.INSTANCE
    is JsonElement -> v
    is String -> com.google.gson.JsonPrimitive(v)
    is Boolean, is Number ->
        throw IllegalArgumentException("bare scalar on the wire: use jenc()")
    is List<*> -> JsonArray().also { a -> v.forEach { a.add(anyToJson(it)) } }
    is Map<*, *> -> JsonObject().also { o ->
        v.forEach { (k, vv) -> o.add(k.toString(), anyToJson(vv)) }
    }
    else -> com.google.gson.JsonPrimitive(v.toString())
}

fun param(
    name: String, location: String, required: Boolean, type: String,
    desc: String, default: String? = null, allowed: List<String>? = null
): JsonObject {
    val o = jo("name" to name, "location" to location, "required" to jenc(required),
        "type" to type, "description" to desc)
    if (default != null) o.addProperty("default", default)
    if (allowed != null) o.add("allowed", anyToJson(allowed))
    return o
}

fun routeEntry(
    method: String, url: String, name: String, desc: String,
    params: List<JsonObject>, response: String, errors: String? = null
): JsonObject {
    val o = jo("method" to method, "url" to url, "name" to name,
        "description" to desc, "params" to anyToJson(params),
        "response" to response)
    if (errors != null) o.addProperty("errors", errors)
    return o
}

fun buildRoutes(): JsonArray {
    val r = JsonArray()
    r.add(routeEntry("GET", "/api/help", "help",
        "Return the full list of implemented routes with URL, name, description and parameter formats. Wire convention: JSON carries only string/array/object - numbers, booleans and nulls travel as tagged strings (i:<int>, f:<float>, b:true|false, null). Fixed vocabulary and free text stay raw; sqlite row cells and params values are fully tagged (strings take an s: prefix there).",
        listOf(), "JSON object { version, count, routes: [...] }."))
    r.add(routeEntry("GET", "/api/dir?path=<path>", "read_directory",
        "List the contents of a directory.",
        listOf(param("path", "query", true, "string",
            "Absolute or relative path of the directory. Relative paths resolve against the server working directory. Paths starting with ~/ expand to the server user's home directory.")),
        "JSON { path, absolute_path, count, entries: [{ name, path, type, size, atime, mtime, ctime, crtime, mode }] }. type is one of file|dir|symlink|other. Times are epoch seconds: atime (last access), mtime (last modification), ctime (last status change; creation on Windows), crtime (creation/birth, null where unavailable).",
        "400 missing path / not-a-directory, 404 not found, 403 forbidden."))
    r.add(routeEntry("GET", "/api/file?path=<path>[&encoding=text|base64|auto][&raw=1]", "read_file",
        "Read a file. Supports text and binary files. Default JSON mode auto-detects: UTF-8-decodable content is returned as text, otherwise as base64. With raw=1 the raw bytes are returned with a guessed Content-Type.",
        listOf(
            param("path", "query", true, "string",
                "Absolute or relative path of the file. Relative paths resolve against the server working directory; ~/ expands to the user's home directory."),
            param("encoding", "query", false, "string",
                "auto: try UTF-8 text, fall back to base64. text: force UTF-8 decode with replacement of invalid sequences. base64: force base64.",
                "auto", listOf("auto", "text", "base64")),
            param("raw", "query", false, "string",
                "If '1', respond with raw file bytes and a guessed Content-Type instead of JSON.",
                "0", listOf("0", "1"))),
        "JSON mode: { path, absolute_path, size, mtime, mime_type, is_binary, encoding, content }. Raw mode: file bytes with Content-Type + Content-Length headers.",
        "400 missing path / is-a-directory, 404 not found, 403 forbidden."))
    r.add(routeEntry("POST", "/api/file?path=<path>", "create_file",
        "Create a file, empty when no body is sent or with initial data (text or binary). Fails if the path already exists (use PUT to overwrite).",
        listOf(
            param("path", "query", true, "string",
                "Absolute or relative path of the file to create. ~/ expands to the server user's home directory."),
            param("content", "body (JSON)", false, "string",
                "Initial content. Plain string for encoding=text, base64 string for encoding=base64. Omitted means empty file. Alternatively send raw bytes with a non-JSON Content-Type."),
            param("encoding", "body (JSON)", false, "string",
                "How to interpret the JSON body content.", "text", listOf("text", "base64"))),
        "201 JSON { path, absolute_path, size, mime_type }.",
        "400 missing path / bad body, 404 parent directory missing, 409 already exists, 403 forbidden."))
    r.add(routeEntry("PUT", "/api/file?path=<path>", "overwrite_file",
        "Overwrite the content of an existing file (text or binary). Same body rules as create_file.",
        listOf(
            param("path", "query", true, "string",
                "Absolute or relative path of the file to overwrite. ~/ expands to the server user's home directory."),
            param("content", "body (JSON)", false, "string",
                "New content (see create_file). Omitted truncates the file to zero bytes."),
            param("encoding", "body (JSON)", false, "string",
                "How to interpret the JSON body content.", "text", listOf("text", "base64"))),
        "200 JSON { path, absolute_path, size, mime_type }.",
        "400 missing path / is-a-directory / bad body, 404 not found, 403 forbidden."))
    r.add(routeEntry("DELETE", "/api/file?path=<path>", "delete_file",
        "Delete a file.",
        listOf(param("path", "query", true, "string",
            "Absolute or relative path of the file to delete. ~/ expands to the server user's home directory.")),
        "200 JSON { path, absolute_path, deleted: true }.",
        "400 missing path / is-a-directory, 404 not found, 403 forbidden."))
    r.add(routeEntry("DELETE", "/api/dir?path=<path>", "delete_directory",
        "Delete an empty directory (rmdir equivalent). Fails when the directory is not empty.",
        listOf(param("path", "query", true, "string",
            "Absolute or relative path of the empty directory to delete. ~/ expands to the server user's home directory.")),
        "200 JSON { path, absolute_path, deleted: true }.",
        "400 missing path / not-a-directory / not-empty, 404 not found, 403 forbidden."))
    r.add(routeEntry("POST", "/api/shell", "exec_shell",
        "Execute a bash command string (pipes, redirections and all bash syntax allowed) and return the exit code plus separately captured stdout and stderr. WARNING: this is arbitrary remote code execution on the host; the server binds localhost by default and relies on that for protection.",
        listOf(
            param("command", "body (JSON)", true, "string",
                "Exact string as typed in bash, e.g. \"cat < a.txt | sort > b.txt\"."),
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
                "text", listOf("text", "base64"))),
        "200 JSON { command, cwd, returncode, stdout, stderr, timed_out, stdout_truncated, stderr_truncated, max_stdout, max_stderr, stdin_bytes }. returncode is the exit status of the last pipeline element (plain bash semantics). stdout/stderr are what the shell process emitted on its own fds, so in-command redirections apply: '> f' leaves captured stdout empty, '2>&1' merges into captured stdout. stdin_bytes is how many input bytes were fed.",
        "400 missing/empty command / bad JSON / bad timeout / bad cwd / bad or over-cap output limits, 405 wrong method."))
    r.add(routeEntry("POST", "/api/sqlite/create?path=<path>", "sqlite_create",
        "Create a new SQLite database file. The file is permanent; only the per-request connection is closed afterwards.",
        listOf(param("path", "query", true, "string", "Full path of the database file to create.")),
        "201 JSON { path, absolute_path, size, sqlite_version }.",
        "400 missing path, 404 parent directory missing, 409 file already exists, 403 forbidden."))
    r.add(routeEntry("POST", "/api/sqlite/query?path=<db-path>", "sqlite_query",
        "Execute one arbitrary SQL statement on a SQLite database. Stateless: a fresh connection is opened per request and closed right after. SELECT-style statements return rows; anything else is committed and returns the change counters.",
        listOf(
            param("path", "query", true, "string", "Path of the database file."),
            param("sql", "body (JSON)", true, "string",
                "Exactly one SQL statement. Multi-statement strings are rejected (send one per request)."),
            param("params", "body (JSON)", false, "array | object",
                "Bound parameters: array for ? placeholders, object for :named placeholders. Values must be JSON scalars (BLOB input is not supported).",
                "[]"),
            param("timeout", "body (JSON)", false, "number",
                "Busy timeout in seconds while waiting on a locked database (max 300).", "i:5"),
            param("readonly", "body (JSON)", false, "boolean",
                "Open the database read-only.", "b:false")),
        "200 JSON, SELECT-style: { path, absolute_path, columns, rows, rowcount } with BLOB values as base64 strings. Writes: { path, absolute_path, rowcount, lastrowid }.",
        "400 missing path/sql, bad JSON/params/timeout, multi-statement, or the SQLite error text itself (e.g. no such table). 404 database file not found, 403 forbidden, 405 wrong method."))
    r.add(routeEntry("POST", "/api/socket/open", "socket_open",
        "Open a TCP connection to a network peer and hold it as a session, so browser pages (which cannot open raw sockets) can speak arbitrary TCP protocols through the local service (e.g. SMTP/IMAP for a browser email client). With tls:b:true the handshake runs immediately (implicit TLS, e.g. IMAPS on 993), verified against the system roots (+ optional ca_pem) with hostname check and SNI. Returns an opaque session id for send/recv/close/starttls. WARNING: this is an open network proxy on the host; the server binds localhost by default and relies on that for protection.",
        listOf(param("host", "body (JSON)", true, "string",
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
                "Extra PEM CA bundle appended to the system roots (corporate intercepts, test PKIs).")),
        "201 JSON { id, kind (tcp|tls), host, port }. The id is an opaque session token for the other socket routes.",
        "400 missing/invalid host/port/timeout/tls/sni/ca_pem, bad JSON, connect or TLS-handshake failure (engine error text), or too many open sessions. 405 wrong method."))
    r.add(routeEntry("POST", "/api/socket/send?id=<id>", "socket_send",
        "Write bytes to an open socket session (transparent: the peer receives exactly these bytes).",
        listOf(param("id", "query", true, "string",
            "Session id from socket_open."),
            param("content", "body (JSON)", false, "string",
                "Bytes to write. Plain string for encoding=text, base64 string for encoding=base64. Alternatively send raw bytes with a non-JSON Content-Type."),
            param("encoding", "body (JSON)", false, "string",
                "How to interpret the JSON body content.", "text", listOf("text", "base64"))),
        "200 JSON { id, bytes } with the written byte count.",
        "400 missing id / bad body / socket error, 404 unknown session, 405 wrong method."))
    r.add(routeEntry("GET", "/api/socket/recv?id=<id>[&timeout=][&max_bytes=]", "socket_recv",
        "Read bytes from an open socket session (transparent: whatever the peer sent). Long-polls up to timeout seconds; returns whatever arrived, an EOF flag when the peer closed, or a timeout flag when nothing arrived in time.",
        listOf(param("id", "query", true, "string",
            "Session id from socket_open."),
            param("timeout", "query", false, "string",
                "Seconds to wait for data (max 60).", "5"),
            param("max_bytes", "query", false, "string",
                "Max bytes per call (1-1048576).", "65536")),
        "200 JSON { id, encoding (base64), content, bytes, eof, timed_out }. content carries base64 bytes (may be empty); eof is true only with empty content after the peer closed; timed_out is true only with empty content when nothing arrived in time.",
        "400 missing id / bad timeout/max_bytes / socket error, 404 unknown session, 405 wrong method."))
    r.add(routeEntry("POST", "/api/socket/close?id=<id>", "socket_close",
        "Close an open socket session and release it. Idle sessions also expire automatically.",
        listOf(param("id", "query", true, "string",
            "Session id from socket_open.")),
        "200 JSON { id, closed: true }.",
        "400 missing id, 404 unknown session, 405 wrong method."))
    r.add(routeEntry("POST", "/api/socket/starttls?id=<id>", "socket_starttls",
        "Upgrade a plaintext socket session to TLS in place (STARTTLS-style protocols: drive the cleartext prelude over the session first, then call this before sending any more application bytes). Verified like implicit TLS (system roots + optional ca_pem, hostname check, SNI). A failed handshake leaves the plaintext session untouched; refused while a WebSocket owns the session, while a recv is pending, or when already TLS.",
        listOf(param("id", "query", true, "string",
            "Session id from socket_open."),
            param("sni", "body (JSON)", false, "string",
                "Server name for SNI and certificate verification (default: the session host)."),
            param("ca_pem", "body (JSON)", false, "string",
                "Extra PEM CA bundle appended to the system roots.")),
        "200 JSON { id, kind (tls) }.",
        "400 missing id / bad body / already TLS / recv pending / attached / handshake failure (engine error text), 404 unknown session, 405 wrong method."))
    r.add(routeEntry("GET", "/api/socket/ws?id=<id>", "socket_ws",
        "Attach a WebSocket to an open socket session (the browser-native byte transport). Upgrade with `Upgrade: websocket` (token via `?token=` since browsers cannot set WS headers); after the 101 handshake, WS message payloads flow transparently client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back as binary frames; ping gets an auto-pong. A close from either side, or any TCP/protocol error, ends the whole session (a WS close is a session close, like HTTP close). HTTP send/recv are refused while attached; HTTP close kills the bridge too.",
        listOf(param("id", "query", true, "string",
            "Session id from socket_open.")),
        "101 Switching Protocols, then transparent bidirectional bytes (see description).",
        "400 not-an-upgrade / bad handshake / missing id / already attached / recv pending, 401 missing/invalid token, 404 unknown session, 405 wrong method."))
    r.add(routeEntry("GET", "/api/sysinfo", "system_info",
        "Return operating-system information about the host running the server. Takes no parameters.",
        listOf(),
        "200 JSON { system, system_name, release, version, machine, processor, hostname, python_version, cpu_count, distro, windows, mac }. system is the raw platform name (Linux/Windows/Darwin); system_name is the friendly one (Darwin -> macOS). distro is parsed from /etc/os-release on Linux and null elsewhere; windows/mac objects appear only on those systems.",
        "401 missing/invalid token, 405 wrong method."))
    r.add(routeEntry("GET", "/", "index",
        "Service index with version and pointer to /api/help.",
        listOf(), "JSON { service, version, help }."))
    return r
}

val ROUTES: JsonArray by lazy { buildRoutes() }

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

fun statusLabel(status: Int): String = when (status) {
    400 -> "bad_request"
    401 -> "unauthorized"
    403 -> "forbidden"
    404 -> "not_found"
    405 -> "method_not_allowed"
    409 -> "conflict"
    else -> "error"
}

fun cors(ex: Exchange) {
    ex.responseHeaders.set("Access-Control-Allow-Origin", "*")
    ex.responseHeaders.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
    ex.responseHeaders.set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
    ex.responseHeaders.set("Access-Control-Max-Age", "86400")
}

fun sendJson(ex: Exchange, status: Int, el: JsonElement) {
    val body = GSON.toJson(el).toByteArray(StandardCharsets.UTF_8)
    cors(ex)
    ex.responseHeaders.set("Content-Type", "application/json; charset=utf-8")
    ex.sendResponseHeaders(status, body.size.toLong())
    ex.responseBody.use { it.write(body) }
    ex.close()
}

fun sendRaw(ex: Exchange, status: Int, body: ByteArray, contentType: String) {
    cors(ex)
    ex.responseHeaders.set("Content-Type", contentType)
    ex.sendResponseHeaders(status, body.size.toLong())
    ex.responseBody.use { it.write(body) }
    ex.close()
}

fun err(ex: Exchange, status: Int, message: String, path: String? = null) {
    val o = jo("error" to statusLabel(status), "message" to message)
    if (path != null) o.addProperty("path", path)
    sendJson(ex, status, o)
}

fun queryParams(ex: Exchange): Map<String, String> {
    val raw = ex.requestURI.rawQuery ?: return mapOf()
    val out = LinkedHashMap<String, String>()
    for (part in raw.split("&")) {
        if (part.isEmpty()) continue
        val eq = part.indexOf('=')
        val k = URLDecoder.decode(if (eq < 0) part else part.substring(0, eq), StandardCharsets.UTF_8)
        if (k !in out) {
            out[k] = if (eq < 0) "" else URLDecoder.decode(part.substring(eq + 1), StandardCharsets.UTF_8)
        }
    }
    return out
}

fun readBody(ex: Exchange): ByteArray = ex.requestBody.readAllBytes()

fun reqJson(ex: Exchange): JsonObject? {
    val ct = ex.requestHeaders.getFirst("Content-Type") ?: ""
    if (!ct.contains("application/json")) {
        err(ex, 400, "Content-Type must be application/json.")
        return null
    }
    val raw = readBody(ex)
    return try {
        when (val v = com.google.gson.JsonParser.parseString(
            if (raw.isEmpty()) "{}" else raw.toString(StandardCharsets.UTF_8))) {
            is JsonObject -> v
            else -> {
                err(ex, 400, "JSON body must be an object.")
                null
            }
        }
    } catch (e: Exception) {
        err(ex, 400, "Invalid JSON body.")
        null
    }
}

fun resolvePath(rawPath: String): String {
    var p = rawPath
    if (p == "~" || p.startsWith("~/")) {
        p = System.getProperty("user.home") + p.substring(1)
    }
    val path = Paths.get(p)
    val abs = if (path.isAbsolute) path else Paths.get(CWD, p)
    return abs.normalize().toString()
}

fun entryType(p: Path): String = when {
    Files.isSymbolicLink(p) -> "symlink"
    Files.isDirectory(p, LinkOption.NOFOLLOW_LINKS) -> "dir"
    Files.isRegularFile(p, LinkOption.NOFOLLOW_LINKS) -> "file"
    else -> "other"
}

fun fileTimeEpoch(ft: java.nio.file.attribute.FileTime): Double {
    val ins = ft.toInstant()
    return ins.epochSecond + ins.nano / 1e9
}

fun fileTimeOrNull(ft: java.nio.file.attribute.FileTime): Double? {
    val ins = ft.toInstant()
    if (ins.epochSecond == 0L && ins.nano == 0) return null
    return ins.epochSecond + ins.nano / 1e9
}

private val INT_TAG = Regex("-?(0|[1-9][0-9]*)")

/** Encode a value scalar as a tagged string (i:/f:/b:/null). */
fun jenc(v: Any?): String = when (v) {
    null -> "null"
    is Boolean -> if (v) "b:true" else "b:false"
    is Long -> "i:$v"
    is Int -> "i:$v"
    is Double -> {
        require(v.isFinite()) { "non-finite double" }
        "f:" + java.math.BigDecimal(v.toString()).stripTrailingZeros().toPlainString()
    }
    is Float -> jenc(v.toDouble())
    else -> throw IllegalArgumentException("jenc takes null/bool/int/double")
}

/** Tag a string for fully-tagged positions (row cells, params values). */
fun jtagStr(s: String): String = "s:$s"

class BadTag(msg: String) : Exception(msg)

/** Parse a tagged string back to a value. Throws BadTag. */
fun jdec(s: String): Any? {
    if (s == "null") return null
    if (s.startsWith("i:")) {
        val body = s.substring(2)
        if (!INT_TAG.matches(body)) throw BadTag("bad int tag: $s")
        return body.toLong()
    }
    if (s.startsWith("f:")) {
        val v = s.substring(2).toDoubleOrNull() ?: throw BadTag("bad float tag: $s")
        if (!v.isFinite()) throw BadTag("bad float tag: $s")
        return v
    }
    if (s == "b:true") return true
    if (s == "b:false") return false
    if (s.startsWith("s:")) return s.substring(2)
    throw BadTag("untagged scalar: $s")
}

// ---------------------------------------------------------------------------
// Linux ctime via statx (JNA). NIO exposes atime/mtime/birth but no
// change-time, so this single extra syscall per entry closes the gap.
// ---------------------------------------------------------------------------

val IS_LINUX = System.getProperty("os.name", "").startsWith("Linux")

fun interface StatxLib : com.sun.jna.Library {
    fun statx(dirfd: Int, path: String?, flags: Int, mask: Int, buf: StatxBuf): Int
}

@com.sun.jna.Structure.FieldOrder("tv_sec", "tv_nsec", "reserved")
open class StatxTs : com.sun.jna.Structure() {
    @JvmField var tv_sec: Long = 0
    @JvmField var tv_nsec: Int = 0
    @JvmField var reserved: Int = 0
}

@com.sun.jna.Structure.FieldOrder("stx_mask", "stx_blksize", "stx_attributes",
    "stx_nlink", "stx_uid", "stx_gid", "stx_mode", "spare0",
    "stx_ino", "stx_size", "stx_blocks", "stx_attributes_mask",
    "stx_atime", "stx_btime", "stx_ctime", "stx_mtime",
    "stx_rdev_major", "stx_rdev_minor", "stx_dev_major", "stx_dev_minor", "spare2")
open class StatxBuf : com.sun.jna.Structure() {
    @JvmField var stx_mask: Int = 0
    @JvmField var stx_blksize: Int = 0
    @JvmField var stx_attributes: Long = 0
    @JvmField var stx_nlink: Int = 0
    @JvmField var stx_uid: Int = 0
    @JvmField var stx_gid: Int = 0
    @JvmField var stx_mode: Short = 0
    @JvmField var spare0: ShortArray = ShortArray(1)
    @JvmField var stx_ino: Long = 0
    @JvmField var stx_size: Long = 0
    @JvmField var stx_blocks: Long = 0
    @JvmField var stx_attributes_mask: Long = 0
    @JvmField var stx_atime: StatxTs = StatxTs()
    @JvmField var stx_btime: StatxTs = StatxTs()
    @JvmField var stx_ctime: StatxTs = StatxTs()
    @JvmField var stx_mtime: StatxTs = StatxTs()
    @JvmField var stx_rdev_major: Int = 0
    @JvmField var stx_rdev_minor: Int = 0
    @JvmField var stx_dev_major: Int = 0
    @JvmField var stx_dev_minor: Int = 0
    @JvmField var spare2: LongArray = LongArray(14)
}

val statxLib: StatxLib? by lazy {
    if (!IS_LINUX) null else try {
        com.sun.jna.Native.load("c", StatxLib::class.java)
    } catch (e: Exception) {
        null
    }
}

/** Change time via statx on Linux, else null (caller falls back). */
fun linuxCtime(path: String): Double? {
    val libc = statxLib ?: return null
    return try {
        val buf = StatxBuf()
        val ret = libc.statx(-100, path, 0x100, 0x800, buf)
        if (ret != 0) return null
        buf.stx_ctime.tv_sec + buf.stx_ctime.tv_nsec / 1e9
    } catch (e: Exception) {
        null
    }
}

val MIME_BY_EXT = mapOf(
    "txt" to "text/plain", "html" to "text/html", "htm" to "text/html",
    "json" to "application/json", "xml" to "text/xml", "css" to "text/css",
    "js" to "text/javascript", "png" to "image/png", "jpg" to "image/jpeg",
    "jpeg" to "image/jpeg", "gif" to "image/gif", "pdf" to "application/pdf",
    "zip" to "application/zip", "csv" to "text/csv", "md" to "text/markdown"
)

fun guessMime(path: String): String {
    val dot = path.substringAfterLast('.', "")
    if (dot.isEmpty() || dot == path) return "application/octet-stream"
    return MIME_BY_EXT[dot.lowercase()] ?: "application/octet-stream"
}

fun modeString(p: Path): String {
    return try {
        var bits = 0
        for (perm in Files.getPosixFilePermissions(p, LinkOption.NOFOLLOW_LINKS)) {
            bits += when (perm) {
                PosixFilePermission.OWNER_READ -> 256
                PosixFilePermission.OWNER_WRITE -> 128
                PosixFilePermission.OWNER_EXECUTE -> 64
                PosixFilePermission.GROUP_READ -> 32
                PosixFilePermission.GROUP_WRITE -> 16
                PosixFilePermission.GROUP_EXECUTE -> 8
                PosixFilePermission.OTHERS_READ -> 4
                PosixFilePermission.OTHERS_WRITE -> 2
                PosixFilePermission.OTHERS_EXECUTE -> 1
            }
        }
        "0o" + Integer.toOctalString(bits)
    } catch (e: Exception) {
        "0o644"
    }
}

fun decodeStrict(bytes: ByteArray): String? {
    return try {
        StandardCharsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes)).toString()
    } catch (e: java.nio.charset.CharacterCodingException) {
        null
    }
}

// ---------------------------------------------------------------------------
// SQL helpers (single-statement scan, classification, param binding)
// ---------------------------------------------------------------------------

class BadSql(msg: String) : Exception(msg)

data class SqlScan(val statements: List<String>, val firstWord: String, val hasReturning: Boolean)

fun scanSql(sql: String): SqlScan {
    val stmts = ArrayList<String>()
    val cur = StringBuilder()
    var firstWord: String? = null
    var hasReturning = false
    var word = StringBuilder()
    fun flushWord() {
        if (word.isNotEmpty()) {
            val w = word.toString().uppercase()
            if (firstWord == null) firstWord = w
            if (w == "RETURNING") hasReturning = true
            word = StringBuilder()
        }
    }
    var state = 0 // 0 normal, 1 squote, 2 dquote, 3 bquote, 4 linecomment, 5 blockcomment
    var i = 0
    val n = sql.length
    while (i < n) {
        val c = sql[i]
        when (state) {
            0 -> when {
                c == '\'' -> { state = 1; cur.append(c); i++ }
                c == '"' -> { state = 2; cur.append(c); i++ }
                c == '`' -> { state = 3; cur.append(c); i++ }
                c == '-' && i + 1 < n && sql[i + 1] == '-' -> { state = 4; i += 2 }
                c == '/' && i + 1 < n && sql[i + 1] == '*' -> { state = 5; i += 2 }
                c == ';' -> {
                    flushWord()
                    if (cur.toString().isNotBlank()) stmts.add(cur.toString())
                    cur.clear(); i++
                }
                c.isLetter() || c == '_' -> { word.append(c); cur.append(c); i++ }
                else -> {
                    if (!c.isLetterOrDigit() && c != '$') flushWord()
                    cur.append(c); i++
                }
            }
            1 -> if (c == '\'' && i + 1 < n && sql[i + 1] == '\'') {
                cur.append("''"); i += 2
            } else {
                cur.append(c); i++
                if (c == '\'') state = 0
            }
            2 -> if (c == '"' && i + 1 < n && sql[i + 1] == '"') {
                cur.append("\"\""); i += 2
            } else {
                cur.append(c); i++
                if (c == '"') state = 0
            }
            3 -> {
                cur.append(c); i++
                if (c == '`') state = 0
            }
            4 -> {
                if (c == '\n') state = 0
                i++
            }
            else -> {
                if (c == '*' && i + 1 < n && sql[i + 1] == '/') {
                    state = 0; i += 2
                } else i++
            }
        }
    }
    flushWord()
    if (cur.toString().isNotBlank()) stmts.add(cur.toString())
    return SqlScan(stmts, firstWord ?: "", hasReturning)
}

val ROW_FIRST_WORDS = setOf("SELECT", "VALUES", "WITH", "EXPLAIN", "PRAGMA", "TABLE")
val DDL_FIRST_WORDS = setOf("CREATE", "DROP", "ALTER", "VACUUM", "ANALYZE", "REINDEX",
    "ATTACH", "DETACH", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "END")

/**
 * Rewrite named placeholders (:a, @a, $a) to positional `?` for JDBC,
 * which has no named parameters. Returns (rewrittenSql, nameOrder).
 * Only placeholder TOKENS are rewritten (strings/comments skipped);
 * values never touch SQL text — they go through PreparedStatement.
 * Throws BadSql on mixed placeholder styles or unknown names.
 */
fun rewriteNamed(sql: String, named: Map<String, Any?>, positional: List<Any?>): Pair<String, List<String>> {
    if (positional.isNotEmpty()) throw BadSql("Named placeholder with array params.")
    val out = StringBuilder()
    val order = ArrayList<String>()
    var state = 0
    var i = 0
    val n = sql.length
    while (i < n) {
        val c = sql[i]
        when (state) {
            0 -> when {
                c == '\'' -> { state = 1; out.append(c); i++ }
                c == '"' -> { state = 2; out.append(c); i++ }
                c == '`' -> { state = 3; out.append(c); i++ }
                c == '-' && i + 1 < n && sql[i + 1] == '-' -> { state = 4; i += 2 }
                c == '/' && i + 1 < n && sql[i + 1] == '*' -> { state = 5; i += 2 }
                c == '?' -> throw BadSql("Unnamed placeholder with object params.")
                (c == ':' || c == '@' || c == '$') && i + 1 < n &&
                        (sql[i + 1].isLetter() || sql[i + 1] == '_') -> {
                    var j = i + 1
                    while (j < n && (sql[j].isLetterOrDigit() || sql[j] == '_')) j++
                    val name = sql.substring(i + 1, j)
                    if (!named.containsKey(name)) throw BadSql("Missing named param: $name.")
                    out.append('?')
                    order.add(name)
                    i = j
                }
                else -> { out.append(c); i++ }
            }
            1 -> if (c == '\'' && i + 1 < n && sql[i + 1] == '\'') {
                out.append("''"); i += 2
            } else {
                out.append(c); i++
                if (c == '\'') state = 0
            }
            2 -> if (c == '"' && i + 1 < n && sql[i + 1] == '"') {
                out.append("\"\""); i += 2
            } else {
                out.append(c); i++
                if (c == '"') state = 0
            }
            3 -> {
                out.append(c); i++
                if (c == '`') state = 0
            }
            4 -> {
                if (c == '\n') state = 0
                i++
            }
            else -> {
                if (c == '*' && i + 1 < n && sql[i + 1] == '/') {
                    state = 0; i += 2
                } else i++
            }
        }
    }
    return out.toString() to order
}

/** Throw BadSql if any named placeholder appears outside literals. */
fun ensureNoNamed(sql: String) {
    var state = 0
    var i = 0
    val n = sql.length
    while (i < n) {
        val c = sql[i]
        when (state) {
            0 -> when {
                c == '\'' -> { state = 1; i++ }
                c == '"' -> { state = 2; i++ }
                c == '`' -> { state = 3; i++ }
                c == '-' && i + 1 < n && sql[i + 1] == '-' -> { state = 4; i += 2 }
                c == '/' && i + 1 < n && sql[i + 1] == '*' -> { state = 5; i += 2 }
                (c == ':' || c == '@' || c == '$') && i + 1 < n &&
                        (sql[i + 1].isLetter() || sql[i + 1] == '_') ->
                    throw BadSql("Named placeholder with array params.")
                else -> i++
            }
            1 -> if (c == '\'' && i + 1 < n && sql[i + 1] == '\'') i += 2
            else {
                if (c == '\'') state = 0
                i++
            }
            2 -> if (c == '"' && i + 1 < n && sql[i + 1] == '"') i += 2
            else {
                if (c == '"') state = 0
                i++
            }
            3 -> {
                if (c == '`') state = 0
                i++
            }
            4 -> {
                if (c == '\n') state = 0
                i++
            }
            else -> {
                if (c == '*' && i + 1 < n && sql[i + 1] == '/') {
                    state = 0; i += 2
                } else i++
            }
        }
    }
}

/** Bind a decoded tag value natively (bool -> 1/0 for Python parity). */
fun bindJdbc(ps: java.sql.PreparedStatement, idx: Int, v: Any?) {
    when (v) {
        null -> ps.setNull(idx, java.sql.Types.NULL)
        is Boolean -> ps.setLong(idx, if (v) 1L else 0L)
        is Long -> ps.setLong(idx, v)
        is Int -> ps.setLong(idx, v.toLong())
        is Double -> ps.setDouble(idx, v)
        is Float -> ps.setDouble(idx, v.toDouble())
        is String -> ps.setString(idx, v)
        else -> throw BadSql("Params must be tagged scalars (i:/f:/s:/b:/null).")
    }
}

// ---------------------------------------------------------------------------
// SQLite backend (JDBC driver when present)
// ---------------------------------------------------------------------------

val sqliteAvailable: Boolean by lazy {
    try {
        java.sql.DriverManager.getConnection("jdbc:sqlite::memory:").close()
        true
    } catch (e: Exception) {
        false
    }
}

// ---------------------------------------------------------------------------
// socket proxy
// ---------------------------------------------------------------------------

const val SOCK_DEFAULT_CONNECT_TIMEOUT = 5.0
const val SOCK_MAX_CONNECT_TIMEOUT = 30.0
const val SOCK_DEFAULT_RECV_TIMEOUT = 5.0
const val SOCK_MAX_RECV_TIMEOUT = 60.0
const val SOCK_DEFAULT_MAX_BYTES = 65536
const val SOCK_MAX_BYTES = 1048576
const val SOCK_MAX_SESSIONS = 128
const val SOCK_IDLE_SECS = 300L

class SockSession(var sock: java.net.Socket, val host: String, val port: Long) {
    @Volatile var last: Long = System.currentTimeMillis()
    @Volatile var eof: Boolean = false
    @Volatile var recvBusy: Boolean = false
    @Volatile var kind: String = "tcp"
    // wsAttached marks a live WebSocket bridge (HTTP send/recv/starttls
    // are refused while set; the idle sweeper skips it).
    @Volatile var wsAttached: Boolean = false
    // Pre-TLS raw socket kept only for STARTTLS sessions (the TLS
    // layer is created with autoClose=false so a failed handshake
    // leaves the plaintext session untouched); closed together.
    @Volatile var rawSock: java.net.Socket? = null
}

fun sockCloseAll(sess: SockSession) {
    try { sess.sock.close() } catch (_: Exception) { }
    try { sess.rawSock?.close() } catch (_: Exception) { }
}

val SOCK_SESSIONS = java.util.concurrent.ConcurrentHashMap<String, SockSession>()

@Volatile var sockSweeperStarted = false

fun sockSweep() {
    val now = System.currentTimeMillis()
    for ((sid, sess) in SOCK_SESSIONS) {
        if (sess.wsAttached) continue
        if (now - sess.last > SOCK_IDLE_SECS * 1000) {
            SOCK_SESSIONS.remove(sid)
            try { sockCloseAll(sess) } catch (_: Exception) { }
        }
    }
}

fun sockEnsureSweeper() {
    if (sockSweeperStarted) return
    synchronized(SOCK_SESSIONS) {
        if (sockSweeperStarted) return
        sockSweeperStarted = true
    }
    Thread({
        while (true) {
            try { Thread.sleep(60000) } catch (_: InterruptedException) { break }
            try { sockSweep() } catch (_: Exception) { }
        }
    }).also { it.isDaemon = true }.start()
}

fun sockMintId(): String {
    val bytes = ByteArray(16)
    java.security.SecureRandom().nextBytes(bytes)
    return java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
}

// ---------------------------------------------------------------------------
// TLS socket sessions (implicit TLS at open, STARTTLS upgrade).
// ---------------------------------------------------------------------------

fun matchDnsName(pattern: String, name: String): Boolean {
    // RFC 6125 left-label wildcard only, ASCII case-insensitive.
    if (pattern.startsWith("*.")) {
        val suffix = pattern.substring(2)
        if (suffix.isEmpty()) return false
        if (!name.endsWith(".$suffix", ignoreCase = true)) return false
        val left = name.substring(0, name.length - suffix.length - 1)
        return left.isNotEmpty() && "." !in left
    }
    return pattern.equals(name, ignoreCase = true)
}

fun verifyPeerHostname(
    sni: String, cert: java.security.cert.X509Certificate
): Boolean {
    return try {
        val sans: List<List<*>>? = try {
            cert.subjectAlternativeNames?.toList()
        } catch (_: Exception) {
            null
        }
        if (isIpLiteral(sni)) {
            val want = java.net.InetAddress.getByName(sni)
            sans?.any { san ->
                san.size >= 2 && (san[0] as? Int) == 7 && san[1] is String &&
                    try {
                        java.net.InetAddress.getByName(san[1] as String) == want
                    } catch (_: Exception) {
                        false
                    }
            } == true
        } else {
            val dns = sans
                ?.filter { it.size >= 2 && (it[0] as? Int) == 2 && it[1] is String }
                ?.map { (it[1] as String).lowercase() }
                ?: emptyList()
            if (dns.isNotEmpty()) {
                dns.any { matchDnsName(it, sni.lowercase()) }
            } else {
                val dn = cert.subjectX500Principal.getName(
                    javax.security.auth.x500.X500Principal.RFC2253)
                val cn = dn.split(",").map { it.trim() }
                    .firstOrNull { it.startsWith("CN=") }
                    ?.substring(3) ?: return false
                matchDnsName(cn.lowercase(), sni.lowercase())
            }
        }
    } catch (_: Exception) {
        false
    }
}

fun isIpLiteral(s: String): Boolean {
    if (s.contains(":")) return true
    if (!s.matches(Regex("[0-9.]+"))) return false
    val parts = s.split(".")
    return parts.size == 4 && parts.all {
        it.isNotEmpty() && it.length <= 3 && it.all { c -> c.isDigit() } &&
            (try { it.toInt() <= 255 } catch (_: Exception) { false })
    }
}

/** Returns Triple(sni, caPem, errorMessage?). */
fun parseTlsIdentity(obj: JsonObject): Triple<String, String, String?> {
    var sni = ""
    val sniEl = obj.get("sni")
    if (sniEl != null && !sniEl.isJsonNull) {
        if (!sniEl.isJsonPrimitive || !sniEl.asJsonPrimitive.isString ||
            sniEl.asString.trim().isEmpty()) {
            return Triple("", "", "Field 'sni' must be a non-empty string.")
        }
        sni = sniEl.asString.trim()
    }
    var caPem = ""
    val caEl = obj.get("ca_pem")
    if (caEl != null && !caEl.isJsonNull) {
        if (!caEl.isJsonPrimitive || !caEl.asJsonPrimitive.isString ||
            caEl.asString.trim().isEmpty()) {
            return Triple("", "", "Field 'ca_pem' must be a PEM CA bundle string.")
        }
        caPem = caEl.asString
    }
    return Triple(sni, caPem, null)
}

/** System roots plus an optional extra PEM bundle. Returns Pair(ctx, error). */
fun tlsContext(caPem: String): Pair<javax.net.ssl.SSLContext?, String?> {
    val extra: Collection<java.security.cert.Certificate>
    if (caPem.isNotEmpty()) {
        try {
            val cf = java.security.cert.CertificateFactory.getInstance("X.509")
            extra = cf.generateCertificates(
                java.io.ByteArrayInputStream(caPem.toByteArray(StandardCharsets.UTF_8)))
            if (extra.isEmpty()) {
                return Pair(null, "Field 'ca_pem' must be a valid PEM CA bundle.")
            }
        } catch (e: Exception) {
            return Pair(null, "Field 'ca_pem' must be a valid PEM CA bundle.")
        }
    } else {
        extra = emptyList()
    }
    return try {
        val ks = java.security.KeyStore.getInstance(java.security.KeyStore.getDefaultType())
        ks.load(null, null)
        val cacerts = java.io.File(
            System.getProperty("java.home") + "/lib/security/cacerts")
        if (cacerts.isFile) {
            java.io.FileInputStream(cacerts).use { fis ->
                ks.load(fis, "changeit".toCharArray())
            }
        }
        var n = 0
        for (c in extra) {
            ks.setCertificateEntry("extra-$n", c)
            n++
        }
        val tmf = javax.net.ssl.TrustManagerFactory.getInstance(
            javax.net.ssl.TrustManagerFactory.getDefaultAlgorithm())
        tmf.init(ks)
        val ctx = javax.net.ssl.SSLContext.getInstance("TLS")
        ctx.init(null, tmf.trustManagers, java.security.SecureRandom())
        Pair(ctx, null)
    } catch (e: Exception) {
        Pair(null, "TLS setup failed: ${e.message}")
    }
}

/**
 * Client handshake over an established TCP connection, verified with
 * hostname check + SNI. With autoClose=true the raw connection dies
 * with a failed handshake (open path); with false it survives
 * (starttls path keeps the plaintext session).
 */
fun tlsWrap(
    raw: java.net.Socket, sni: String, ctx: javax.net.ssl.SSLContext,
    timeoutMs: Int, autoClose: Boolean
): javax.net.ssl.SSLSocket {
    val factory = ctx.socketFactory
    val sslSock = factory.createSocket(
        raw, raw.inetAddress.hostAddress, raw.port, autoClose)
        as javax.net.ssl.SSLSocket
    try {
        if (!isIpLiteral(sni)) {
            val params = sslSock.sslParameters
            params.serverNames = listOf(javax.net.ssl.SNIHostName(sni))
            sslSock.sslParameters = params
        }
        sslSock.soTimeout = timeoutMs
        sslSock.startHandshake()
        val peerCerts = try {
            sslSock.session.peerCertificates
        } catch (e: Exception) {
            throw javax.net.ssl.SSLPeerUnverifiedException(
                "no peer certificates: ${e.message}")
        }
        val leaf = peerCerts.firstOrNull()
            as? java.security.cert.X509Certificate
            ?: throw javax.net.ssl.SSLPeerUnverifiedException(
                "peer did not present an X.509 certificate")
        if (!verifyPeerHostname(sni, leaf)) {
            throw javax.net.ssl.SSLPeerUnverifiedException(
                "hostname verification failed for $sni")
        }
        sslSock.soTimeout = 0
        return sslSock
    } catch (e: Exception) {
        try { sslSock.close() } catch (_: Exception) { }
        throw e
    }
}

// ---------------------------------------------------------------------------
// Loopback-only bind + Host allowlist.
// ---------------------------------------------------------------------------

const val HOST_DENIED_MSG =
    "Host not allowed (use 127.0.0.1, localhost or ::1)."

private fun isAsciiDigits(s: String): Boolean {
    if (s.isEmpty()) return false
    for (c in s) if (c < '0' || c > '9') return false
    return true
}

private fun isLoopbackIp(name: String): Boolean {
    if (name == "::1") return true
    val parts = name.split(".")
    if (parts.size != 4) return false
    for (p in parts) {
        if (p.isEmpty() || (p.length > 1 && p[0] == '0')) return false
        if (!isAsciiDigits(p)) return false
        if (p.length > 3 || p.toInt() > 255) return false
    }
    return parts[0] == "127"
}

private fun isLoopbackName(name: String): Boolean {
    if (name.isEmpty()) return false
    if (name.equals("localhost", ignoreCase = true)) return true
    return isLoopbackIp(name)
}

/** Whether an HTTP Host header targets this service. */
fun hostAllowed(headerValue: String?): Boolean {
    val h = (headerValue ?: "").trim()
    if (h.isEmpty()) return false
    val name: String
    if (h.startsWith("[")) {
        val end = h.indexOf(']')
        if (end < 0) return false
        val rest = h.substring(end + 1)
        if (rest.isNotEmpty()) {
            if (!rest.startsWith(":") || !isAsciiDigits(rest.substring(1))) {
                return false
            }
        }
        name = h.substring(1, end)
    } else {
        if (h.count { it == ':' } > 1) return false
        val i = h.lastIndexOf(':')
        if (i >= 0) {
            if (!isAsciiDigits(h.substring(i + 1))) return false
            name = h.substring(0, i)
        } else {
            name = h
        }
    }
    return isLoopbackName(name)
}

/** Whether an address may be bound: loopback only. */
fun bindAllowed(host: String): Boolean {
    var h = host.trim()
    if (h.startsWith("[") && h.endsWith("]") && h.length > 2) {
        h = h.substring(1, h.length - 1)
    }
    return isLoopbackName(h)
}

// ---------------------------------------------------------------------------
// handler
// ---------------------------------------------------------------------------

class ApiHandler(val authRequired: Boolean, val authToken: String) {

    fun handle(ex: Exchange) {
        try {
            when (ex.requestMethod) {
                "OPTIONS" -> {
                    if (!hostAllowed(ex.requestHeaders.getFirst("Host"))) {
                        err(ex, 403, HOST_DENIED_MSG)
                    } else {
                        cors(ex)
                        ex.sendResponseHeaders(204, -1)
                        ex.close()
                    }
                }
                "GET" -> if (checkAuth(ex)) doGet(ex)
                "POST" -> if (checkAuth(ex)) doPost(ex)
                "PUT" -> if (checkAuth(ex)) doPut(ex)
                "DELETE" -> if (checkAuth(ex)) doDelete(ex)
                else -> if (checkAuth(ex)) err(ex, 405, "Method ${ex.requestMethod} not supported.")
            }
        } catch (e: Exception) {
            try {
                err(ex, 500, "Internal error: ${e.message}")
            } catch (_: Exception) {
            }
        }
    }

    fun checkAuth(ex: Exchange): Boolean {
        if (!hostAllowed(ex.requestHeaders.getFirst("Host"))) {
            err(ex, 403, HOST_DENIED_MSG)
            return false
        }
        if (!authRequired) return true
        val path = ex.requestURI.path
        if (ex.requestMethod == "GET" && path in OPEN_ROUTES) return true
        var supplied = ""
        val h = ex.requestHeaders.getFirst("Authorization") ?: ""
        if (h.startsWith("Bearer ")) supplied = h.removePrefix("Bearer ").trim()
        if (supplied.isEmpty()) supplied = queryParams(ex)["token"] ?: ""
        val a = supplied.toByteArray(StandardCharsets.UTF_8)
        val b = authToken.toByteArray(StandardCharsets.UTF_8)
        if (authToken.isNotEmpty() && MessageDigest.isEqual(a, b)) return true
        cors(ex)
        ex.responseHeaders.set("Content-Type", "application/json; charset=utf-8")
        ex.responseHeaders.set("WWW-Authenticate", "Bearer")
        val body = GSON.toJson(jo("error" to "unauthorized",
            "message" to "Valid API token required: send Authorization: Bearer <token> or ?token=."))
            .toByteArray(StandardCharsets.UTF_8)
        ex.sendResponseHeaders(401, body.size.toLong())
        ex.responseBody.use { it.write(body) }
        ex.close()
        return false
    }

    // -- routing ----------------------------------------------------------

    fun doGet(ex: Exchange) {
        val path = ex.requestURI.path
        val q = queryParams(ex)
        when (path) {
            "/api/help" -> sendJson(ex, 200, jo("version" to VERSION, "count" to jenc(ROUTES.size()),
                "routes" to ROUTES,
                "auth" to jo("required" to jenc(authRequired),
                    "open_routes" to anyToJson(OPEN_ROUTES.toList() + "OPTIONS *"),
                    "how" to "Authorization: Bearer <token> header (preferred) or ?token= query parameter.")))
            "/api/dir" -> handleReadDir(ex, q)
            "/api/file" -> handleReadFile(ex, q)
            "/" -> sendJson(ex, 200, jo("service" to SERVICE, "version" to VERSION, "help" to "/api/help"))
            "/api/shell", "/api/sqlite/create", "/api/sqlite/query",
            "/api/socket/open", "/api/socket/send", "/api/socket/close",
            "/api/socket/starttls" ->
                err(ex, 405, "Method GET not supported for this route (use POST).")
            "/api/socket/recv" -> handleSocketRecv(ex, q)
            "/api/socket/ws" -> handleSocketWs(ex, q)
            "/api/sysinfo" -> handleSysinfo(ex)
            else -> err(ex, 404, "Unknown route: $path")
        }
    }

    fun doPost(ex: Exchange) {
        val path = ex.requestURI.path
        val q = queryParams(ex)
        when (path) {
            "/api/file" -> handleCreateFile(ex, q)
            "/api/shell" -> handleExecShell(ex)
            "/api/sqlite/create" -> handleSqliteCreate(ex, q)
            "/api/sqlite/query" -> handleSqliteQuery(ex, q)
            "/api/socket/open" -> handleSocketOpen(ex)
            "/api/socket/send" -> handleSocketSend(ex, q)
            "/api/socket/close" -> handleSocketClose(ex, q)
            "/api/socket/starttls" -> handleSocketStarttls(ex, q)
            "/", "/api/help", "/api/dir", "/api/sysinfo",
            "/api/socket/recv", "/api/socket/ws" ->
                err(ex, 405, "Method POST not supported for this route.")
            else -> err(ex, 404, "Unknown route: $path")
        }
    }

    fun doPut(ex: Exchange) {
        val path = ex.requestURI.path
        val q = queryParams(ex)
        when (path) {
            "/api/file" -> handleOverwriteFile(ex, q)
            "/", "/api/help", "/api/dir", "/api/shell", "/api/sqlite/create",
            "/api/sqlite/query", "/api/sysinfo",
            "/api/socket/open", "/api/socket/send", "/api/socket/recv",
            "/api/socket/close", "/api/socket/starttls", "/api/socket/ws" ->
                err(ex, 405, "Method PUT not supported for this route.")
            else -> err(ex, 404, "Unknown route: $path")
        }
    }

    fun doDelete(ex: Exchange) {
        val path = ex.requestURI.path
        val q = queryParams(ex)
        when (path) {
            "/api/file" -> handleDeleteFile(ex, q)
            "/api/dir" -> handleDeleteDir(ex, q)
            "/", "/api/help", "/api/shell", "/api/sqlite/create",
            "/api/sqlite/query", "/api/sysinfo",
            "/api/socket/open", "/api/socket/send", "/api/socket/recv",
            "/api/socket/close", "/api/socket/starttls", "/api/socket/ws" ->
                err(ex, 405, "Method DELETE not supported for this route.")
            else -> err(ex, 404, "Unknown route: $path")
        }
    }

    fun reqPath(ex: Exchange, q: Map<String, String>): String? {
        val raw = q["path"]
        if (raw.isNullOrEmpty()) {
            err(ex, 400, "Missing required query parameter: path")
            return null
        }
        return resolvePath(raw)
    }

    // -- socket proxy -----------------------------------------------------

    fun sockLookup(ex: Exchange, q: Map<String, String>): Pair<String, SockSession>? {
        val sid = q["id"]
        if (sid.isNullOrEmpty()) {
            err(ex, 400, "Missing required query parameter: id")
            return null
        }
        val sess = SOCK_SESSIONS[sid]
        if (sess == null) {
            err(ex, 404, "Unknown socket session: $sid")
            return null
        }
        sess.last = System.currentTimeMillis()
        return Pair(sid, sess)
    }

    fun sockReqObj(ex: Exchange): JsonObject? {
        val ct = ex.requestHeaders.getFirst("Content-Type") ?: ""
        if (!ct.contains("application/json")) {
            err(ex, 400, "Content-Type must be application/json with body { host, port, timeout? }.")
            return null
        }
        val raw = readBody(ex)
        return try {
            when (val v = com.google.gson.JsonParser.parseString(
                if (raw.isEmpty()) "{}" else raw.toString(StandardCharsets.UTF_8))) {
                is JsonObject -> v
                else -> {
                    err(ex, 400, "JSON body must be an object.")
                    null
                }
            }
        } catch (e: Exception) {
            err(ex, 400, "Invalid JSON body.")
            null
        }
    }

    fun handleSocketOpen(ex: Exchange) {
        sockEnsureSweeper()
        val obj = sockReqObj(ex) ?: return
        val hostEl = obj.get("host")
        if (hostEl == null || !hostEl.isJsonPrimitive ||
            !hostEl.asJsonPrimitive.isString ||
            hostEl.asString.trim().isEmpty()) {
            err(ex, 400, "Missing or empty required field: host.")
            return
        }
        val host = hostEl.asString
        val portEl = obj.get("port")
        if (portEl == null || portEl.isJsonNull) {
            err(ex, 400, "Missing required field: port (i:<1-65535>).")
            return
        }
        if (!portEl.isJsonPrimitive || !portEl.asJsonPrimitive.isString ||
            !portEl.asString.startsWith("i:")) {
            err(ex, 400, "Field 'port' must be an integer 1-65535 (i:<int>).")
            return
        }
        val port: Long
        try {
            val v = jdec(portEl.asString)
            if (v !is Long) throw BadTag("x")
            port = v
        } catch (e: Exception) {
            err(ex, 400, "Field 'port' must be an integer 1-65535 (i:<int>).")
            return
        }
        if (port < 1 || port > 65535) {
            err(ex, 400, "Field 'port' must be an integer 1-65535 (i:<int>).")
            return
        }
        var timeout = SOCK_DEFAULT_CONNECT_TIMEOUT
        val tEl = obj.get("timeout")
        if (tEl != null && !tEl.isJsonNull) {
            if (!tEl.isJsonPrimitive || !tEl.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).")
                return
            }
            try {
                timeout = when (val v = jdec(tEl.asString)) {
                    is Long -> v.toDouble()
                    is Double -> v
                    else -> throw BadTag("x")
                }
            } catch (e: Exception) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).")
                return
            }
        }
        if (!(timeout > 0 && timeout <= SOCK_MAX_CONNECT_TIMEOUT)) {
            err(ex, 400, "Field 'timeout' must be within (0, $SOCK_MAX_CONNECT_TIMEOUT].")
            return
        }
        var tlsMode = false
        val tlsEl = obj.get("tls")
        if (tlsEl != null && !tlsEl.isJsonNull) {
            if (!tlsEl.isJsonPrimitive || !tlsEl.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'tls' must be b:true or b:false.")
                return
            }
            val t = try {
                jdec(tlsEl.asString)
            } catch (e: Exception) {
                err(ex, 400, "Field 'tls' must be b:true or b:false.")
                return
            }
            if (t !is Boolean) {
                err(ex, 400, "Field 'tls' must be b:true or b:false.")
                return
            }
            tlsMode = t
        }
        val (sni0, caPem, identErr) = parseTlsIdentity(obj)
        if (identErr != null) {
            err(ex, 400, identErr)
            return
        }
        val sni = if (sni0.isEmpty()) host else sni0
        val ctx = if (tlsMode) {
            val (c, ctxErr) = tlsContext(caPem)
            if (ctxErr != null) {
                err(ex, 400, ctxErr)
                return
            }
            c!!
        } else null
        sockSweep()
        if (SOCK_SESSIONS.size >= SOCK_MAX_SESSIONS) {
            err(ex, 400, "Too many open socket sessions ($SOCK_MAX_SESSIONS).")
            return
        }
        val sock = java.net.Socket()
        try {
            sock.connect(InetSocketAddress(host, port.toInt()), (timeout * 1000).toInt())
        } catch (e: Exception) {
            try { sock.close() } catch (_: Exception) { }
            err(ex, 400, "Cannot connect to $host:$port: ${e.message}")
            return
        }
        var peer: java.net.Socket = sock
        var kind = "tcp"
        if (tlsMode) {
            try {
                peer = tlsWrap(sock, sni, ctx!!, (timeout * 1000).toInt(), true)
            } catch (e: Exception) {
                err(ex, 400, "TLS handshake with $host:$port: ${e.message}")
                return
            }
            kind = "tls"
        }
        val sid = sockMintId()
        val sess = SockSession(peer, host, port)
        sess.kind = kind
        SOCK_SESSIONS[sid] = sess
        sendJson(ex, 201, jo("id" to sid, "kind" to kind, "host" to host,
            "port" to jenc(port)))
    }

    fun sockSendData(ex: Exchange, body: ByteArray): ByteArray? {
        val ct = ex.requestHeaders.getFirst("Content-Type") ?: ""
        if (!ct.contains("application/json")) return body
        val obj = try {
            when (val v = com.google.gson.JsonParser.parseString(
                if (body.isEmpty()) "{}" else body.toString(StandardCharsets.UTF_8))) {
                is JsonObject -> v
                else -> {
                    err(ex, 400, "JSON body must be an object.")
                    return null
                }
            }
        } catch (e: Exception) {
            err(ex, 400, "Invalid JSON body.")
            return null
        }
        var content = ""
        val cEl = obj.get("content")
        if (cEl != null && !cEl.isJsonNull) {
            if (!cEl.isJsonPrimitive || !cEl.asJsonPrimitive.isString) {
                err(ex, 400, "'content' must be a string.")
                return null
            }
            content = cEl.asString
        }
        var encoding = "text"
        val eEl = obj.get("encoding")
        if (eEl != null && !eEl.isJsonNull) {
            if (!eEl.isJsonPrimitive || !eEl.asJsonPrimitive.isString) {
                err(ex, 400, "Invalid encoding: (use text|base64).")
                return null
            }
            encoding = eEl.asString.lowercase()
        }
        return when (encoding) {
            "text" -> content.toByteArray(StandardCharsets.UTF_8)
            "base64" -> try {
                Base64.getDecoder().decode(content)
            } catch (e: Exception) {
                err(ex, 400, "Invalid base64 'content'.")
                null
            }
            else -> {
                err(ex, 400, "Invalid encoding: '$encoding' (use text|base64).")
                null
            }
        }
    }

    fun handleSocketSend(ex: Exchange, q: Map<String, String>) {
        val (sid, sess) = sockLookup(ex, q) ?: return
        if (sess.wsAttached) {
            err(ex, 400, "Socket session is attached to a websocket (HTTP send refused while attached).")
            return
        }
        val data = sockSendData(ex, readBody(ex)) ?: return
        try {
            val out = sess.sock.getOutputStream()
            out.write(data)
            out.flush()
        } catch (e: Exception) {
            err(ex, 400, "Socket error: ${e.message}")
            return
        }
        sess.last = System.currentTimeMillis()
        sendJson(ex, 200, jo("id" to sid, "bytes" to jenc(data.size.toLong())))
    }

    fun handleSocketRecv(ex: Exchange, q: Map<String, String>) {
        var timeout = SOCK_DEFAULT_RECV_TIMEOUT
        val tRaw = q["timeout"]
        if (!tRaw.isNullOrEmpty()) {
            val t = tRaw.toDoubleOrNull()
            if (t == null || !(t > 0 && t <= SOCK_MAX_RECV_TIMEOUT)) {
                err(ex, 400, "Invalid timeout param (seconds within (0, $SOCK_MAX_RECV_TIMEOUT]).")
                return
            }
            timeout = t
        }
        var maxBytes = SOCK_DEFAULT_MAX_BYTES
        val mRaw = q["max_bytes"]
        if (!mRaw.isNullOrEmpty()) {
            val m = mRaw.toIntOrNull()
            if (m == null || m < 1 || m > SOCK_MAX_BYTES) {
                err(ex, 400, "Invalid max_bytes param (1-$SOCK_MAX_BYTES).")
                return
            }
            maxBytes = m
        }
        val (sid, sess) = sockLookup(ex, q) ?: return
        if (sess.wsAttached) {
            err(ex, 400, "Socket session is attached to a websocket (HTTP recv refused while attached).")
            return
        }
        synchronized(sess) {
            if (sess.eof) {
                sendJson(ex, 200, jo("id" to sid, "encoding" to "base64",
                    "content" to "", "bytes" to jenc(0L),
                    "eof" to jenc(true), "timed_out" to jenc(false)))
                return
            }
            if (sess.recvBusy) {
                err(ex, 400, "Another recv is already pending on this session.")
                return
            }
            sess.recvBusy = true
        }
        val buf = ByteArray(maxBytes)
        var n = -2
        var failure: String? = null
        try {
            sess.sock.soTimeout = (timeout * 1000).toInt()
            n = sess.sock.getInputStream().read(buf)
        } catch (e: SocketTimeoutException) {
            n = -3
        } catch (e: Exception) {
            failure = e.message ?: "read failed"
        } finally {
            try { sess.sock.soTimeout = 0 } catch (_: Exception) { }
            synchronized(sess) {
                sess.recvBusy = false
                sess.last = System.currentTimeMillis()
                if (n == -1) sess.eof = true
            }
        }
        if (failure != null) {
            err(ex, 400, "Socket error: $failure")
            return
        }
        if (n == -1) {
            sendJson(ex, 200, jo("id" to sid, "encoding" to "base64",
                "content" to "", "bytes" to jenc(0L),
                "eof" to jenc(true), "timed_out" to jenc(false)))
            return
        }
        if (n == -3) {
            sendJson(ex, 200, jo("id" to sid, "encoding" to "base64",
                "content" to "", "bytes" to jenc(0L),
                "eof" to jenc(false), "timed_out" to jenc(true)))
            return
        }
        sendJson(ex, 200, jo("id" to sid, "encoding" to "base64",
            "content" to Base64.getEncoder().encodeToString(buf.copyOf(n)),
            "bytes" to jenc(n.toLong()),
            "eof" to jenc(false), "timed_out" to jenc(false)))
    }

    fun handleSocketClose(ex: Exchange, q: Map<String, String>) {
        val (sid, sess) = sockLookup(ex, q) ?: return
        SOCK_SESSIONS.remove(sid)
        try { sockCloseAll(sess) } catch (_: Exception) { }
        sendJson(ex, 200, jo("id" to sid, "closed" to jenc(true)))
    }

    // -- websocket attach -------------------------------------------------

    fun handleSocketWs(ex: Exchange, q: Map<String, String>) {
        if (!wsHasToken(ex.requestHeaders.getFirst("Connection"), "upgrade") ||
            !(ex.requestHeaders.getFirst("Upgrade") ?: "").trim()
                .equals("websocket", ignoreCase = true)) {
            err(ex, 400, "This route requires a WebSocket upgrade (Upgrade: websocket).")
            return
        }
        if ((ex.requestHeaders.getFirst("Sec-WebSocket-Version") ?: "").trim() != "13") {
            err(ex, 400, "Unsupported WebSocket version (need 13).")
            return
        }
        val key = (ex.requestHeaders.getFirst("Sec-WebSocket-Key") ?: "").trim()
        if (key.isEmpty()) {
            err(ex, 400, "Missing Sec-WebSocket-Key.")
            return
        }
        val (sid, sess) = sockLookup(ex, q) ?: return
        synchronized(sess) {
            if (sess.wsAttached) {
                err(ex, 400, "Socket session is already attached to a websocket.")
                return
            }
            if (sess.recvBusy) {
                err(ex, 400, "Another recv is already pending on this session.")
                return
            }
            sess.wsAttached = true
            sess.last = System.currentTimeMillis()
        }
        val socket = ex.hijack()
        // Clear any leftover HTTP recv timeout on the proxied peer socket.
        try { sess.sock.soTimeout = 0 } catch (_: Exception) { }
        val response = "HTTP/1.1 101 Switching Protocols\r\n" +
            "Upgrade: websocket\r\n" +
            "Connection: Upgrade\r\n" +
            "Sec-WebSocket-Accept: " + wsAcceptKey(key) + "\r\n\r\n"
        try {
            val out = socket.getOutputStream()
            out.write(response.toByteArray(StandardCharsets.US_ASCII))
            out.flush()
        } catch (e: Exception) {
            try { socket.close() } catch (_: Exception) { }
            wsUnmark(sid, sess)
            return
        }
        runWsBridge(sid, sess, socket, ex.leftoverBytes())
    }

    fun handleSocketStarttls(ex: Exchange, q: Map<String, String>) {
        val (sid, sess) = sockLookup(ex, q) ?: return
        if (sess.wsAttached) {
            err(ex, 400, "Socket session is attached to a websocket (STARTTLS refused while attached).")
            return
        }
        synchronized(sess) {
            if (sess.kind == "tls") {
                err(ex, 400, "Socket session is already TLS.")
                return
            }
            if (sess.recvBusy) {
                err(ex, 400, "Another recv is already pending on this session.")
                return
            }
            sess.recvBusy = true
        }
        try {
            starttlsLocked(ex, sid, sess)
        } finally {
            synchronized(sess) { sess.recvBusy = false }
        }
    }

    fun starttlsLocked(ex: Exchange, sid: String, sess: SockSession) {
        val raw = readBody(ex)
        var obj = JsonObject()
        if (!raw.isEmpty()) {
            val ct = ex.requestHeaders.getFirst("Content-Type") ?: ""
            if (!ct.contains("application/json")) {
                err(ex, 400, "Content-Type must be application/json with body { sni?, ca_pem? }.")
                return
            }
            try {
                when (val v = com.google.gson.JsonParser.parseString(
                    raw.toString(StandardCharsets.UTF_8))) {
                    is JsonObject -> obj = v
                    else -> {
                        err(ex, 400, "JSON body must be an object.")
                        return
                    }
                }
            } catch (e: Exception) {
                err(ex, 400, "Invalid JSON body.")
                return
            }
        }
        val (sni0, caPem, identErr) = parseTlsIdentity(obj)
        if (identErr != null) {
            err(ex, 400, identErr)
            return
        }
        val sni = if (sni0.isEmpty()) sess.host else sni0
        val (ctx, ctxErr) = tlsContext(caPem)
        if (ctxErr != null) {
            err(ex, 400, ctxErr)
            return
        }
        val tlsSock = try {
            tlsWrap(sess.sock, sni, ctx!!,
                (SOCK_MAX_CONNECT_TIMEOUT * 1000).toInt(), false)
        } catch (e: Exception) {
            err(ex, 400, "TLS handshake with ${sess.host}:${sess.port}: ${e.message}")
            return
        }
        synchronized(sess) {
            if (SOCK_SESSIONS[sid] === sess) {
                sess.rawSock = sess.sock
                sess.sock = tlsSock
                sess.kind = "tls"
                sess.last = System.currentTimeMillis()
            } else {
                try { tlsSock.close() } catch (_: Exception) { }
            }
        }
        sendJson(ex, 200, jo("id" to sid, "kind" to "tls"))
    }

    // -- read endpoints ---------------------------------------------------

    fun handleReadDir(ex: Exchange, q: Map<String, String>) {
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (!Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 404, "Path does not exist", abs); return
        }
        if (!Files.isDirectory(p)) {
            err(ex, 400, "Path is not a directory", abs); return
        }
        val names: List<String> = try {
            File(abs).list()?.sorted() ?: run {
                err(ex, 403, "Cannot list directory", abs); return
            }
        } catch (e: SecurityException) {
            err(ex, 403, "Permission denied", abs); return
        }
        val entries = JsonArray()
        for (name in names) {
            try {
                val full = Paths.get(abs, name)
                val at = Files.readAttributes(full, BasicFileAttributes::class.java,
                    LinkOption.NOFOLLOW_LINKS)
                entries.add(jo("name" to name, "path" to full.toString(),
                    "type" to entryType(full), "size" to jenc(at.size()),
                    "atime" to jenc(fileTimeEpoch(at.lastAccessTime())),
                    "mtime" to jenc(fileTimeEpoch(at.lastModifiedTime())),
                    "ctime" to jenc(linuxCtime(full.toString())
                        ?: fileTimeEpoch(at.lastModifiedTime())),
                    "crtime" to jenc(fileTimeOrNull(at.creationTime())),
                    "mode" to modeString(full)))
            } catch (e: Exception) {
                continue
            }
        }
        sendJson(ex, 200, jo("path" to (q["path"] ?: ""), "absolute_path" to abs,
            "count" to jenc(entries.size()), "entries" to entries))
    }

    fun handleReadFile(ex: Exchange, q: Map<String, String>) {
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (!Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 404, "Path does not exist", abs); return
        }
        if (Files.isDirectory(p) && !Files.isSymbolicLink(p)) {
            err(ex, 400, "Path is a directory, not a file", abs); return
        }
        val encoding = (q["encoding"] ?: "auto").lowercase()
        if (encoding !in setOf("auto", "text", "base64")) {
            err(ex, 400, "Invalid encoding param: '$encoding' (use auto|text|base64)"); return
        }
        val rawMode = q["raw"] == "1"
        val data: ByteArray = try {
            Files.readAllBytes(p)
        } catch (e: AccessDeniedException) {
            err(ex, 403, "Permission denied", abs); return
        } catch (e: NoSuchFileException) {
            err(ex, 404, "Path does not exist", abs); return
        } catch (e: java.io.IOException) {
            if (Files.isDirectory(p)) {
                err(ex, 400, "Path is a directory, not a file", abs); return
            }
            err(ex, 403, "Cannot read file: ${e.message}", abs); return
        }
        var mtime: Double? = null
        try {
            mtime = fileTimeEpoch(Files.getLastModifiedTime(p, LinkOption.NOFOLLOW_LINKS))
        } catch (e: Exception) {
        }
        if (rawMode) {
            sendRaw(ex, 200, data, guessMime(abs))
            return
        }
        val mime = guessMime(abs)
        fun meta(binary: Boolean, enc: String, content: String): JsonObject =
            jo("path" to (q["path"] ?: ""), "absolute_path" to abs,
                "size" to jenc(data.size.toLong()), "mtime" to jenc(mtime),
                "mime_type" to mime, "is_binary" to jenc(binary), "encoding" to enc,
                "content" to content)
        when (encoding) {
            "text" -> sendJson(ex, 200, meta(false, "utf-8", data.toString(StandardCharsets.UTF_8)))
            "base64" -> sendJson(ex, 200, meta(true, "base64",
                Base64.getEncoder().encodeToString(data)))
            else -> {
                val text = decodeStrict(data)
                if (text != null && text.none { it.code < 32 && it != '\t' && it != '\n' && it != '\r' }) {
                    sendJson(ex, 200, meta(false, "utf-8", text))
                } else {
                    sendJson(ex, 200, meta(true, "base64",
                        Base64.getEncoder().encodeToString(data)))
                }
            }
        }
    }

    // -- write/delete endpoints --------------------------------------------

    fun parseWriteBody(ex: Exchange): ByteArray? {
        val body = readBody(ex)
        val ct = ex.requestHeaders.getFirst("Content-Type") ?: ""
        if (!ct.contains("application/json")) return body
        val obj = try {
            when (val v = com.google.gson.JsonParser.parseString(
                if (body.isEmpty()) "{}" else body.toString(StandardCharsets.UTF_8))) {
                is JsonObject -> v
                else -> {
                    err(ex, 400, "JSON body must be an object."); return null
                }
            }
        } catch (e: Exception) {
            err(ex, 400, "Invalid JSON body."); return null
        }
        val content = if (obj.has("content")) obj.get("content") else JsonNull.INSTANCE
        val encoding = if (obj.has("encoding")) {
            val e = obj.get("encoding")
            if (!e.isJsonPrimitive || !e.asJsonPrimitive.isString) {
                err(ex, 400, "Invalid encoding: (use text|base64)."); return null
            }
            e.asString.lowercase()
        } else "text"
        if (encoding == "text") {
            if (content.isJsonNull) return ByteArray(0)
            if (!content.isJsonPrimitive || !content.asJsonPrimitive.isString) {
                err(ex, 400, "'content' must be a string for text encoding."); return null
            }
            return content.asString.toByteArray(StandardCharsets.UTF_8)
        }
        if (encoding == "base64") {
            if (content.isJsonNull) return ByteArray(0)
            if (!content.isJsonPrimitive || !content.asJsonPrimitive.isString) {
                err(ex, 400, "'content' must be a base64 string."); return null
            }
            return try {
                Base64.getDecoder().decode(content.asString)
            } catch (e: IllegalArgumentException) {
                err(ex, 400, "Invalid base64 'content'."); return null
            }
        }
        err(ex, 400, "Invalid encoding: '$encoding' (use text|base64).")
        return null
    }

    fun handleCreateFile(ex: Exchange, q: Map<String, String>) {
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 409, "Path already exists (use PUT to overwrite).", abs); return
        }
        val parent = p.parent
        if (parent == null || !Files.isDirectory(parent)) {
            err(ex, 404, "Parent directory does not exist.", abs); return
        }
        val data = parseWriteBody(ex) ?: return
        try {
            Files.write(p, data, java.nio.file.StandardOpenOption.CREATE_NEW)
        } catch (e: FileAlreadyExistsException) {
            err(ex, 409, "Path already exists (use PUT to overwrite).", abs); return
        } catch (e: NoSuchFileException) {
            err(ex, 404, "Parent directory does not exist.", abs); return
        } catch (e: AccessDeniedException) {
            err(ex, 403, "Permission denied", abs); return
        } catch (e: java.io.IOException) {
            err(ex, 400, "Cannot create file: ${e.message}", abs); return
        }
        sendJson(ex, 201, jo("path" to (q["path"] ?: ""), "absolute_path" to abs,
            "size" to jenc(data.size.toLong()), "mime_type" to guessMime(abs)))
    }

    fun handleOverwriteFile(ex: Exchange, q: Map<String, String>) {
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (!Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 404, "Path does not exist", abs); return
        }
        if (Files.isDirectory(p) && !Files.isSymbolicLink(p)) {
            err(ex, 400, "Path is a directory, not a file", abs); return
        }
        val data = parseWriteBody(ex) ?: return
        try {
            Files.write(p, data, java.nio.file.StandardOpenOption.WRITE,
                java.nio.file.StandardOpenOption.TRUNCATE_EXISTING)
        } catch (e: AccessDeniedException) {
            err(ex, 403, "Permission denied", abs); return
        } catch (e: NoSuchFileException) {
            err(ex, 404, "Path does not exist", abs); return
        } catch (e: java.io.IOException) {
            if (Files.isDirectory(p)) {
                err(ex, 400, "Path is a directory, not a file", abs); return
            }
            err(ex, 400, "Cannot write file: ${e.message}", abs); return
        }
        sendJson(ex, 200, jo("path" to (q["path"] ?: ""), "absolute_path" to abs,
            "size" to jenc(data.size.toLong()), "mime_type" to guessMime(abs)))
    }

    fun handleDeleteFile(ex: Exchange, q: Map<String, String>) {
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (!Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 404, "Path does not exist", abs); return
        }
        if (Files.isDirectory(p) && !Files.isSymbolicLink(p)) {
            err(ex, 400, "Path is a directory, not a file (use DELETE /api/dir).", abs); return
        }
        try {
            Files.delete(p)
        } catch (e: NoSuchFileException) {
            err(ex, 404, "Path does not exist", abs); return
        } catch (e: AccessDeniedException) {
            err(ex, 403, "Permission denied", abs); return
        } catch (e: java.io.IOException) {
            err(ex, 400, "Cannot delete file: ${e.message}", abs); return
        }
        sendJson(ex, 200, jo("path" to (q["path"] ?: ""), "absolute_path" to abs, "deleted" to jenc(true)))
    }

    fun handleDeleteDir(ex: Exchange, q: Map<String, String>) {
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (!Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 404, "Path does not exist", abs); return
        }
        if (Files.isSymbolicLink(p) || !Files.isDirectory(p)) {
            err(ex, 400, "Path is not a directory", abs); return
        }
        try {
            Files.delete(p)
        } catch (e: NoSuchFileException) {
            err(ex, 404, "Path does not exist", abs); return
        } catch (e: AccessDeniedException) {
            err(ex, 403, "Permission denied", abs); return
        } catch (e: DirectoryNotEmptyException) {
            err(ex, 400, "Cannot delete directory: Directory not empty: '$abs'", abs); return
        } catch (e: java.io.IOException) {
            err(ex, 400, "Cannot delete directory: ${e.message}", abs); return
        }
        sendJson(ex, 200, jo("path" to (q["path"] ?: ""), "absolute_path" to abs, "deleted" to jenc(true)))
    }

    // -- shell --------------------------------------------------------------

    class CappedPipe(val stream: java.io.InputStream, val cap: Int) : Thread() {
        val buf = java.io.ByteArrayOutputStream()
        @Volatile var truncated = false
        override fun run() {
            val tmp = ByteArray(65536)
            try {
                while (true) {
                    val n = stream.read(tmp)
                    if (n < 0) break
                    val room = cap - buf.size()
                    if (room > 0) buf.write(tmp, 0, minOf(n, room))
                    if (n > maxOf(room, 0)) truncated = true
                }
            } catch (e: java.io.IOException) {
            } finally {
                try {
                    stream.close()
                } catch (_: Exception) {
                }
            }
        }
    }

    fun handleExecShell(ex: Exchange) {
        val obj = reqJson(ex) ?: return
        if (!obj.has("command") || !obj.get("command").isJsonPrimitive ||
            !obj.get("command").asJsonPrimitive.isString ||
            obj.get("command").asString.isBlank()
        ) {
            err(ex, 400, "Missing or empty required field: command."); return
        }
        val command = obj.get("command").asString
        val timeout: Double = if (!obj.has("timeout") || obj.get("timeout").isJsonNull) {
            SHELL_DEFAULT_TIMEOUT
        } else {
            val raw = obj.get("timeout")
            if (!raw.isJsonPrimitive || !raw.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."); return
            }
            val t = try {
                jdec(raw.asString)
            } catch (e: BadTag) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."); return
            }
            if (t !is Long && t !is Double) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."); return
            }
            (t as Number).toDouble().coerceIn(1.0, SHELL_MAX_TIMEOUT)
        }
        var cwd = CWD
        if (obj.has("cwd") && !(obj.get("cwd").isJsonNull)) {
            val c = obj.get("cwd")
            if (!c.isJsonPrimitive || !c.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'cwd' must be a string."); return
            }
            cwd = resolvePath(c.asString)
            if (!Files.isDirectory(Paths.get(cwd))) {
                err(ex, 400, "Working directory does not exist.", cwd); return
            }
        }
        fun limitOf(key: String): Int? {
            if (!obj.has(key) || obj.get(key).isJsonNull) {
                return SHELL_DEFAULT_MAX_OUTPUT
            }
            val raw = obj.get(key)
            if (!raw.isJsonPrimitive || !raw.asJsonPrimitive.isString) {
                err(ex, 400, "Field '$key' must be an integer >= 0 (i:<int>)."); return null
            }
            val v = try {
                jdec(raw.asString)
            } catch (e: BadTag) {
                err(ex, 400, "Field '$key' must be an integer >= 0 (i:<int>)."); return null
            }
            if (v !is Long || v < 0) {
                err(ex, 400, "Field '$key' must be an integer >= 0 (i:<int>)."); return null
            }
            if (v > SHELL_MAX_OUTPUT) {
                err(ex, 400, "Field '$key' exceeds the $SHELL_MAX_OUTPUT byte cap."); return null
            }
            return v.toInt()
        }
        val maxOut = limitOf("max_stdout") ?: return
        val maxErr = limitOf("max_stderr") ?: return
        val stdinRaw = if (!obj.has("stdin") || obj.get("stdin").isJsonNull) "" else {
            val s = obj.get("stdin")
            if (!s.isJsonPrimitive || !s.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'stdin' must be a string."); return
            }
            s.asString
        }
        var stdinEnc = "text"
        if (obj.has("stdin_encoding") && !obj.get("stdin_encoding").isJsonNull) {
            val s = obj.get("stdin_encoding")
            if (!s.isJsonPrimitive || !s.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'stdin_encoding' must be text|base64."); return
            }
            stdinEnc = s.asString.lowercase()
        }
        val stdinData: ByteArray = when (stdinEnc) {
            "text" -> stdinRaw.toByteArray(StandardCharsets.UTF_8)
            "base64" -> try {
                Base64.getDecoder().decode(stdinRaw)
            } catch (e: IllegalArgumentException) {
                err(ex, 400, "Invalid base64 'stdin'."); return
            }
            else -> {
                err(ex, 400, "Field 'stdin_encoding' must be text|base64."); return
            }
        }

        val proc: Process
        try {
            proc = ProcessBuilder("bash", "-c", command)
                .directory(File(cwd))
                .start()
        } catch (e: java.io.IOException) {
            err(ex, 400, "Cannot start shell: ${e.message}"); return
        }
        val outPipe = CappedPipe(proc.inputStream, maxOut)
        val errPipe = CappedPipe(proc.errorStream, maxErr)
        outPipe.start()
        errPipe.start()
        val feeder = Thread({
            try {
                proc.outputStream.write(stdinData)
            } catch (_: Exception) {
            } finally {
                try {
                    proc.outputStream.close()
                } catch (_: Exception) {
                }
            }
        })
        feeder.isDaemon = true
        feeder.start()
        val finished = proc.waitFor((timeout * 1e9).toLong(), TimeUnit.NANOSECONDS)
        val timedOut: Boolean
        val returncode: Int
        if (finished) {
            timedOut = false
            returncode = proc.exitValue()
        } else {
            try {
                proc.toHandle().descendants().forEach { it.destroyForcibly() }
            } catch (_: Exception) {
            }
            proc.destroyForcibly()
            try {
                proc.outputStream.close() // unblock the feeder thread
            } catch (_: Exception) {
            }
            proc.waitFor()
            timedOut = true
            returncode = 124
        }
        outPipe.join(5000)
        errPipe.join(5000)
        feeder.join(5000)
        sendJson(ex, 200, jo("command" to command, "cwd" to cwd,
            "returncode" to jenc(returncode.toLong()),
            "stdout" to outPipe.buf.toString(StandardCharsets.UTF_8),
            "stderr" to errPipe.buf.toString(StandardCharsets.UTF_8),
            "timed_out" to jenc(timedOut),
            "stdout_truncated" to jenc(outPipe.truncated),
            "stderr_truncated" to jenc(errPipe.truncated),
            "max_stdout" to jenc(maxOut.toLong()), "max_stderr" to jenc(maxErr.toLong()),
            "stdin_bytes" to jenc(stdinData.size.toLong())))
    }

    // -- sqlite ---------------------------------------------------------------

    fun sqliteConn(abs: String, timeoutSec: Double, readonly: Boolean): java.sql.Connection {
        val cfg = SQLiteConfig()
        cfg.setBusyTimeout((timeoutSec * 1000).toInt())
        if (readonly) cfg.setReadOnly(true)
        return java.sql.DriverManager.getConnection("jdbc:sqlite:$abs", cfg.toProperties())
    }

    fun handleSqliteCreate(ex: Exchange, q: Map<String, String>) {
        if (!sqliteAvailable) {
            err(ex, 503, "SQLite support unavailable: JDBC driver not loaded."); return
        }
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 409, "Database file already exists.", abs); return
        }
        val parent = p.parent
        if (parent == null || !Files.isDirectory(parent)) {
            err(ex, 404, "Parent directory does not exist.", abs); return
        }
        val version: String = try {
            sqliteConn(abs, 5.0, false).use { conn ->
                conn.createStatement().use { st ->
                    st.executeQuery("SELECT sqlite_version()").use { rs ->
                        rs.next()
                        rs.getString(1)
                    }
                }
            }
        } catch (e: java.sql.SQLException) {
            try {
                Files.deleteIfExists(p)
            } catch (_: Exception) {
            }
            err(ex, 400, "Cannot create database: ${e.message}", abs); return
        } catch (e: Exception) {
            err(ex, 403, "Cannot create database: ${e.message}", abs); return
        }
        val size = try {
            Files.size(p)
        } catch (e: Exception) {
            0L
        }
        sendJson(ex, 201, jo("path" to (q["path"] ?: ""), "absolute_path" to abs,
            "size" to jenc(size), "sqlite_version" to version))
    }

    fun handleSqliteQuery(ex: Exchange, q: Map<String, String>) {
        if (!sqliteAvailable) {
            err(ex, 503, "SQLite support unavailable: JDBC driver not loaded."); return
        }
        val abs = reqPath(ex, q) ?: return
        val p = Paths.get(abs)
        if (!Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
            err(ex, 404, "Database file does not exist", abs); return
        }
        val obj = reqJson(ex) ?: return
        if (!obj.has("sql") || !obj.get("sql").isJsonPrimitive ||
            !obj.get("sql").asJsonPrimitive.isString ||
            obj.get("sql").asString.isBlank()
        ) {
            err(ex, 400, "Missing or empty required field: sql."); return
        }
        val sql = obj.get("sql").asString
        val params: Any? = if (!obj.has("params") || obj.get("params").isJsonNull) {
            null
        } else {
            val raw = obj.get("params")
            when {
                raw.isJsonArray -> raw.asJsonArray.map { cell ->
                    if (!cell.isJsonPrimitive || !cell.asJsonPrimitive.isString) {
                        err(ex, 400, "Array params must be tagged scalars (i:/f:/s:/b:/null)."); return
                    }
                    try {
                        jdec(cell.asString)
                    } catch (e: BadTag) {
                        err(ex, 400, "Array params must be tagged scalars (i:/f:/s:/b:/null)."); return
                    }
                }
                raw.isJsonObject -> raw.asJsonObject.entrySet().associate { (k, cell) ->
                    if (!cell.isJsonPrimitive || !cell.asJsonPrimitive.isString) {
                        err(ex, 400, "Object params must be tagged scalars (i:/f:/s:/b:/null)."); return
                    }
                    k to try {
                        jdec(cell.asString)
                    } catch (e: BadTag) {
                        err(ex, 400, "Object params must be tagged scalars (i:/f:/s:/b:/null)."); return
                    }
                }
                else -> {
                    err(ex, 400, "Field 'params' must be an array or an object."); return
                }
            }
        }
        val timeout: Double = if (!obj.has("timeout") || obj.get("timeout").isJsonNull) {
            SQLITE_DEFAULT_TIMEOUT
        } else {
            val raw = obj.get("timeout")
            if (!raw.isJsonPrimitive || !raw.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."); return
            }
            val t = try {
                jdec(raw.asString)
            } catch (e: BadTag) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."); return
            }
            if (t !is Long && t !is Double) {
                err(ex, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."); return
            }
            val d = (t as Number).toDouble()
            if (!(d > 0 && d <= SQLITE_MAX_TIMEOUT)) {
                err(ex, 400, "Field 'timeout' must be within (0, 300]."); return
            }
            d
        }
        val readonly = if (!obj.has("readonly") || obj.get("readonly").isJsonNull) {
            false
        } else {
            val raw = obj.get("readonly")
            if (!raw.isJsonPrimitive || !raw.asJsonPrimitive.isString) {
                err(ex, 400, "Field 'readonly' must be b:true or b:false."); return
            }
            val r = try {
                jdec(raw.asString)
            } catch (e: BadTag) {
                err(ex, 400, "Field 'readonly' must be b:true or b:false."); return
            }
            if (r !is Boolean) {
                err(ex, 400, "Field 'readonly' must be b:true or b:false."); return
            }
            r
        }

        val scan: SqlScan
        try {
            scan = scanSql(sql)
        } catch (e: BadSql) {
            err(ex, 400, e.message ?: "Bad SQL.", abs); return
        }
        if (scan.statements.size != 1) {
            err(ex, 400, "Send exactly one SQL statement per request.", abs); return
        }
        val single = scan.statements[0]
        // Native binding: JDBC takes `?` only, so named placeholders are
        // rewritten to `?` (tokens only, never values); ? stays as-is.
        val jdbcSql: String
        val bindOrder: List<Any?>
        try {
            if (params is Map<*, *>) {
                @Suppress("UNCHECKED_CAST")
                val named = params as Map<String, Any?>
                if (named.isEmpty()) {
                    jdbcSql = single
                    bindOrder = listOf()
                } else {
                    val (rewritten, order) = rewriteNamed(single, named, listOf())
                    jdbcSql = rewritten
                    bindOrder = order.map { named[it] }
                }
            } else {
                @Suppress("UNCHECKED_CAST")
                val positional = (params as? List<Any?>) ?: listOf()
                ensureNoNamed(single)
                jdbcSql = single
                bindOrder = positional
            }
        } catch (e: BadSql) {
            err(ex, 400, e.message ?: "Bad params.", abs); return
        }
        val rowsMode = scan.firstWord in ROW_FIRST_WORDS || scan.hasReturning
        val payload: JsonObject
        try {
            sqliteConn(abs, timeout, readonly).use { conn ->
                if (rowsMode) {
                    conn.prepareStatement(jdbcSql).use { ps ->
                        bindOrder.forEachIndexed { idx, v -> bindJdbc(ps, idx + 1, v) }
                        ps.executeQuery().use { rs ->
                            val md = rs.metaData
                            val cols = JsonArray()
                            for (i in 1..md.columnCount) cols.add(md.getColumnLabel(i))
                            val rows = JsonArray()
                            while (rs.next()) {
                                val row = JsonArray()
                                for (i in 1..md.columnCount) {
                                    row.add(when (val v = rs.getObject(i)) {
                                        null -> "null"
                                        is Boolean -> jenc(v)
                                        is Long, is Int, is Short -> jenc((v as Number).toLong())
                                        is Double, is Float -> jenc((v as Number).toDouble())
                                        is ByteArray -> jtagStr(
                                            Base64.getEncoder().encodeToString(v))
                                        else -> jtagStr(v.toString())
                                    })
                                }
                                rows.add(row)
                            }
                            payload = jo("columns" to cols, "rows" to rows,
                                "rowcount" to jenc(rows.size().toLong()))
                        }
                    }
                } else {
                    conn.prepareStatement(jdbcSql).use { ps ->
                        bindOrder.forEachIndexed { idx, v -> bindJdbc(ps, idx + 1, v) }
                        val rowcount = ps.executeUpdate()
                        val lastrowid = conn.createStatement().use { st ->
                            st.executeQuery("SELECT last_insert_rowid()").use { rs ->
                                rs.next()
                                rs.getLong(1)
                            }
                        }
                        val rc = if (scan.firstWord in DDL_FIRST_WORDS) -1L else rowcount.toLong()
                        payload = jo("rowcount" to jenc(rc), "lastrowid" to jenc(lastrowid))
                    }
                }
            }
        } catch (e: java.sql.SQLException) {
            err(ex, 400, "SQLite error: ${e.message}", abs); return
        }
        payload.addProperty("path", q["path"] ?: "")
        payload.addProperty("absolute_path", abs)
        sendJson(ex, 200, payload)
    }

    // -- sysinfo ------------------------------------------------------------------

    fun readOsRelease(): Map<String, String> {
        val info = LinkedHashMap<String, String>()
        try {
            for (line in File("/etc/os-release").readLines(StandardCharsets.UTF_8)) {
                val t = line.trim()
                if (t.isEmpty() || t.startsWith("#") || '=' !in t) continue
                val eq = t.indexOf('=')
                info[t.substring(0, eq)] =
                    t.substring(eq + 1).trim().trim('"').trim('\'')
            }
        } catch (e: Exception) {
        }
        return info
    }

    fun handleSysinfo(ex: Exchange) {
        val osName = System.getProperty("os.name", "")
        val system = when {
            osName.startsWith("Windows") -> "Windows"
            osName.startsWith("Mac") -> "Darwin"
            else -> "Linux"
        }
        val systemName = if (system == "Darwin") "macOS" else system
        var distro: Any = "null"
        var windows: Any = "null"
        var mac: Any = "null"
        if (system == "Linux") {
            val rel = readOsRelease()
            if (rel.isNotEmpty()) {
                distro = jo("id" to (rel["ID"] ?: ""), "name" to (rel["NAME"] ?: ""),
                    "version" to (rel["VERSION"] ?: ""),
                    "version_id" to (rel["VERSION_ID"] ?: ""),
                    "pretty_name" to (rel["PRETTY_NAME"] ?: ""))
            }
        } else if (system == "Windows") {
            windows = jo("release" to osName, "version" to System.getProperty("os.version", ""),
                "service_pack" to "", "edition" to "")
        } else if (system == "Darwin") {
            mac = jo("version" to System.getProperty("os.version", ""))
        }
        val hostname = try {
            InetAddress.getLocalHost().hostName ?: ""
        } catch (e: Exception) {
            ""
        }
        sendJson(ex, 200, jo("system" to system, "system_name" to systemName,
            "release" to System.getProperty("os.version", ""),
            "version" to System.getProperty("os.version", ""),
            "machine" to System.getProperty("os.arch", ""),
            "processor" to "",
            "hostname" to hostname,
            "python_version" to "null",
            "jvm_version" to System.getProperty("java.version", ""),
            "kotlin_version" to KotlinVersion.CURRENT.toString(),
            "cpu_count" to jenc(Runtime.getRuntime().availableProcessors().toLong()),
            "distro" to distro, "windows" to windows, "mac" to mac))
    }
}
