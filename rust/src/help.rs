//! Route catalog for /api/help (single source of truth, mirrors the others).

use crate::api::jstr;

fn param_full(
    name: &str,
    location: &str,
    required: bool,
    typ: &str,
    desc: &str,
    default: Option<&str>,
    allowed: Option<&[&str]>,
) -> serde_json::Value {
    let mut o = serde_json::Map::new();
    o.insert("name".to_string(), jstr(name));
    o.insert("location".to_string(), jstr(location));
    o.insert(
        "required".to_string(),
        jstr(if required { "b:true" } else { "b:false" }),
    );
    o.insert("type".to_string(), jstr(typ));
    o.insert("description".to_string(), jstr(desc));
    if let Some(d) = default {
        o.insert("default".to_string(), jstr(d));
    }
    if let Some(a) = allowed {
        o.insert(
            "allowed".to_string(),
            serde_json::Value::Array(a.iter().map(|s| jstr(s)).collect()),
        );
    }
    serde_json::Value::Object(o)
}

macro_rules! route {
    ($m:expr, $u:expr, $n:expr, $d:expr, $p:expr, $r:expr) => {
        route_full($m, $u, $n, $d, $p, $r, None)
    };
    ($m:expr, $u:expr, $n:expr, $d:expr, $p:expr, $r:expr, $e:expr) => {
        route_full($m, $u, $n, $d, $p, $r, $e)
    };
}

fn route_full(
    method: &str,
    url: &str,
    name: &str,
    desc: &str,
    params: Vec<serde_json::Value>,
    response: &str,
    errors: Option<&str>,
) -> serde_json::Value {
    let mut o = serde_json::Map::new();
    o.insert("method".to_string(), jstr(method));
    o.insert("url".to_string(), jstr(url));
    o.insert("name".to_string(), jstr(name));
    o.insert("description".to_string(), jstr(desc));
    o.insert("params".to_string(), serde_json::Value::Array(params));
    o.insert("response".to_string(), jstr(response));
    if let Some(e) = errors {
        o.insert("errors".to_string(), jstr(e));
    }
    serde_json::Value::Object(o)
}

macro_rules! param {
    ($n:expr, $l:expr, $r:expr, $t:expr, $d:expr) => {
        param_full($n, $l, $r, $t, $d, None, None)
    };
    ($n:expr, $l:expr, $r:expr, $t:expr, $d:expr, $def:expr) => {
        param_full($n, $l, $r, $t, $d, $def, None)
    };
    ($n:expr, $l:expr, $r:expr, $t:expr, $d:expr, $def:expr, $al:expr) => {
        param_full($n, $l, $r, $t, $d, $def, $al)
    };
}

