# Gnumarus Native Operations HTTP Service

A small, self-contained CLI HTTP server that exposes low-level native
operations on the machine it runs on (filesystem, shell, SQLite, TCP
sockets, system information) through a REST API. It is written so that a
plain web frontend can reach native resources without Electron, NW.js, or
any other desktop shell: the server runs locally, binds to loopback only,
and answers cross-origin requests with open CORS.

The same service contract is implemented in nine runtimes. The Go port
(`go/`) is the reference: protocol ambiguities resolve in Go's favor,
`tests/run_all.py` runs the Go server by default, and new features land
there first. The other ports mirror the same routes, status codes, JSON
shapes, CLI flags, configuration precedence, and token authentication.

## Table of contents

- [What it is](#what-it-is)
- [Features](#features)
- [Implementations](#implementations)
- [Quick start](#quick-start)
- [API overview](#api-overview)
- [Wire format](#wire-format)
- [Configuration](#configuration)
- [Security](#security)
- [Possible applications](#possible-applications)
- [Usage examples](#usage-examples)
- [Tests](#tests)
- [Showcases](#showcases)
- [License](#license)

## What it is

Gnumarus Native Operations HTTP Service is a command-line program. When
started it listens on a TCP port (default `1423`) on a loopback address
and exposes a REST HTTP API. A frontend running in any browser, on any
origin, can call it to read and write files, run shell commands, query
SQLite databases, open TCP connections, and read system information on the
host where the server runs.

Key design points:

- Loopback only. `--host` accepts only `localhost`, addresses in
  `127.0.0.0/8`, and `::1`. Anything else is a configuration error and
  the process exits before listening. Every request's `Host` header must
  name a loopback address as well.
- Open CORS on every response, including errors, plus `OPTIONS` preflight
  with `204`. `Access-Control-Allow-Origin: *`.
- Token authentication is required on every route except `GET /`,
  `GET /api/help`, and (where implemented) the OAuth2 loopback callback.
  The token is minted at startup and printed, or fixed with `--token`.
  `--no-auth` disables it explicitly, with a loud warning.
- `GET /api/help` is the single source of truth for the route catalog:
  URL, name, description, parameters, and (for the Go reference) the
  OAuth2 routes. Clients are expected to drive themselves from it.
- Stdlib only for the reference Python port; the other ports add only the
  dependencies listed below.

## Features

Filesystem:

- `GET /api/dir?path=...` lists a directory with per-entry metadata:
  `name, path, type, size, atime, mtime, ctime, crtime, mode`. `crtime`
  is null where the OS has no birth time.
- `GET /api/file?path=...` reads text and binary files, in JSON
  (`auto`/`text`/`base64` modes) or as raw bytes (`raw=1`).
- `POST /api/file?path=...` creates a file, empty or with initial data
  (JSON `{content, encoding}` or raw bytes). `201` on success, `409` if
  it already exists.
- `PUT /api/file?path=...` overwrites file content. `404` if missing.
- `DELETE /api/file?path=...` deletes a file. `400` if the path is a
  directory.
- `DELETE /api/dir?path=...` removes an empty directory only.

Shell:

- `POST /api/shell` runs a bash string via `bash -c` and returns
  `{command, cwd, returncode, stdout, stderr, timed_out,
  stdout_truncated, stderr_truncated, max_stdout, max_stderr, stdin_bytes}`.
  Stdin can be piped in (text or base64). Output capture is streamed and
  truncated to a configurable cap (default 1 MB, 100 MB hard cap). A
  timeout kills the whole process group.

SQLite:

- `POST /api/sqlite/create?path=...` creates a new SQLite file.
- `POST /api/sqlite/query?path=<db>` runs one SQL statement. SELECT-style
  results come back as `{columns, rows, rowcount}` (BLOBs as base64);
  writes commit and return `{rowcount, lastrowid}`. Fresh connection per
  request. Bound parameters are supported natively.

TCP socket proxy:

- `POST /api/socket/open` dials a TCP peer and holds it as a session
  (`{host, port, timeout?, tls?, sni?, ca_pem?}`). Returns `{id, kind,
  host, port}`.
- `POST /api/socket/starttls?id=<id>` upgrades a plaintext session to
  TLS in place.
- `POST /api/socket/send?id=<id>` writes bytes.
- `GET /api/socket/recv?id=<id>` long-polls for bytes.
- `POST /api/socket/close?id=<id>` closes the session.
- `GET /api/socket/ws?id=<id>` attaches a real WebSocket to a session
  (token via `?token=`). After the `101`, WS message payloads flow
  client to TCP and TCP bytes come back as binary frames.

System information:

- `GET /api/sysinfo` returns OS and host information: `{system,
  system_name, release, version, machine, processor, hostname,
  python_version, cpu_count, distro?, windows?, mac?}`.

Gmail OAuth2 (Go reference only):

- `POST /api/oauth2/start`, `GET /api/oauth2/callback`,
  `GET /api/oauth2/poll?id=`, `POST /api/oauth2/refresh?id=`,
  `POST /api/oauth2/close?id=` implement an OAuth 2.0 Authorization
  Code + PKCE flow for mail providers, with Google defaults. The service
  holds the client secret and refresh token server-side; the page never
  sees a Google URL.

Service index:

- `GET /` returns `{service, version, help: "/api/help"}`.

## Implementations

| Runtime | Directory | Build | Run |
|---|---|---|---|
| Go (reference) | `go/` | `./go/build.sh` | `go/gnumarus` |
| Rust | `rust/` | `./rust/build.sh` | `rust/bin/gnumarus` |
| Python 3 | `python3/` | none | `python3 python3/server.py` |
| Node.js / TypeScript | `nodejs/` | `./nodejs/build.sh` | `node nodejs/dist/server.js` |
| PHP | `php/` | `./php/build.sh` | `php php/server.php` |
| Ruby | `ruby/` | `./ruby/build.sh` | `ruby ruby/server.rb` |
| Kotlin / JVM | `kotlin/` | `./kotlin/build.sh` | `kotlin/build/install/gnumarus/bin/gnumarus` |
| C# / .NET | `dotnet/` | `./dotnet/build.sh` | `dotnet dotnet/publish/gnumarus.dll` |
| C++ | `cpp/` | `./cpp/build.sh` | `cpp/bin/gnumarus` |

All ports share the same 19 routes; the Go reference adds the 5 Gmail
OAuth2 routes (23 total). A unified launcher is provided:

```
./start.sh --runtime <name> [server args...]
```

`name` is one of `python`, `kotlin`, `php`, `ruby`, `nodejs`, `go`,
`dotnet`, `rust`. `start.bat` and `start.ps1` provide the same contract
on Windows.

## Quick start

Build and run the Go reference:

```
./go/build.sh
./go/gnumarus --port 1423
```

Or use the launcher:

```
./start.sh --runtime go --port 1423
```

The server prints a line like:

```
config sources: host=... port=... token=... no_auth=...
<token>
```

The printed token is required for every request except `GET /` and
`GET /api/help`. Keep the server running and call the API, for example:

```
curl http://127.0.0.1:1423/api/help
curl -H "Authorization: Bearer <token>" "http://127.0.0.1:1423/api/dir?path=."
```

## API overview

All routes return JSON unless noted. Errors are JSON objects of the form
`{error, message, path?}` with the appropriate HTTP status
(400/403/404/405/409).

| Method | URL | Name |
|---|---|---|
| GET | `/` | (index) |
| GET | `/api/help` | help |
| GET | `/api/dir?path=...` | read_directory |
| GET | `/api/file?path=...` | read_file |
| POST | `/api/file?path=...` | create_file |
| PUT | `/api/file?path=...` | overwrite_file |
| DELETE | `/api/file?path=...` | delete_file |
| DELETE | `/api/dir?path=...` | delete_directory |
| DELETE | `/api/dir/shallow?path=...` | delete_directory_shallow |
| POST | `/api/shell` | exec_shell |
| POST | `/api/sqlite/create?path=...` | sqlite_create |
| POST | `/api/sqlite/query?path=...` | sqlite_query |
| POST | `/api/socket/open` | socket_open |
| POST | `/api/socket/starttls?id=...` | socket_starttls |
| POST | `/api/socket/send?id=...` | socket_send |
| GET | `/api/socket/recv?id=...` | socket_recv |
| POST | `/api/socket/close?id=...` | socket_close |
| GET | `/api/socket/ws?id=...` | socket_ws |
| GET | `/api/sysinfo` | system_info |
| POST | `/api/oauth2/start` | oauth2_start (Go only) |
| GET | `/api/oauth2/callback` | oauth2_callback (Go only, open route) |
| GET | `/api/oauth2/poll?id=...` | oauth2_poll (Go only) |
| POST | `/api/oauth2/refresh?id=...` | oauth2_refresh (Go only) |
| POST | `/api/oauth2/close?id=...` | oauth2_close (Go only) |

Use `GET /api/help` for the full machine-readable catalog, including
parameter types and descriptions.

## Wire format

JSON carries only strings, arrays, and objects. Numbers, booleans, and
nulls are encoded as tagged strings so that a JSON parser never produces
a native number, boolean, or null anywhere in a response:

- `i:<int>` for integers (for example `i:42`)
- `f:<float>` for finite floats (for example `f:3.14`)
- `b:true` / `b:false` for booleans
- `null` (the bare string) for null

Fixed vocabulary and free text (paths, contents, messages, column names,
query parameters) travel raw. Two positions are fully tagged, element by
element, because they are heterogeneous: SQLite row cells and `params`
values. In those positions strings take an `s:` prefix (`"s:ada"`,
`"s:"` for the empty string), so `"s:i:5"` (text) never collides with
`"i:5"` (integer 5).

Optional inputs treat a missing key and an explicit `"null"` value as
equivalent. Strict per-field kinds apply: `max_*` require `i:`, `timeout`
accepts `i:` or `f:`, `readonly` requires `b:`. Anything else is a `400`.

## Configuration

Every behavior resolves per key with the following precedence, highest
first:

1. CLI flag (`--host`, `--port`, `--token`, `--auth`/`--no-auth`)
2. Environment variable (`GNUMARUS_HOST`, `GNUMARUS_PORT`,
   `GNUMARUS_TOKEN`, `GNUMARUS_NO_AUTH`)
3. Stdin `key = value` lines (skipped when stdin is a TTY)
4. `config.cfg` next to the server script
5. Default (`127.0.0.1` / `1423` / minted token / auth on)

`config.cfg` and stdin use UTF-8 `key = value` lines; `#` and `;` start
comments, `[section]` headers and unknown keys are skipped, surrounding
quotes are stripped, and the last duplicate wins. `no_auth` accepts
`true/false/yes/no/on/off/1/0`.

Defensive reads apply: input containing a NUL byte or failing UTF-8
decode is ignored wholesale; at most 1 MB is read. Invalid values fail
fast with `config error: ...` on stderr and exit code 2. Startup prints a
flushed `config sources:` summary line before binding.

## Security

This service is powerful by design and is intended for local, single-user
use. Read this before running it.

Risks:

- Arbitrary code execution. `POST /api/shell` runs an arbitrary bash
  string. Combined with open CORS, any website the user visits while the
  server runs can invoke it. The only current protection is the
  loopback bind.
- Open network proxy. The socket proxy (`/api/socket/*`) can dial any
  host and port reachable from the machine, and forward bytes in both
  directions. Like the shell endpoint, it is callable by any origin.
- Filesystem access. Any client with the token can read, create,
  overwrite, and delete files and directories reachable by the server
  process, including outside the current working directory (paths are
  resolved against the server, `~` expands to the server user's home).
- Token leakage. The token is passed either in the `Authorization`
  header or as `?token=`. Query strings leak into logs and browser
  history; prefer the header.
- No sandbox. There is no path sandboxing, command allowlist, or rate
  limiting. `--no-auth` removes the only credential check.
- Host header is not a credential. The loopback Host allowlist is
  defense in depth; a hostile page can still use `http://127.0.0.1`
  directly, and non-browser clients can forge the header. The token is
  the real protection.
- Stored credentials. The Go OAuth2 helper keeps client secrets and
  refresh tokens in process memory for the lifetime of the session.

Mitigations present:

- Loopback-only bind (`--host` validation, exit 2 on anything else).
- Host header allowlist on every request, including `OPTIONS`, open
  routes, and WebSocket upgrades, checked before auth.
- Token authentication with constant-time comparison; `401` plus
  `WWW-Authenticate: Bearer`.
- `--no-auth` prints a loud warning and is opt-in.
- Idle socket session expiry (300 s) and caps (128 open sessions,
  64 OAuth2 sessions).

Recommendations:

- Keep the server stopped when not in use.
- Never run with `--no-auth` unless you understand the consequences.
- Never expose the port beyond loopback (do not reverse-proxy it).
- Prefer the `Authorization: Bearer` header over `?token=`.
- Treat the shell and socket endpoints as privileged.

## Possible applications

- Web-based file managers that operate on the real filesystem (the
  included `filemanager.html` and `showcase/filemanager/`).
- Browser email or chat clients speaking IMAP/SMTP through the socket
  proxy (the included `showcase/emailclient/`).
- Frontend tooling and dashboards that need to read logs, query local
  SQLite databases, or run build commands.
- Automation bridges between a web UI and local developer tools.
- Educational demos of HTTP, sockets, and OAuth without a backend
  framework.

## Usage examples

Start the server (Go reference):

```
./start.sh --runtime go --port 1423
```

List the current directory:

```
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:1423/api/dir?path=."
```

Read a text file:

```
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:1423/api/file?path=README.md"
```

Download raw bytes:

```
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:1423/api/file?path=image.png&raw=1" -o image.png
```

Create a file:

```
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content":"hello","encoding":"text"}' \
  "http://127.0.0.1:1423/api/file?path=/tmp/demo.txt"
```

Run a shell command:

```
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"ls -la && pwd","timeout":10}' \
  http://127.0.0.1:1423/api/shell
```

Query SQLite:

```
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT 1 AS n"}' \
  "http://127.0.0.1:1423/api/sqlite/query?path=/tmp/demo.db"
```

Open a TCP session:

```
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"host":"example.com","port":"i:80"}' \
  http://127.0.0.1:1423/api/socket/open
```

Read system information:

```
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:1423/api/sysinfo
```

## Tests

The shared black-box suite lives in `tests/` and uses only the Python
stdlib. It runs against any port:

```
python3 tests/run_all.py
```

The runner mints a token, starts the Go reference server, runs all tests,
and stops it. To test another port:

```
python3 tests/run_all.py --server-cmd "python3 python3/server.py" \
  --server-cfg python3/config.cfg
```

`--server-cfg` is required together with `--server-cmd`. SQLite tests
self-skip when the server reports no support. The suite enforces the wire
invariant (no JSON number/boolean/null anywhere after parsing) on every
response.

## Showcases

- `filemanager.html`: single-file web file manager (inline CSS and JS, no
  build). Talks to `http://127.0.0.1:1423`. Works hosted anywhere or
  via `file://`.
- `showcase/filemanager/`: multi-file web file manager (index.html,
  styles.css, app.js) with full entry metadata, text edit/save, binary
  download, create/delete, and a sysinfo line.
- `showcase/emailclient/`: read-only IMAP inbox with provider presets,
  password and Google OAuth2 authentication, paginated UID listing, and
  MIME-aware message viewing. Uses only the socket proxy.

## License

MIT. See `LICENSE`.