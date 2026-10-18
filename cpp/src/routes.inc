// Route catalog (included by api.cpp).
namespace {

json param(const std::string& name, const std::string& location,
           bool required, const std::string& type,
           const std::string& desc, const std::string& dflt = "",
           bool has_default = false,
           const std::vector<std::string>& allowed = {}) {
    json p;
    p["name"] = name;
    p["location"] = location;
    p["required"] = required ? "b:true" : "b:false";
    p["type"] = type;
    p["description"] = desc;
    if (has_default) p["default"] = dflt;
    if (!allowed.empty()) {
        json arr = json::array();
        for (auto& a : allowed) arr.push_back(a);
        p["allowed"] = arr;
    }
    return p;
}

json route(const std::string& method, const std::string& url,
           const std::string& name, const std::string& desc,
           const json& params, const std::string& response,
           const std::string& errors = "") {
    json r;
    r["method"] = method;
    r["url"] = url;
    r["name"] = name;
    r["description"] = desc;
    r["params"] = params;
    r["response"] = response;
    if (!errors.empty()) r["errors"] = errors;
    return r;
}

json all_routes() {
    json routes = json::array();
    routes.push_back(route(
        "GET", "/api/help", "help",
        "Return the full list of implemented routes with URL, name, description and parameter formats. Wire convention: JSON carries only string/array/object - numbers, booleans and nulls travel as tagged strings (i:<int>, f:<float>, b:true|false, null). Fixed vocabulary and free text stay raw; sqlite row cells and params values are fully tagged (strings take an s: prefix there).",
        json::array(),
        "JSON object { version, count, routes: [...] }."));
    routes.push_back(route(
        "GET", "/api/dir?path=<path>", "read_directory",
        "List the contents of a directory.",
        json::array(
            {param("path", "query", true, "string",
                   "Absolute or relative path of the directory. Relative paths resolve against the server working directory. Paths starting with ~/ expand to the server user's home directory.")}),
        "JSON { path, absolute_path, count, entries: [{ name, path, type, size, atime, mtime, ctime, crtime, mode }] }. type is one of file|dir|symlink|other. Times are epoch seconds: atime (last access), mtime (last modification), ctime (last status change; creation on Windows), crtime (creation/birth, null where unavailable).",
        "400 missing path / not-a-directory, 404 not found, 403 forbidden."));
    routes.push_back(route(
        "GET",
        "/api/file?path=<path>[&encoding=text|base64|auto][&raw=1]",
        "read_file",
        "Read a file. Supports text and binary files. Default JSON mode auto-detects: UTF-8-decodable content is returned as text, otherwise as base64. With raw=1 the raw bytes are returned with a guessed Content-Type.",
        json::array(
            {param("path", "query", true, "string",
                   "Absolute or relative path of the file. Relative paths resolve against the server working directory; ~/ expands to the user's home directory."),
             param("encoding", "query", false, "string",
                   "auto: try UTF-8 text, fall back to base64. text: force UTF-8 decode with replacement of invalid sequences. base64: force base64.",
                   "auto", true, {"auto", "text", "base64"}),
             param("raw", "query", false, "string",
                   "If '1', respond with raw file bytes and a guessed Content-Type instead of JSON.",
                   "0", true, {"0", "1"})}),
        "JSON mode: { path, absolute_path, size, mtime, mime_type, is_binary, encoding, content }. Raw mode: file bytes with Content-Type + Content-Length headers.",
        "400 missing path / is-a-directory, 404 not found, 403 forbidden."));
    routes.push_back(route(
        "POST", "/api/file?path=<path>", "create_file",
        "Create a file, empty when no body is sent or with initial data (text or binary). Fails if the path already exists (use PUT to overwrite).",
        json::array(
            {param("path", "query", true, "string",
                   "Absolute or relative path of the file to create. ~/ expands to the server user's home directory."),
             param("content", "body (JSON)", false, "string",
                   "Initial content. Plain string for encoding=text, base64 string for encoding=base64. Omitted means empty file. Alternatively send raw bytes with a non-JSON Content-Type."),
             param("encoding", "body (JSON)", false, "string",
                   "How to interpret the JSON body content.", "text", true,
                   {"text", "base64"})}),
        "201 JSON { path, absolute_path, size, mime_type }.",
        "400 missing path / bad body, 404 parent directory missing, 409 already exists, 403 forbidden."));
    routes.push_back(route(
        "PUT", "/api/file?path=<path>", "overwrite_file",
        "Overwrite the content of an existing file (text or binary). Same body rules as create_file.",
        json::array(
            {param("path", "query", true, "string",
                   "Absolute or relative path of the file to overwrite. ~/ expands to the server user's home directory."),
             param("content", "body (JSON)", false, "string",
                   "New content (see create_file). Omitted truncates the file to zero bytes."),
             param("encoding", "body (JSON)", false, "string",
                   "How to interpret the JSON body content.", "text", true,
                   {"text", "base64"})}),
        "200 JSON { path, absolute_path, size, mime_type }.",
        "400 missing path / is-a-directory / bad body, 404 not found, 403 forbidden."));
    routes.push_back(route(
        "DELETE", "/api/file?path=<path>", "delete_file", "Delete a file.",
        json::array({param("path", "query", true, "string",
                           "Absolute or relative path of the file to delete. ~/ expands to the server user's home directory.")}),
        "200 JSON { path, absolute_path, deleted: true }.",
        "400 missing path / is-a-directory, 404 not found, 403 forbidden."));
    routes.push_back(route(
        "DELETE", "/api/dir?path=<path>", "delete_directory",
        "Delete an empty directory (rmdir equivalent). Fails when the directory is not empty.",
        json::array({param("path", "query", true, "string",
                           "Absolute or relative path of the empty directory to delete. ~/ expands to the server user's home directory.")}),
        "200 JSON { path, absolute_path, deleted: true }.",
        "400 missing path / not-a-directory / not-empty, 404 not found, 403 forbidden."));
    routes.push_back(route(
        "POST", "/api/shell", "exec_shell",
        "Execute a bash command string (pipes, redirections and all bash syntax allowed) and return the exit code plus separately captured stdout and stderr. WARNING: this is arbitrary remote code execution on the host; the server binds localhost by default and relies on that for protection.",
        json::array(
            {param("command", "body (JSON)", true, "string",
                   "Exact string as typed in bash, e.g. \"cat < a.txt | sort > b.txt\"."),
             param("timeout", "body (JSON)", false, "number",
                   "Seconds before the process group is killed (max 300). Timed-out calls return 124 with timed_out=true plus partial output.",
                   "i:30", true),
             param("cwd", "body (JSON)", false, "string",
                   "Working directory for the command (default: server working directory)."),
             param("max_stdout", "body (JSON)", false, "integer",
                   "Max captured stdout bytes sent back (1 MB default, 100 MB hard cap). The command runs fully; only the response is truncated.",
                   "i:1048576", true),
             param("max_stderr", "body (JSON)", false, "integer",
                   "Max captured stderr bytes sent back (same rules as max_stdout).",
                   "i:1048576", true),
             param("stdin", "body (JSON)", false, "string",
                   "Input piped to the command's stdin (as if from a previous pipe stage). Plain string for stdin_encoding=text, base64 for stdin_encoding=base64. Omitted means empty stdin (EOF at once)."),
             param("stdin_encoding", "body (JSON)", false, "string",
                   "How to interpret the stdin field.", "text", true,
                   {"text", "base64"})}),
        "200 JSON { command, cwd, returncode, stdout, stderr, timed_out, stdout_truncated, stderr_truncated, max_stdout, max_stderr, stdin_bytes }. returncode is the exit status of the last pipeline element (plain bash semantics). stdout/stderr are what the shell process emitted on its own fds, so in-command redirections apply: '> f' leaves captured stdout empty, '2>&1' merges into captured stdout. stdin_bytes is how many input bytes were fed.",
        "400 missing/empty command / bad JSON / bad timeout / bad cwd / bad or over-cap output limits, 405 wrong method."));
    routes.push_back(route(
        "POST", "/api/sqlite/create?path=<path>", "sqlite_create",
        "Create a new SQLite database file. The file is permanent; only the per-request connection is closed afterwards.",
        json::array({param("path", "query", true, "string",
                           "Full path of the database file to create.")}),
        "201 JSON { path, absolute_path, size, sqlite_version }.",
        "400 missing path, 404 parent directory missing, 409 file already exists, 403 forbidden."));
    routes.push_back(route(
        "POST", "/api/sqlite/query?path=<db-path>", "sqlite_query",
        "Execute one arbitrary SQL statement on a SQLite database. Stateless: a fresh connection is opened per request and closed right after. SELECT-style statements return rows; anything else is committed and returns the change counters.",
        json::array(
            {param("path", "query", true, "string",
                   "Path of the database file."),
             param("sql", "body (JSON)", true, "string",
                   "Exactly one SQL statement. Multi-statement strings are rejected (send one per request)."),
             param("params", "body (JSON)", false, "array | object",
                   "Bound parameters: array for ? placeholders, object for :named placeholders. Values must be tagged scalars (i:/f:/s:/b:/null); BLOB input is not supported.",
                   "[]", true),
             param("timeout", "body (JSON)", false, "number",
                   "Busy timeout in seconds while waiting on a locked database (max 300).",
                   "i:5", true),
             param("readonly", "body (JSON)", false, "boolean",
                   "Open the database read-only.", "b:false", true)}),
        "200 JSON, SELECT-style: { path, absolute_path, columns, rows, rowcount } with BLOB values as base64 strings. Writes: { path, absolute_path, rowcount, lastrowid }.",
        "400 missing path/sql, bad JSON/params/timeout, multi-statement, or the SQLite error text itself (e.g. no such table). 404 database file not found, 403 forbidden, 405 wrong method."));
    routes.push_back(route(
        "GET", "/api/sysinfo", "system_info",
        "Return operating-system information about the host running the server. Takes no parameters.",
        json::array(),
        "200 JSON { system, system_name, release, version, machine, processor, hostname, python_version, cpu_count, distro, windows, mac }. system is the raw platform name (Linux/Windows/Darwin); system_name is the friendly one (Darwin -> macOS). distro is parsed from /etc/os-release on Linux and null elsewhere; windows/mac objects appear only on those systems.",
        "401 missing/invalid token, 405 wrong method."));
    routes.push_back(route(
        "POST", "/api/socket/open", "socket_open",
        "Open a TCP connection to a network peer and hold it as a session, so browser pages (which cannot open raw sockets) can speak arbitrary TCP protocols through the local service (e.g. SMTP/IMAP for a browser email client). With tls:b:true the handshake runs immediately (implicit TLS, e.g. IMAPS on 993), verified against the system roots (+ optional ca_pem) with hostname check and SNI. Returns an opaque session id for send/recv/close/starttls. WARNING: this is an open network proxy on the host; the server binds localhost by default and relies on that for protection.",
        json::array(
            {param("host", "body (JSON)", true, "string",
                   "Peer hostname or IP address to connect to."),
             param("port", "body (JSON)", true, "integer",
                   "Peer TCP port, 1-65535 (i:<int>)."),
             param("timeout", "body (JSON)", false, "number",
                   "Connect timeout in seconds (max 30).", "i:5", true),
             param("tls", "body (JSON)", false, "boolean",
                   "Handshake TLS immediately and verify the peer (implicit TLS).",
                   "b:false", true),
             param("sni", "body (JSON)", false, "string",
                   "Server name for SNI and certificate verification (default: host)."),
             param("ca_pem", "body (JSON)", false, "string",
                   "Extra PEM CA bundle appended to the system roots (corporate intercepts, test PKIs).")}),
        "201 JSON { id, kind (tcp|tls), host, port }. The id is an opaque session token for the other socket routes.",
        "400 missing/invalid host/port/timeout/tls/sni/ca_pem, bad JSON, connect or TLS-handshake failure (engine error text), or too many open sessions. 405 wrong method."));
    routes.push_back(route(
        "POST", "/api/socket/send?id=<id>", "socket_send",
        "Write bytes to an open socket session (transparent: the peer receives exactly these bytes).",
        json::array(
            {param("id", "query", true, "string",
                   "Session id from socket_open."),
             param("content", "body (JSON)", false, "string",
                   "Bytes to write. Plain string for encoding=text, base64 string for encoding=base64. Alternatively send raw bytes with a non-JSON Content-Type."),
             param("encoding", "body (JSON)", false, "string",
                   "How to interpret the JSON body content.", "text", true,
                   {"text", "base64"})}),
        "200 JSON { id, bytes } with the written byte count.",
        "400 missing id / bad body / socket error, 404 unknown session, 405 wrong method."));
    routes.push_back(route(
        "GET", "/api/socket/recv?id=<id>[&timeout=][&max_bytes=]", "socket_recv",
        "Read bytes from an open socket session (transparent: whatever the peer sent). Long-polls up to timeout seconds; returns whatever arrived, an EOF flag when the peer closed, or a timeout flag when nothing arrived in time.",
        json::array(
            {param("id", "query", true, "string",
                   "Session id from socket_open."),
             param("timeout", "query", false, "string",
                   "Seconds to wait for data (max 60).", "5", true),
             param("max_bytes", "query", false, "string",
                   "Max bytes per call (1-1048576).", "65536", true)}),
        "200 JSON { id, encoding (base64), content, bytes, eof, timed_out }. content carries base64 bytes (may be empty); eof is true only with empty content after the peer closed; timed_out is true only with empty content when nothing arrived in time.",
        "400 missing id / bad timeout/max_bytes / socket error, 404 unknown session, 405 wrong method."));
    routes.push_back(route(
        "POST", "/api/socket/close?id=<id>", "socket_close",
        "Close an open socket session and release it. Idle sessions also expire automatically.",
        json::array({param("id", "query", true, "string",
                           "Session id from socket_open.")}),
        "200 JSON { id, closed: true }.",
        "400 missing id, 404 unknown session, 405 wrong method."));
    routes.push_back(route(
        "POST", "/api/socket/starttls?id=<id>", "socket_starttls",
        "Upgrade a plaintext socket session to TLS in place (STARTTLS-style protocols: drive the cleartext prelude over the session first, then call this before sending any more application bytes). Verified like implicit TLS (system roots + optional ca_pem, hostname check, SNI). A failed handshake leaves the plaintext session untouched; refused while a WebSocket owns the session, while a recv is pending, or when already TLS.",
        json::array(
            {param("id", "query", true, "string",
                   "Session id from socket_open."),
             param("sni", "body (JSON)", false, "string",
                   "Server name for SNI and certificate verification (default: the session host)."),
             param("ca_pem", "body (JSON)", false, "string",
                   "Extra PEM CA bundle appended to the system roots.")}),
        "200 JSON { id, kind (tls) }.",
        "400 missing id / bad body / already TLS / recv pending / attached / handshake failure (engine error text), 404 unknown session, 405 wrong method."));
    routes.push_back(route(
        "GET", "/api/socket/ws?id=<id>", "socket_ws",
        "Attach a WebSocket to an open socket session (the browser-native byte transport). Upgrade with `Upgrade: websocket` (token via `?token=`); after the 101 handshake, WS message payloads flow transparently client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back as binary frames; ping gets an auto-pong. A close from either side, or any TCP/protocol error, ends the whole session (a WS close is a session close, like HTTP close). HTTP send/recv are refused while attached; HTTP close kills the bridge too.",
        json::array({param("id", "query", true, "string",
                           "Session id from socket_open.")}),
        "101 Switching Protocols, then transparent bidirectional bytes (see description).",
        "400 not-an-upgrade / bad handshake / missing id / already attached, 404 unknown session, 405 wrong method."));
    routes.push_back(route(
        "GET", "/", "index",
        "Service index with version and pointer to /api/help.",
        json::array(), "JSON { service, version, help }."));
    return routes;
}

}  // namespace
