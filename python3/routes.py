"""Route registry (single source of truth for /api/help).
Paths that never require the API token."""

OPEN_ROUTES = ("/", "/api/help")


ROUTES = [
    {
        "method": "GET",
        "url": "/api/help",
        "name": "help",
        "description": "Return the full list of implemented routes with URL, "
                       "name, description and parameter formats. Wire "
                       "convention: JSON carries only string/array/object - "
                       "numbers, booleans and nulls travel as tagged "
                       "strings (i:<int>, f:<float>, b:true|false, null). "
                       "Fixed vocabulary and free text stay raw; sqlite "
                       "row cells and params values are fully tagged "
                       "(strings take an s: prefix there).",
        "params": [],
        "response": "JSON object { version, count, routes: [...] }.",
    },
    {
        "method": "GET",
        "url": "/api/dir?path=<path>",
        "name": "read_directory",
        "description": "List the contents of a directory.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Absolute or relative path of the directory. "
                               "Relative paths resolve against the server "
                               "working directory. Paths starting with ~/ "
                               "expand to the server user's home directory.",
            }
        ],
        "response": "JSON { path, absolute_path, count, entries: ["
                    "{ name, path, type, size, atime, mtime, ctime, crtime, "
                    "mode } ] }. "
                    "type is one of file|dir|symlink|other. Times are "
                    "epoch seconds: atime (last access), mtime (last "
                    "modification), ctime (last status change; creation "
                    "on Windows), crtime (creation/birth: st_birthtime "
                    "where exposed, else a Linux statx lookup, else null "
                    "where neither the filesystem nor the kernel "
                    "provides it).",
        "errors": "400 missing path / not-a-directory, 404 not found, "
                  "403 forbidden.",
    },
    {
        "method": "GET",
        "url": "/api/file?path=<path>[&encoding=text|base64|auto][&raw=1]",
        "name": "read_file",
        "description": "Read a file. Supports text and binary files. "
                       "Default JSON mode auto-detects: UTF-8-decodable "
                       "content is returned as text, otherwise as base64. "
                       "With raw=1 the raw bytes are returned with a guessed "
                       "Content-Type.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Absolute or relative path of the file. "
                               "Relative paths resolve against the server "
                               "working directory; ~/ expands to the user's "
                               "home directory.",
            },
            {
                "name": "encoding",
                "location": "query",
                "required": "b:false",
                "type": "string",
                "default": "auto",
                "allowed": ["auto", "text", "base64"],
                "description": "auto: try UTF-8 text, fall back to base64. "
                               "text: force UTF-8 decode with replacement of "
                               "invalid sequences. base64: force base64.",
            },
            {
                "name": "raw",
                "location": "query",
                "required": "b:false",
                "type": "string",
                "default": "0",
                "allowed": ["0", "1"],
                "description": "If '1', respond with raw file bytes and a "
                               "guessed Content-Type instead of JSON.",
            },
        ],
        "response": "JSON mode: { path, absolute_path, size, mtime, "
                    "mime_type, is_binary, encoding, content }. "
                    "Raw mode: file bytes with Content-Type + "
                    "Content-Length headers.",
        "errors": "400 missing path / is-a-directory, 404 not found, "
                  "403 forbidden.",
    },
    {
        "method": "GET",
        "url": "/",
        "name": "index",
        "description": "Service index with version and pointer to /api/help.",
        "params": [],
        "response": "JSON { service, version, help }.",
    },
    {
        "method": "POST",
        "url": "/api/file?path=<path>",
        "name": "create_file",
        "description": "Create a file, empty when no body is sent or with "
                       "initial data (text or binary). Fails if the path "
                       "already exists (use PUT to overwrite).",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Absolute or relative path of the file to create. "
                               "~/ expands to the server user's home "
                               "directory.",
            },
            {
                "name": "content",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Initial content. Plain string for "
                               "encoding=text, base64 string for "
                               "encoding=base64. Omitted means empty file. "
                               "Alternatively send raw bytes with a non-JSON "
                               "Content-Type.",
            },
            {
                "name": "encoding",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "default": "text",
                "allowed": ["text", "base64"],
                "description": "How to interpret the JSON body content.",
            },
        ],
        "response": "201 JSON { path, absolute_path, size, mime_type }.",
        "errors": "400 missing path / bad body, 404 parent directory "
                  "missing, 409 already exists, 403 forbidden.",
    },
    {
        "method": "PUT",
        "url": "/api/file?path=<path>",
        "name": "overwrite_file",
        "description": "Overwrite the content of an existing file "
                       "(text or binary). Same body rules as create_file.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Absolute or relative path of the file to overwrite. "
                               "~/ expands to the server user's home "
                               "directory.",
            },
            {
                "name": "content",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "New content (see create_file). Omitted "
                               "truncates the file to zero bytes.",
            },
            {
                "name": "encoding",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "default": "text",
                "allowed": ["text", "base64"],
                "description": "How to interpret the JSON body content.",
            },
        ],
        "response": "200 JSON { path, absolute_path, size, mime_type }.",
        "errors": "400 missing path / is-a-directory / bad body, "
                  "404 not found, 403 forbidden.",
    },
    {
        "method": "DELETE",
        "url": "/api/file?path=<path>",
        "name": "delete_file",
        "description": "Delete a file.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Absolute or relative path of the file to delete. "
                               "~/ expands to the server user's home "
                               "directory.",
            }
        ],
        "response": "200 JSON { path, absolute_path, deleted: true }.",
        "errors": "400 missing path / is-a-directory, 404 not found, "
                  "403 forbidden.",
    },
    {
        "method": "DELETE",
        "url": "/api/dir?path=<path>",
        "name": "delete_directory",
        "description": "Delete an empty directory (rmdir equivalent). "
                       "Fails when the directory is not empty.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Absolute or relative path of the empty "
                               "directory to delete. ~/ expands to the "
                               "server user's home directory.",
            }
        ],
        "response": "200 JSON { path, absolute_path, deleted: true }.",
        "errors": "400 missing path / not-a-directory / not-empty, "
                  "404 not found, 403 forbidden.",
    },
    {
        "method": "DELETE",
        "url": "/api/dir/shallow?path=<path>",
        "name": "delete_directory_shallow",
        "description": "Delete a shallow directory: a directory containing "
                       "only files (including symlinks) and empty "
                       "subdirectories. Fails when it contains a non-empty "
                       "real subdirectory. Does not delete recursively.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Absolute or relative path of the shallow "
                               "directory to delete. ~/ expands to the "
                               "server user's home directory.",
            }
        ],
        "response": "200 JSON { path, absolute_path, deleted: true }.",
        "errors": "400 missing path / not-a-directory / not-shallow, "
                  "404 not found, 403 forbidden.",
    },
    {
        "method": "POST",
        "url": "/api/shell",
        "name": "exec_shell",
        "description": "Execute a bash command string (pipes, redirections "
                       "and all bash syntax allowed) and return the exit "
                       "code plus separately captured stdout and stderr. "
                       "WARNING: this is arbitrary remote code execution on "
                       "the host; the server binds localhost by default and "
                       "relies on that for protection.",
        "params": [
            {
                "name": "command",
                "location": "body (JSON)",
                "required": "b:true",
                "type": "string",
                "description": "Exact string as typed in bash, e.g. "
                               "\"cat < a.txt | sort > b.txt\".",
            },
            {
                "name": "timeout",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "number",
                "default": "i:30",
                "description": "Seconds before the process group is killed "
                               "(max 300). Timed-out calls return 124 with "
                               "timed_out=true plus partial output.",
            },
            {
                "name": "cwd",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Working directory for the command "
                               "(default: server working directory).",
            },
            {
                "name": "max_stdout",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "integer",
                "default": "i:1048576",
                "description": "Max captured stdout bytes sent back (1 MB "
                               "default, 100 MB hard cap). The command runs "
                               "fully; only the response is truncated.",
            },
            {
                "name": "max_stderr",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "integer",
                "default": "i:1048576",
                "description": "Max captured stderr bytes sent back "
                               "(same rules as max_stdout).",
            },
            {
                "name": "stdin",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Input piped to the command's stdin "
                               "(as if from a previous pipe stage). Plain "
                               "string for stdin_encoding=text, base64 for "
                               "stdin_encoding=base64. Omitted means empty "
                               "stdin (EOF at once).",
            },
            {
                "name": "stdin_encoding",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "default": "text",
                "allowed": ["text", "base64"],
                "description": "How to interpret the stdin field.",
            },
        ],
        "response": "200 JSON { command, cwd, returncode, stdout, stderr, "
                    "timed_out, stdout_truncated, stderr_truncated, "
                    "max_stdout, max_stderr, stdin_bytes }. returncode is the exit status "
                    "of the last pipeline element (plain bash semantics). stdout/stderr "
                    "are what the shell process emitted on its own fds, so "
                    "in-command redirections apply: '> f' leaves captured "
                    "stdout empty, '2>&1' merges into captured stdout. "
                    "stdin_bytes is how many input bytes were fed.",
        "errors": "400 missing/empty command / bad JSON / bad timeout / "
                  "bad cwd / bad or over-cap output limits, 405 wrong method.",
    },
    {
        "method": "POST",
        "url": "/api/sqlite/create?path=<path>",
        "name": "sqlite_create",
        "description": "Create a new SQLite database file. The file is "
                       "permanent; only the per-request connection is "
                       "closed afterwards.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Full path of the database file to create.",
            }
        ],
        "response": "201 JSON { path, absolute_path, size, sqlite_version }.",
        "errors": "400 missing path, 404 parent directory missing, "
                  "409 file already exists, 403 forbidden.",
    },
    {
        "method": "POST",
        "url": "/api/sqlite/query?path=<db-path>",
        "name": "sqlite_query",
        "description": "Execute one arbitrary SQL statement on a SQLite "
                       "database. Stateless: a fresh connection is opened "
                       "per request and closed right after. SELECT-style "
                       "statements return rows; anything else is committed "
                       "and returns the change counters.",
        "params": [
            {
                "name": "path",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Path of the database file.",
            },
            {
                "name": "sql",
                "location": "body (JSON)",
                "required": "b:true",
                "type": "string",
                "description": "Exactly one SQL statement. Multi-statement "
                               "strings are rejected (send one per request).",
            },
            {
                "name": "params",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "array | object",
                "default": [],
                "description": "Bound parameters: array for ? placeholders, "
                               "object for :named placeholders. Values must "
                               "be JSON scalars (BLOB input is not "
                               "supported).",
            },
            {
                "name": "timeout",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "number",
                "default": "i:5",
                "description": "Busy timeout in seconds while waiting on a "
                               "locked database (max 300).",
            },
            {
                "name": "readonly",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "boolean",
                "default": "b:false",
                "description": "Open the database read-only.",
            },
        ],
        "response": "200 JSON, SELECT-style: { path, absolute_path, "
                    "columns, rows, rowcount } with BLOB values as base64 "
                    "strings. Writes: { path, absolute_path, rowcount, "
                    "lastrowid }.",
        "errors": "400 missing path/sql, bad JSON/params/timeout, "
                  "multi-statement, or the SQLite error text itself "
                  "(e.g. no such table). 404 database file not found, "
                  "403 forbidden, 405 wrong method.",
    },
    {
        "method": "GET",
        "url": "/api/sysinfo",
        "name": "system_info",
        "description": "Return operating-system information about the host "
                       "running the server. Takes no parameters.",
        "params": [],
        "response": "200 JSON { system, system_name, release, version, "
                    "machine, processor, hostname, python_version, "
                    "cpu_count, distro, windows, mac }. system is the raw "
                    "platform name (Linux/Windows/Darwin); system_name is "
                    "the friendly one (Darwin -> macOS). distro is parsed "
                    "from /etc/os-release on Linux and null elsewhere; "
                    "windows/mac objects appear only on those systems.",
        "errors": "401 missing/invalid token, 405 wrong method.",
    },
    {
        "method": "POST",
        "url": "/api/socket/open",
        "name": "socket_open",
        "description": "Open a TCP connection to a network peer and hold "
                       "it as a session, so browser pages (which cannot "
                       "open raw sockets) can speak arbitrary TCP "
                       "protocols through the local service (e.g. SMTP/ "
                       "IMAP for a browser email client). With tls:b:true "
                       "the handshake runs immediately (implicit TLS, "
                       "e.g. IMAPS on 993), verified against the system "
                       "roots (+ optional ca_pem) with hostname check "
                       "and SNI. Returns an opaque session id for "
                       "send/recv/close/starttls. WARNING: "
                       "this is an open network proxy on the host; the "
                       "server binds localhost by default and relies on "
                       "that for protection.",
        "params": [
            {
                "name": "host",
                "location": "body (JSON)",
                "required": "b:true",
                "type": "string",
                "description": "Peer hostname or IP address to connect to.",
            },
            {
                "name": "port",
                "location": "body (JSON)",
                "required": "b:true",
                "type": "integer",
                "description": "Peer TCP port, 1-65535 (i:<int>).",
            },
            {
                "name": "timeout",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "number",
                "default": "i:5",
                "description": "Connect timeout in seconds (max 30).",
            },
            {
                "name": "tls",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "boolean",
                "default": "b:false",
                "description": "Handshake TLS immediately and verify the "
                               "peer (implicit TLS).",
            },
            {
                "name": "sni",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Server name for SNI and certificate "
                               "verification (default: host).",
            },
            {
                "name": "ca_pem",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Extra PEM CA bundle appended to the "
                               "system roots (corporate intercepts, "
                               "test PKIs).",
            },
        ],
        "response": "201 JSON { id, kind (tcp|tls), host, port }. "
                    "The id is an opaque session token for the other "
                    "socket routes.",
        "errors": "400 missing/invalid host/port/timeout/tls/sni/ca_pem, "
                  "bad JSON, connect or TLS-handshake failure (engine "
                  "error text), or too many open sessions. "
                  "405 wrong method.",
    },
    {
        "method": "POST",
        "url": "/api/socket/send?id=<id>",
        "name": "socket_send",
        "description": "Write bytes to an open socket session "
                       "(transparent: the peer receives exactly these "
                       "bytes).",
        "params": [
            {
                "name": "id",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Session id from socket_open.",
            },
            {
                "name": "content",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Bytes to write. Plain string for "
                               "encoding=text, base64 string for "
                               "encoding=base64. Alternatively send raw "
                               "bytes with a non-JSON Content-Type.",
            },
            {
                "name": "encoding",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "default": "text",
                "allowed": ["text", "base64"],
                "description": "How to interpret the JSON body content.",
            },
        ],
        "response": "200 JSON { id, bytes } with the written byte count.",
        "errors": "400 missing id / bad body / socket error, "
                  "404 unknown session, 405 wrong method.",
    },
    {
        "method": "GET",
        "url": "/api/socket/recv?id=<id>[&timeout=][&max_bytes=]",
        "name": "socket_recv",
        "description": "Read bytes from an open socket session "
                       "(transparent: whatever the peer sent). Long-polls "
                       "up to timeout seconds; returns whatever arrived, "
                       "an EOF flag when the peer closed, or a timeout "
                       "flag when nothing arrived in time.",
        "params": [
            {
                "name": "id",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Session id from socket_open.",
            },
            {
                "name": "timeout",
                "location": "query",
                "required": "b:false",
                "type": "string",
                "default": "5",
                "description": "Seconds to wait for data (max 60).",
            },
            {
                "name": "max_bytes",
                "location": "query",
                "required": "b:false",
                "type": "string",
                "default": "65536",
                "description": "Max bytes per call (1-1048576).",
            },
        ],
        "response": "200 JSON { id, encoding (base64), content, bytes, "
                    "eof, timed_out }. content carries base64 bytes (may "
                    "be empty); eof is true only with empty content "
                    "after the peer closed; timed_out is true only with "
                    "empty content when nothing arrived in time.",
        "errors": "400 missing id / bad timeout/max_bytes / socket "
                  "error, 404 unknown session, 405 wrong method.",
    },
    {
        "method": "POST",
        "url": "/api/socket/close?id=<id>",
        "name": "socket_close",
        "description": "Close an open socket session and release it. "
                       "Idle sessions also expire automatically.",
        "params": [
            {
                "name": "id",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Session id from socket_open.",
            }
        ],
        "response": "200 JSON { id, closed: true }.",
        "errors": "400 missing id, 404 unknown session, 405 wrong method.",
    },
    {
        "method": "POST",
        "url": "/api/socket/starttls?id=<id>",
        "name": "socket_starttls",
        "description": "Upgrade a plaintext socket session to TLS in "
                       "place (STARTTLS-style protocols: drive the "
                       "cleartext prelude over the session first, then "
                       "call this before sending any more application "
                       "bytes). Verified like implicit TLS (system roots "
                       "+ optional ca_pem, hostname check, SNI). A "
                       "failed handshake leaves the plaintext session "
                       "untouched; refused while a WebSocket owns the "
                       "session, while a recv is pending, or when "
                       "already TLS.",
        "params": [
            {
                "name": "id",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Session id from socket_open.",
            },
            {
                "name": "sni",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Server name for SNI and certificate "
                               "verification (default: the session host).",
            },
            {
                "name": "ca_pem",
                "location": "body (JSON)",
                "required": "b:false",
                "type": "string",
                "description": "Extra PEM CA bundle appended to the "
                               "system roots.",
            },
        ],
        "response": "200 JSON { id, kind (tls) }.",
        "errors": "400 missing id / bad body / already TLS / recv "
                  "pending / attached / handshake failure (engine error "
                  "text), 404 unknown session, 405 wrong method.",
    },
    {
        "method": "GET",
        "url": "/api/socket/ws?id=<id>",
        "name": "socket_ws",
        "description": "Attach a WebSocket to an open socket session "
                       "(the browser-native byte transport). Upgrade "
                       "with `Upgrade: websocket` (token via `?token=`); "
                       "after the 101 handshake, WS message payloads "
                       "flow transparently client->TCP (text frames as "
                       "UTF-8 bytes) and TCP bytes flow back as binary "
                       "frames; ping gets an auto-pong. A close from "
                       "either side, or any TCP/protocol error, ends "
                       "the whole session (a WS close is a session "
                       "close, like HTTP close). HTTP send/recv are "
                       "refused while attached; HTTP close kills the "
                       "bridge too.",
        "params": [
            {
                "name": "id",
                "location": "query",
                "required": "b:true",
                "type": "string",
                "description": "Session id from socket_open.",
            }
        ],
        "response": "101 Switching Protocols, then transparent "
                    "bidirectional bytes (see description).",
        "errors": "400 not-an-upgrade / bad handshake / missing id / "
                  "already attached, 404 unknown session, 405 wrong "
                  "method.",
    },
]