pub fn routes() -> Vec<serde_json::Value> {
    vec![
        route!("GET", "/api/help", "help",
            "Return the full list of implemented routes with URL, name, description and parameter formats. Wire convention: JSON carries only string/array/object - numbers, booleans and nulls travel as tagged strings (i:<int>, f:<float>, b:true|false, null). Fixed vocabulary and free text stay raw; sqlite row cells and params values are fully tagged (strings take an s: prefix there).",
            vec![], "JSON object { version, count, routes: [...] }."),
        route!("GET", "/api/dir?path=<path>", "read_directory",
            "List the contents of a directory.",
            vec![param!("path", "query", true, "string",
                "Absolute or relative path of the directory. Relative paths resolve against the server working directory. Paths starting with ~/ expand to the server user's home directory.")],
            "JSON { path, absolute_path, count, entries: [{ name, path, type, size, atime, mtime, ctime, crtime, mode }] }. type is one of file|dir|symlink|other. Times are epoch seconds: atime (last access), mtime (last modification), ctime (last status change; creation on Windows), crtime (creation/birth, null where unavailable).",
            Some("400 missing path / not-a-directory, 404 not found, 403 forbidden.")),
        route!("GET", "/api/file?path=<path>[&encoding=text|base64|auto][&raw=1]", "read_file",
            "Read a file. Supports text and binary files. Default JSON mode auto-detects: UTF-8-decodable content is returned as text, otherwise as base64. With raw=1 the raw bytes are returned with a guessed Content-Type.",
            vec![param!("path", "query", true, "string",
                "Absolute or relative path of the file. Relative paths resolve against the server working directory; ~/ expands to the user's home directory."),
                param!("encoding", "query", false, "string",
                    "auto: try UTF-8 text, fall back to base64. text: force UTF-8 decode with replacement of invalid sequences. base64: force base64.",
                    Some("auto"), Some(&["auto", "text", "base64"])),
                param!("raw", "query", false, "string",
                    "If '1', respond with raw file bytes and a guessed Content-Type instead of JSON.",
                    Some("0"), Some(&["0", "1"]))],
            "JSON mode: { path, absolute_path, size, mtime, mime_type, is_binary, encoding, content }. Raw mode: file bytes with Content-Type + Content-Length headers.",
            Some("400 missing path / is-a-directory, 404 not found, 403 forbidden.")),
        route!("POST", "/api/file?path=<path>", "create_file",
            "Create a file, empty when no body is sent or with initial data (text or binary). Fails if the path already exists (use PUT to overwrite).",
            vec![param!("path", "query", true, "string",
                "Absolute or relative path of the file to create. ~/ expands to the server user's home directory."),
                param!("content", "body (JSON)", false, "string",
                    "Initial content. Plain string for encoding=text, base64 string for encoding=base64. Omitted means empty file. Alternatively send raw bytes with a non-JSON Content-Type."),
                param!("encoding", "body (JSON)", false, "string",
                    "How to interpret the JSON body content.", Some("text"), Some(&["text", "base64"]))],
            "201 JSON { path, absolute_path, size, mime_type }.",
            Some("400 missing path / bad body, 404 parent directory missing, 409 already exists, 403 forbidden.")),
        route!("PUT", "/api/file?path=<path>", "overwrite_file",
            "Overwrite the content of an existing file (text or binary). Same body rules as create_file.",
            vec![param!("path", "query", true, "string",
                "Absolute or relative path of the file to overwrite. ~/ expands to the server user's home directory."),
                param!("content", "body (JSON)", false, "string",
                    "New content (see create_file). Omitted truncates the file to zero bytes."),
                param!("encoding", "body (JSON)", false, "string",
                    "How to interpret the JSON body content.", Some("text"), Some(&["text", "base64"]))],
            "200 JSON { path, absolute_path, size, mime_type }.",
            Some("400 missing path / is-a-directory / bad body, 404 not found, 403 forbidden.")),
        route!("DELETE", "/api/file?path=<path>", "delete_file",
            "Delete a file.",
            vec![param!("path", "query", true, "string",
                "Absolute or relative path of the file to delete. ~/ expands to the server user's home directory.")],
            "200 JSON { path, absolute_path, deleted: true }.",
            Some("400 missing path / is-a-directory, 404 not found, 403 forbidden.")),
        route!("DELETE", "/api/dir?path=<path>", "delete_directory",
            "Delete an empty directory (rmdir equivalent). Fails when the directory is not empty.",
            vec![param!("path", "query", true, "string",
                "Absolute or relative path of the empty directory to delete. ~/ expands to the server user's home directory.")],
            "200 JSON { path, absolute_path, deleted: true }.",
            Some("400 missing path / not-a-directory / not-empty, 404 not found, 403 forbidden.")),
        route!("DELETE", "/api/dir/shallow?path=<path>", "delete_directory_shallow",
            "Delete a shallow directory: a directory containing only files (including symlinks) and empty subdirectories. Fails when it contains a non-empty real subdirectory. Does not delete recursively.",
            vec![param!("path", "query", true, "string",
                "Absolute or relative path of the shallow directory to delete. ~/ expands to the server user's home directory.")],
            "200 JSON { path, absolute_path, deleted: true }.",
            Some("400 missing path / not-a-directory / not-shallow, 404 not found, 403 forbidden.")),
        route!("POST", "/api/shell", "exec_shell",
            "Execute a bash command string (pipes, redirections and all bash syntax allowed) and return the exit code plus separately captured stdout and stderr. WARNING: this is arbitrary remote code execution on the host; the server binds localhost by default and relies on that for protection.",
            vec![param!("command", "body (JSON)", true, "string",
                "Exact string as typed in bash, e.g. \"cat < a.txt | sort > b.txt\"."),
                param!("timeout", "body (JSON)", false, "number",
                    "Seconds before the process group is killed (max 300). Timed-out calls return 124 with timed_out=true plus partial output.",
                    Some("i:30"), None),
                param!("cwd", "body (JSON)", false, "string",
                    "Working directory for the command (default: server working directory)."),
                param!("max_stdout", "body (JSON)", false, "integer",
                    "Max captured stdout bytes sent back (1 MB default, 100 MB hard cap). The command runs fully; only the response is truncated.",
                    Some("i:1048576"), None),
                param!("max_stderr", "body (JSON)", false, "integer",
                    "Max captured stderr bytes sent back (same rules as max_stdout).",
                    Some("i:1048576"), None),
                param!("stdin", "body (JSON)", false, "string",
                    "Input piped to the command's stdin (as if from a previous pipe stage). Plain string for stdin_encoding=text, base64 for stdin_encoding=base64. Omitted means empty stdin (EOF at once)."),
                param!("stdin_encoding", "body (JSON)", false, "string",
                    "How to interpret the stdin field.",
                    Some("text"), Some(&["text", "base64"]))],
            "200 JSON { command, cwd, returncode, stdout, stderr, timed_out, stdout_truncated, stderr_truncated, max_stdout, max_stderr, stdin_bytes }. returncode is the exit status of the last pipeline element (plain bash semantics). stdout/stderr are what the shell process emitted on its own fds, so in-command redirections apply: '> f' leaves captured stdout empty, '2>&1' merges into captured stdout. stdin_bytes is how many input bytes were fed.",
            Some("400 missing/empty command / bad JSON / bad timeout / bad cwd / bad or over-cap output limits, 405 wrong method.")),
        route!("POST", "/api/sqlite/create?path=<path>", "sqlite_create",
            "Create a new SQLite database file. The file is permanent; only the per-request connection is closed afterwards.",
            vec![param!("path", "query", true, "string", "Full path of the database file to create.")],
            "201 JSON { path, absolute_path, size, sqlite_version }.",
            Some("400 missing path, 404 parent directory missing, 409 file already exists, 403 forbidden.")),
        route!("POST", "/api/sqlite/query?path=<db-path>", "sqlite_query",
            "Execute one arbitrary SQL statement on a SQLite database. Stateless: a fresh connection is opened per request and closed right after. SELECT-style statements return rows; anything else is committed and returns the change counters.",
            vec![param!("path", "query", true, "string", "Path of the database file."),
                param!("sql", "body (JSON)", true, "string",
                    "Exactly one SQL statement. Multi-statement strings are rejected (send one per request)."),
                param!("params", "body (JSON)", false, "array | object",
                    "Bound parameters: array for ? placeholders, object for :named placeholders. Values must be tagged scalars (i:/f:/s:/b:/null); BLOB input is not supported.",
                    Some("[]"), None),
                param!("timeout", "body (JSON)", false, "number",
                    "Busy timeout in seconds while waiting on a locked database (max 300).", Some("i:5"), None),
                param!("readonly", "body (JSON)", false, "boolean",
                    "Open the database read-only.", Some("b:false"), None)],
            "200 JSON, SELECT-style: { path, absolute_path, columns, rows, rowcount } with BLOB values as base64 strings. Writes: { path, absolute_path, rowcount, lastrowid }.",
            Some("400 missing path/sql, bad JSON/params/timeout, multi-statement, or the SQLite error text itself (e.g. no such table). 404 database file not found, 403 forbidden, 405 wrong method.")),
        route!("GET", "/api/sysinfo", "system_info",
            "Return operating-system information about the host running the server. Takes no parameters.",
            vec![],
            "200 JSON { system, system_name, release, version, machine, processor, hostname, python_version, cpu_count, distro, windows, mac }. system is the raw platform name (Linux/Windows/Darwin); system_name is the friendly one (Darwin -> macOS). distro is parsed from /etc/os-release on Linux and null elsewhere; windows/mac objects appear only on those systems.",
            Some("401 missing/invalid token, 405 wrong method.")),
        route!("POST", "/api/socket/open", "socket_open",
            "Open a TCP connection to a network peer and hold it as a session, so browser pages (which cannot open raw sockets) can speak arbitrary TCP protocols through the local service (e.g. SMTP/IMAP for a browser email client). With tls:b:true the handshake runs immediately (implicit TLS, e.g. IMAPS on 993), verified against the system roots (+ optional ca_pem) with hostname check and SNI. Returns an opaque session id for send/recv/close/starttls. WARNING: this is an open network proxy on the host; the server binds localhost by default and relies on that for protection.",
            vec![param!("host", "body (JSON)", true, "string",
                "Peer hostname or IP address to connect to."),
                param!("port", "body (JSON)", true, "integer",
                    "Peer TCP port, 1-65535 (i:<int>)."),
                param!("timeout", "body (JSON)", false, "number",
                    "Connect timeout in seconds (max 30).", Some("i:5"), None),
                param!("tls", "body (JSON)", false, "boolean",
                    "Handshake TLS immediately and verify the peer (implicit TLS).", Some("b:false"), None),
                param!("sni", "body (JSON)", false, "string",
                    "Server name for SNI and certificate verification (default: host).", None, None),
                param!("ca_pem", "body (JSON)", false, "string",
                    "Extra PEM CA bundle appended to the system roots (corporate intercepts, test PKIs).", None, None)],
            "201 JSON { id, kind (tcp|tls), host, port }. The id is an opaque session token for the other socket routes.",
            Some("400 missing/invalid host/port/timeout/tls/sni/ca_pem, bad JSON, connect or TLS-handshake failure (engine error text), or too many open sessions. 405 wrong method.")),
        route!("POST", "/api/socket/send?id=<id>", "socket_send",
            "Write bytes to an open socket session (transparent: the peer receives exactly these bytes).",
            vec![param!("id", "query", true, "string",
                "Session id from socket_open."),
                param!("content", "body (JSON)", false, "string",
                    "Bytes to write. Plain string for encoding=text, base64 string for encoding=base64. Alternatively send raw bytes with a non-JSON Content-Type."),
                param!("encoding", "body (JSON)", false, "string",
                    "How to interpret the JSON body content.", Some("text"), Some(&["text", "base64"]))],
            "200 JSON { id, bytes } with the written byte count.",
            Some("400 missing id / bad body / socket error, 404 unknown session, 405 wrong method.")),
        route!("GET", "/api/socket/recv?id=<id>[&timeout=][&max_bytes=]", "socket_recv",
            "Read bytes from an open socket session (transparent: whatever the peer sent). Long-polls up to timeout seconds; returns whatever arrived, an EOF flag when the peer closed, or a timeout flag when nothing arrived in time.",
            vec![param!("id", "query", true, "string",
                "Session id from socket_open."),
                param!("timeout", "query", false, "string",
                    "Seconds to wait for data (max 60).", Some("5"), None),
                param!("max_bytes", "query", false, "string",
                    "Max bytes per call (1-1048576).", Some("65536"), None)],
            "200 JSON { id, encoding (base64), content, bytes, eof, timed_out }. content carries base64 bytes (may be empty); eof is true only with empty content after the peer closed; timed_out is true only with empty content when nothing arrived in time.",
            Some("400 missing id / bad timeout/max_bytes / socket error, 404 unknown session, 405 wrong method.")),
        route!("POST", "/api/socket/close?id=<id>", "socket_close",
            "Close an open socket session and release it. Idle sessions also expire automatically.",
            vec![param!("id", "query", true, "string",
                "Session id from socket_open.")],
            "200 JSON { id, closed: true }.",
            Some("400 missing id, 404 unknown session, 405 wrong method.")),
        route!("POST", "/api/socket/starttls?id=<id>", "socket_starttls",
            "Upgrade a plaintext socket session to TLS in place (STARTTLS-style protocols: drive the cleartext prelude over the session first, then call this before sending any more application bytes). Verified like implicit TLS (system roots + optional ca_pem, hostname check, SNI). A failed handshake leaves the plaintext session untouched; refused while a WebSocket owns the session, while a recv is pending, or when already TLS.",
            vec![param!("id", "query", true, "string",
                "Session id from socket_open."),
                param!("sni", "body (JSON)", false, "string",
                    "Server name for SNI and certificate verification (default: the session host).", None, None),
                param!("ca_pem", "body (JSON)", false, "string",
                    "Extra PEM CA bundle appended to the system roots.", None, None)],
            "200 JSON { id, kind (tls) }.",
            Some("400 missing id / bad body / already TLS / recv pending / attached / handshake failure (engine error text), 404 unknown session, 405 wrong method.")),
        route!("GET", "/api/socket/ws?id=<id>", "socket_ws",
            "Attach a WebSocket to an open socket session (the browser-native byte transport). Upgrade with `Upgrade: websocket` (token via `?token=`); after the 101 handshake, WS message payloads flow transparently client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back as binary frames; ping gets an auto-pong. A close from either side, or any TCP/protocol error, ends the whole session (a WS close is a session close, like HTTP close). HTTP send/recv are refused while attached; HTTP close kills the bridge too.",
            vec![param!("id", "query", true, "string",
                "Session id from socket_open.")],
            "101 Switching Protocols, then transparent bidirectional bytes (see description).",
            Some("400 not-an-upgrade / bad handshake / missing id / already attached, 404 unknown session, 405 wrong method.")),
        route!("GET", "/", "index",
            "Service index with version and pointer to /api/help.",
            vec![], "JSON { service, version, help }."),
    ]
}
