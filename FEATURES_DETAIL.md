# FEATURES_DETAIL

## 1. CLI Server (`python3/`)

- Stdlib only: `http.server.ThreadingHTTPServer`, `argparse`, `json`, `base64`, `mimetypes`. No pip dependencies.
- Flat module layout (no single-file constraint anymore):
  `server.py` (thin entry: CLI + boot), `version.py`, `tags.py`
  (wire tags), `config.py` (multi-source resolution), `fsutil.py`
  (paths/mime), `times.py` (os-release + statx birth), `routes.py`
  (ROUTES + OPEN_ROUTES), `handler.py` (plumbing/auth/dispatch/help
  + Handler wiring), `handler_fs.py`, `handler_shell.py`,
  `handler_sqlite.py`, `handler_sysinfo.py` (endpoint mixins).
  Invocation unchanged: `python3 python3/server.py` (script dir lands
  on `sys.path`, so the sibling imports resolve).
- Start: `python3 python3/server.py [--host H] [--port P] [--token TEXT] [--auth|--no-auth]` (see 16 for all configuration sources).
- A random token (`secrets.token_urlsafe(32)`) is minted at startup and
  printed (flushed) unless `--token` fixes it or auth is disabled
  with a loud warning.
- Threaded, so concurrent frontend requests work.
- `ROUTES` list is the single source of truth; `/api/help` serializes it directly.

## 2. CORS

- Every response (JSON, raw bytes, errors) includes:
  - `Access-Control-Allow-Origin: *`
  - `Access-Control-Allow-Methods: GET, POST, PUT, DELETE, PATCH, OPTIONS`
  - `Access-Control-Allow-Headers: Content-Type, Authorization, X-Requested-With`
- `OPTIONS *` returns `204` with CORS headers (browser preflight support).
- Rationale: the service exists to let plain frontend web apps access native host resources without Electron/NW.js.

## 3. `GET /` (index)

- Response: `{ service, version, help: "/api/help" }`.

## 4. `GET /api/help`

- Response: `{ version, count, routes: [{ method, url, name, description, params, response, errors? }], auth: { required, open_routes, how } }`.
- Each param: `{ name, location, required, type, default?, allowed?, description }`.
- The `auth` block tells clients the policy (required flag, always-open
  routes `/`, `/api/help`, the OAuth loopback callback
  `/api/oauth2/callback` where implemented, and `OPTIONS *`, and how to
  send the token).

## 14. Token Authentication

- Everything except `GET /`, `GET /api/help`, `GET /api/oauth2/callback`
  (where implemented) and `OPTIONS` preflight
  requires the API token: `Authorization: Bearer <token>` header
  (preferred — query strings leak into logs) or `?token=` (handy for
  direct download links).
- Comparison with `hmac.compare_digest`; failures get 401 JSON plus
  `WWW-Authenticate: Bearer`, with CORS headers so browsers can read it.
- `--no-auth` skips all checks (startup prints a warning); `--token`
  fixes the token for scripts/tests.
- `filemanager.html`: password field for the token (persisted in
  localStorage, sent on every request). A 401 switches the header to an
  "Unauthorized — paste the token and Retry" state without disabling
  the controls.
- `tests/`: `run_all.py` mints a per-run token and passes `--token`;
  helpers attach it automatically (`GNATIVE_TEST_TOKEN`, opt out with
  `auth=False`); `tests/test_auth.py` covers 401s, both token
  transports, open routes, and a `--no-auth` sub-server.

## 5. `GET /api/dir?path=<path>` (read_directory)

- `path` (required): absolute or relative (resolved against server CWD, `~` expanded, normalized). All six file endpoints accept `~/` paths expanding to the server user's home directory (documented in `/api/help` since v0.7.1).
- Times (epoch float seconds): `atime` last access, `mtime` last content modification, `ctime` last status change (doubles as creation time on Windows), `crtime` file creation/birth — from `st_birthtime` where exposed (macOS/Windows), else a Linux-only ctypes `statx(STATX_BTIME)` lookup with `stx_mask` check, else `null` (old kernels, filesystems without birth time). Measured: one extra syscall per entry (~0.2 ms/entry for full listings) vs ~6.7 ms/entry for a `stat` subprocess, with no text parsing involved.
- Success (200): `{ path, absolute_path, count, entries }`, entries sorted by name, each `{ name, path, type, size, mtime, mode }` where `type` is `file|dir|symlink|other`, `mtime` is epoch float, `mode` is octal permission string (e.g. `0o664`).
- Errors (JSON `{ error, message, path? }`): `400` missing path / not-a-directory, `404` not found, `403` permission / OS error.

## 6. `GET /api/file?path=<path>[&encoding=...][&raw=1]` (read_file)

- `path` (required), `encoding` (optional, default `auto`: `auto|text|base64`), `raw` (optional, `1` = raw bytes).
- JSON mode (default): `{ path, absolute_path, size, mtime, mime_type, is_binary, encoding, content }`.
  - `auto`: tries UTF-8 decode plus a control-character heuristic; text files come back as `{ is_binary: false, encoding: "utf-8", content: "<text>" }`, binaries as `{ is_binary: true, encoding: "base64", content: "<base64>" }`.
  - `text`: forces UTF-8 decode with `errors="replace"`.
  - `base64`: forces base64 (works for text and binary).
  - `mime_type` via `mimetypes.guess_type`, fallback `application/octet-stream`.
- Raw mode (`raw=1`): responds with raw file bytes, guessed `Content-Type`, `Content-Length`, plus CORS headers. Useful for `<img>`, downloads, etc.
- Errors: `400` missing path / is-a-directory / invalid encoding, `404` not found, `403` permission / OS error.
- Verified: text roundtrip, binary base64 roundtrip (`00 01 02 FF FE 00 41 42 43`), forced modes, raw bytes, all error cases (22 checks passing).

## 7. Error / Method Contract

- Unknown routes → `404` JSON. Wrong method on a known route → `405` JSON.
  `PATCH` is not supported anywhere.
- `409 conflict` when creating a file that already exists.
- All errors are JSON `{ error, message, path? }` with CORS headers.

## 8. Write Operations (`POST` / `PUT /api/file`)

- Request body, two flavors:
  - JSON (`Content-Type: application/json`): `{ content, encoding }`,
    `encoding` is `text` (default, UTF-8 string) or `base64` (binary-safe).
    Omitted `content` means zero bytes.
  - Raw bytes (any other Content-Type, e.g. `application/octet-stream`):
    stored byte-for-byte.
- `POST /api/file?path=...` (create_file): exclusive creation (`O_EXCL`
  semantics via mode `"xb"`, race-safe). No body → empty file.
  Success `201 { path, absolute_path, size, mime_type }`.
  Errors: `400` missing path / bad JSON / bad base64 / bad encoding,
  `404` parent directory missing, `409` path already exists,
  `403` permission.
- `PUT /api/file?path=...` (overwrite_file): same body rules; no body
  truncates to zero bytes. Success `200 { path, absolute_path, size,
  mime_type }`. Errors: `400` missing path / is-a-directory / bad body,
  `404` not found, `403` permission.

## 9. Delete Operations (`DELETE`)

- `DELETE /api/file?path=...` (delete_file): removes one file.
  Success `200 { path, absolute_path, deleted: true }`.
  Errors: `400` missing path / is-a-directory (hint to use
  `DELETE /api/dir`), `404` not found, `403` permission.
- `DELETE /api/dir?path=...` (delete_directory): `rmdir` equivalent —
  only empty directories. Success `200 { ..., deleted: true }`.
  Errors: `400` missing path / not-a-directory / not-empty,
  `404` not found, `403` permission.
- `DELETE /api/dir/shallow?path=...` (delete_directory_shallow): deletes a
  shallow directory containing only files (including symlinks) and empty
  subdirectories. Fails when it contains a non-empty real subdirectory.
  Does not delete recursively. Success `200 { ..., deleted: true }`.
  Errors: `400` missing path / not-a-directory / not-shallow, `404` not
  found, `403` permission.

## 10. Regression Suite (`tests/`)

- Stdlib only (`unittest` + `http.client`, no pytest).
- `run_all.py [--server-cmd CMD] [--server-cfg PATH]` runs the whole
  black-box suite against another implementation with the same
  CLI/HTTP contract (e.g. the Kotlin port); sub-server tests follow
  via `GNATIVE_TEST_SERVER_CMD`/`GNATIVE_TEST_CFG`. `--server-cfg`
  is required with `--server-cmd`. SQLite tests
  self-skip when the server reports no support (`GNATIVE_TEST_SQLITE`).

## 18. PHP Port (`php/`)

- Same service, same contract: all 12 routes, same URLs/statuses/JSON
  shapes, same CLI flags, same config precedence (CLI > `GNUMARUS_*`
  > stdin > `php/config.cfg` > default), same token auth. Zero
  composer dependencies (core + pdo_sqlite only).
- Layout: `php/server.php` (CLI entry: resolves config, prints the
  `config sources:` summary, then spawns `php -S` with the resolved
  values exported as env so every worker router resolves
  identically) + `php/router.php` (front controller: handles every
  request, never returns false, so no static file is ever served) +
  `php/src/Config.php` + `php/src/Api.php`. Run `php php/server.php`.
  Workers via `PHP_CLI_SERVER_WORKERS=8`, Ctrl+C forwarded with pcntl.
- Faithful details: exclusive create via `fopen(..,'xb')`, clearstatcache
  per request (long-lived workers), `setsid`-based process-group kill
  on shell timeout (killpg parity), PDO busy timeout + `PRAGMA
  query_only=ON` for readonly, DDL `rowcount` -1 like CPython, tilde
  expansion, `/etc/os-release` parsing.
- Known impl differences (documented, not bugs): `atime/mtime/ctime`
  are ints (PHP `lstat` has no sub-second precision); `crtime` is
  always null on Linux (no birth-time API; FFI is blocked); no
  `python_version` (see `php_version`); 503 `sqlite_unavailable` when
  pdo_sqlite is missing (suite then skips SQLite).
- Verified: shared suite 123/123 against it (1 documented skip),
  123/123 against Python and Kotlin after the harness changes, plus
  headless `file://` smoke (Online, 12 endpoints).

## 20. Node.js Port (`nodejs/`)

- Same service, same contract: all 12 routes, same URLs/statuses/JSON
  shapes (tagged scalars included), same CLI flags, same config
  precedence (CLI > `GNUMARUS_*` > stdin > file > default; file
  search: working dir → `dist/` → project root), same token auth.
  Runtime is node core only (`http`, `fs`, `child_process`,
  `crypto`, `node:sqlite`); TypeScript + `@types/node` are build-time
  devDeps. Build `./nodejs/build.sh`, run `node nodejs/dist/server.js`.
- Faithful details: exclusive create via `wx` flag, detached spawn
  (`detached: true` + `kill(-pid)`) for killpg parity, stdin
  `ignore`, real birth time via `birthtimeMs` (epoch 0 → null),
  real bound params via `node:sqlite`, DDL `rowcount` -1, tilde via
  `os.homedir()`, `/etc/os-release` parsing.
- Known impl differences (documented, not bugs): `machine` is `x64`
  (node `os.arch`); `release`/`version`/`machine` come from node `os`
  (same values on Linux); no `python_version` (see `node_version`).
- Verified: shared suite 126/126 against it, plus headless `file://`
  smoke (Online, 12 endpoints). During the port the suite caught 2
  real Node bugs (lenient `Buffer.from(x,"base64")`, `f:` tag
  accepted for int fields) and 1 test-harness flake (2 MB stdin via
  pipe+thread → switched to temp-file stdin, deterministic).

## 21. .NET Port (`dotnet/`)

- Same service, same contract: all 12 routes, same URLs/statuses/JSON
  shapes (tagged scalars included), same CLI flags, same config
  precedence (CLI > `GNUMARUS_*` > stdin > file > default; file
  search: working dir → executable dir), same token auth. One NuGet
  dep: Microsoft.Data.Sqlite (HttpListener, JSON, FS, crypto all in
  the shared framework). Build `./dotnet/build.sh`, run
  `dotnet dotnet/publish/gnumarus.dll`.
- Faithful details: exclusive create via `FileMode.CreateNew`,
  `setsid` spawn + libc `kill(-pgid)` via P/Invoke (killpg parity),
  stdin closed at once (DEVNULL parity), real SQLite binding with
  busy-timeout pragma + `Mode=ReadOnly`, DDL `rowcount` -1, tilde via
  user profile, `/etc/os-release` parsing, real birth time via
  `File.GetCreationTimeUtc` and ctime/size via statx P/Invoke (mask
  `BTIME|CTIME`, `stx_mask`-checked, struct sizeof-asserted 256).
- Notable bug found by the suite: `Microsoft.Data.Sqlite` pools
  connections per connection string, so a reused handle survived file
  deletion (create returned 201 with no file on disk). Fixed with
  `Pooling=False` everywhere — statelessness requires fresh opens.
- Environment notes: plain IPv6 is broken here, so the build forces
  `DOTNET_SYSTEM_NET_DISABLEIPV6=1` for NuGet restore; `publish/`,
  `bin/`, `obj/` are gitignored build outputs.
- Known impl differences (documented, not bugs): `machine` is `x64`;
  dangling symlinks are skipped in listings (no lstat equivalent);
  no `python_version` (see `dotnet_version`).
- Verified: shared suite 126/126 against it, plus headless `file://`
  smoke (Online, 12 endpoints).
- Layout: `tests/helpers.py` (request helpers, isolated workspaces under
  `./tmp/test_workspace`, `GNATIVE_TEST_BASE` env), `tests/run_all.py`
  (runner), `tests/test_help_cors.py`, `tests/test_read_dir.py`,
  `tests/test_read_file.py`, `tests/test_write_file.py`,
  `tests/test_delete.py`.
- `python3 tests/run_all.py`: picks a free port, spawns `server.py`,
  waits for readiness, runs `unittest` discovery (158 tests), shuts the
  server down, exits non-zero on failure.
- Test files also work against a manually started server:
  `python3 python3/server.py --port 1423` + `python3 -m unittest discover -s tests`.
- Rule: run the full suite after every new feature (regression gate).

## 11. Web File Manager (`filemanager.html`)

- Single self-contained file: inline CSS + JS, no dependencies, no build.
  Open directly (`file:///.../filemanager.html`) or host anywhere; it
  always talks to the fixed default `http://127.0.0.1:1423`.
- Capability-driven: on load it fetches `/api/help` and enables each UI
  block only when the matching route `name` is advertised
  (`read_directory` → listing/nav, `read_file` → view/download,
  `create_file` → new-file panel, `overwrite_file` → save button,
  `delete_file`/`delete_directory` → per-row delete buttons).
- Offline behavior: failed `/api/help` shows an error banner naming the
  expected address and disables every control except Retry. A 401 shows
  the token guidance instead (see 14) and keeps controls enabled.
- Features: path input + Go, Up, Refresh, breadcrumbs, dirs-first listing
  with type/size/mtime, text preview with edit+save (files over 512 KB
  are download-only), binary download via `?raw=1` blob URL, text file
  creation, delete with `confirm()`, shell console (command + optional
  cwd, max stdout/stderr byte fields defaulting to 1048576, stdin
  textarea + text/base64 select, shows exit
  code/stdout/stderr plus "[truncated at N bytes]" notes and stdin byte
  count) backed by
  `exec_shell`, SQLite panel (create-DB row + query row with result
  grid, each gated by its own capability) backed by `sqlite_create` /
  `sqlite_query`.
- All server/network errors are rendered in the in-page error banner.
- Verified headless (Chrome `--dump-dom` over `file://`): online run
  shows "Online (12 endpoints)" with 26 rows for `/`; offline run shows
  the Offline banner with controls disabled.

## 13. SQLite (`POST /api/sqlite/*`)

- Stateless by design: every request opens a fresh `sqlite3`
  connection, executes, commits when applicable, closes it and returns.
  The database file on disk is permanent; only the connection is
  short-lived. Concurrent requests are serialized by SQLite's own
  locking (plus the busy timeout below).
- `POST /api/sqlite/create?path=<path>` (sqlite_create): connects
  (which creates the file) and verifies with
  `SELECT sqlite_version()`. `409` when the path already exists,
  `404` when the parent directory is missing. On mid-creation failure
  a partial file is removed so a retry starts clean. Success is
  `201 { path, absolute_path, size, sqlite_version }`.
- `POST /api/sqlite/query?path=<db>` (sqlite_query): body
  `{ sql, params?, timeout?, readonly? }`.
  - `sql`: exactly one statement. Multi-statement strings are rejected
    with 400 (sqlite raises `Warning`, mapped explicitly) — send one
    request per statement.
  - `params`: array for `?` placeholders or object for `:named` ones;
    values must be JSON scalars (BLOB input via params is rejected
    with 400 — store blobs with SQL literals/functions instead).
  - `timeout`: busy-wait on locked DBs, default 5 s, (0, 300].
  - `readonly`: opens via `file:...?mode=ro` URI; writes then fail
    with the SQLite error text.
  - SELECT-style (cursor has a description): `{ columns, rows,
    rowcount }`; rows are arrays, BLOB values are base64 strings
    (documented; JSON has no binary type).
  - Writes/DDL: committed, `{ rowcount, lastrowid }`.
  - Errors carry the SQLite message itself
    (e.g. `SQLite error: no such table: missing`) with 400;
    missing DB file → 404; other methods → 405.

## 12. Shell Execution (`POST /api/shell`)

- Body (JSON): `{ command, timeout?, cwd?, max_stdout?, max_stderr?,
  stdin?, stdin_encoding? }`. `command` is the exact
  string as typed in bash — pipes, `<`/`>` redirections, `&&`, `;`
  all work because it runs via `bash -c`. `timeout` defaults to 30 s
  (clamped to 1–300); `cwd` defaults to the server working directory
  and must be an existing directory.
- Input: `stdin` is piped to the command as if from a previous pipe
  stage (`sort` with `stdin: "c\nb\na\n"` behaves like
  `printf ... | sort`); `stdin_encoding` is `text` (default, UTF-8)
  or `base64` (binary-safe). Omitted/null means empty stdin — the
  command sees EOF at once, exactly as before this feature existed.
- Response: `{ command, cwd, returncode, stdout, stderr, timed_out,
  stdout_truncated, stderr_truncated, max_stdout, max_stderr,
  stdin_bytes }`.
  `returncode` is the exit status of the last pipeline element
  (`false | true` → 0, `true | false` → 1). stdout/stderr are decoded
  UTF-8 with replacement of invalid sequences.
- Output limits: `max_stdout` / `max_stderr` are integer byte caps
  (default 1048576 each, hard cap 104857600 → 400 beyond, 0 allowed).
  The command always runs to completion — side effects (files written,
  pipes consumed) happen in full. Capture is a streaming drain
  (reader threads, 64 KB chunks) that keeps only the first N bytes per
  stream and discards the rest, so server memory stays bounded even for
  multi-GB outputs. `stdout_truncated` / `stderr_truncated` flags tell
  clients when to display "truncated at N bytes".
- Possible and easy — the one nuance worth knowing: the API captures
  what the shell process emits on its own stdout/stderr fds, so
  redirections written inside the command apply before capture:
  `... > f.txt` leaves captured stdout empty (content went to the
  file), `cmd 2>&1` merges into captured stdout leaving stderr empty.
  This is plain bash semantics, not an API limitation — the examples
  were verified live (`cat < a.txt | sort > b.txt`,
  `echo AAA | tee out.txt 2>&1`).
- Timeout: the whole process group is killed (`killpg`, new session per
  command, so backgrounded children die too); the response carries
  `timed_out: true`, `returncode: 124` (as in `timeout(1)`) plus
  whatever output was produced.
- Errors: `400` missing/empty command, non-JSON body, non-numeric
  timeout, nonexistent cwd; other methods on the route → `405`.
- SECURITY: combined with open CORS this is arbitrary code execution
  callable by any website the user visits while the server runs. The
  only current protection is the localhost bind (default 127.0.0.1).
  Run it only on your own machine, keep it stopped when not in use,
  and treat token-auth / command allowlist as required follow-ups
  (see Future in PLAN.md).

## 17. Kotlin Port (`kotlin/`)

- Same service, same contract: all 18 routes (`socket_ws` +
  `socket_starttls`), same URLs/statuses/JSON shapes, same CLI flags
  (`--host/--port/--token/--auth/--no-auth`), same config precedence
  (CLI > `GNUMARUS_*` > stdin > file > default; file search: working
  dir → jar dir → install root), same token auth (Bearer/`?token=`,
  help/index open).
- Stack: Kotlin 2.4.21 + Gradle (application plugin, `installDist`),
  a hand-rolled HTTP/1.1 server over a raw `ServerSocket`
  (`kotlin/src/main/kotlin/Http.kt`, one request per connection,
  `Connection: close`, accept loop on virtual threads), Gson 2.14.0
  (with `serializeNulls` — 2.14 drops explicit nulls otherwise),
  real SQLite via sqlite-jdbc 3.53.4.0 (fresh JDBC connection per
  request; DDL `rowcount` -1 like CPython), JNA 5.19.1 for Linux
  `statx` ctime (NIO has no change-time), NIO birth time for
  `crtime`. Build `./kotlin/build.sh`, run
  `kotlin/build/install/gnumarus/bin/gnumarus`. Sources:
  `kotlin/src/main/kotlin/{Main,Config,Api,Http,SocketWs}.kt`.
- Own TCP layer on purpose: the JDK `HttpServer` was replaced because
  it exposes no raw socket, so no same-URL WebSocket upgrade was
  possible through it. The hand-rolled server (same shape as
  Ruby/PHP/C++) hands the raw `java.net.Socket` to the RFC 6455
  bridge ported from Go (`kotlin/src/main/kotlin/SocketWs.kt`),
  which is why the Kotlin port now serves `socket_ws` like the
  others (see §29).
- Known impl differences (documented, not bugs): `machine` is
  `amd64` not `x86_64` (JVM `os.arch`); `python_version` is null
  (see `jvm_version`/`kotlin_version`); doubles may render in
  scientific notation (same parsed value).
- Verified: the shared Python suite passes 214/214 against it with
  zero skips (`run_all.py --server-cmd
  kotlin/build/install/gnumarus/bin/gnumarus --server-cfg
  kotlin/build/install/gnumarus/config.cfg`), including the full
  WebSocket module, the Host-deny-on-upgrade case and the
  STARTTLS-while-WS-attached refusal; and `filemanager.html` works
  against it unchanged (headless `file://` smoke: Online, 18
  endpoints).

## 15. System Info (`GET /api/sysinfo`)

- No parameters. Returns `{ system, system_name, release, version,
  machine, processor, hostname, python_version, cpu_count, distro,
  windows, mac }` using stdlib `platform` only.
- `system` is the raw name (`Linux`/`Windows`/`Darwin`); `system_name`
  is the friendly one (`Darwin` → `macOS`). `release` is the kernel
  release on Linux (e.g. `7.0.0-38-generic`), the Windows build number
  on Windows, the Darwin release on macOS.
- `distro` is parsed from `/etc/os-release` on Linux
  (`{ id, name, version, version_id, pretty_name }`, e.g. Ubuntu
  26.04) and `null` elsewhere. `windows` (`release`, `version`,
  `service_pack`, `edition`) and `mac` (`version`) appear only on
  those systems, `null` elsewhere. Per-field guards keep it total —
  it never 500s on an unknown platform.
- Token required (like everything except help/index); other methods
  → 405.
- `filemanager.html` shows a one-line readout when the `system_info`
  capability is present; failures hide the panel silently (auxiliary).

## 16. Configuration (CLI > env > stdin > config.cfg > default)

- Configurable behaviors: `host`, `port`, `token`, `no_auth`.
  Resolution per key, highest priority first: CLI flag → `GNUMARUS_*`
  env var (`GNUMARUS_HOST/PORT/TOKEN/NO_AUTH`, empty means absent) →
  stdin key=value lines → `config.cfg` (fixed name, next to
  `server.py`) → default (`127.0.0.1` / `1423` / minted token /
  auth on). argparse defaults are None so "flag defined" is
  detectable; `--auth`/`--no-auth` form a tri-state pair so CLI can
  force auth either way.
- `config.cfg` / stdin format: UTF-8 `key = value` lines; `#`/`;`
  comments, `[section]` headers and unknown keys skipped; surrounding
  quotes stripped; last duplicate wins. `no_auth` accepts
  true/false/yes/no/on/off/1/0.
- Defensive reads: input containing a NUL byte anywhere or failing
  UTF-8 decode is ignored wholesale; at most 1 MB is read and the rest
  discarded; stdin is skipped when it is a TTY (interactive startups
  never block — pipe finite input). Invalid values fail fast with
  `config error: ...` on stderr and exit code 2 (never silently
  defaulted); empty token/host are rejected.
- Startup prints a flushed `config sources: host=.. port=.. token=..
  no_auth=..` line (origins: cli/env/stdin/file/default/generated/
  disabled) before binding, so precedence is observable even if the
  port is taken.
- `tests/test_config.py` (14 tests) spawns real servers: full
  precedence chain asserted via summary + HTTP behavior, no_auth via
  cfg, binary/oversize stdin+cfg handling, invalid port exit 2,
  empty-token rule; a pre-existing `config.cfg` is backed up/restored
  and `GNUMARUS_*` is scrubbed from child envs.

## 19. Tagged Scalars (no JSON numbers/booleans/nulls)

- Every JSON payload (requests and responses, all implementations)
  holds only strings, arrays and objects. Dynamic value fields use
  tagged strings: `i:<int>` (strict `-?(0|[1-9][0-9]*)`),
  `f:<float>` (finite, standard notation), `b:true`/`b:false`, and
  the bare string `"null"`. Fixed vocabulary (route/method names,
  type codes, encodings, error codes, messages) and free text
  (paths, contents, stdout, SQL, descriptions) travel raw; query
  parameters stay plain.
- Two fully-tagged positions (heterogeneous, typed per element):
  sqlite ROW CELLS and `params` VALUES — strings take an `s:` prefix
  there (`"s:ada"`, `"s:"` for empty), so `"s:i:5"` (text) never
  collides with `"i:5"` (int 5).
- Optional inputs: missing key == default; explicit `"null"` ==
  omitted. Strict per-field kinds: `max_*` require `i:`, `timeout`
  takes `i:`/`f:`, `readonly` takes `b:` — anything else is 400.
- Applies identically to Python/Kotlin/PHP and is enforced by the
  shared suite on every response (`assert_json_scalars` inside the
  request helpers, plus `tv()`/`ti()`/`tf()` for assertions);
  `filemanager.html` parses tags wherever it needs numbers/booleans
  (`tv`/`tnum`/`cellText`).

## 24. Native SQL Binding (no literal substitution)

- The five non-Rust ports used to interpolate decoded params into
  SQL text via hand-written literal substitution. All five now bind
  through the driver: JDBC `PreparedStatement`, PDO
  `prepare`/`execute`, node:sqlite `all`/`run` args, `database/sql`
  args, `SqliteCommand.Parameters`. Bools become 1/0 explicitly for
  Python parity (node:sqlite rejects bools outright).
- The string/comment-aware scanners remain ONLY where unavoidable:
  single-statement enforcement, rows-vs-writes classification (first
  keyword + RETURNING), and placeholder-style translation where the
  driver lacks a style (`:named`→`?` for JDBC and database/sql,
  `?`→`$pN` for Microsoft.Data.Sqlite, which rejects `?`). Those
  rewrites touch placeholder TOKENS only, never values.
- Rust was already native (`sqlparser` + rusqlite binding); its dead
  `sql.rs` scanner was deleted.

## 22. Go Port (`go/`)

- Same service, same contract: all 18 shared routes plus the five
  Gmail OAuth2 routes (`/api/oauth2/*`, see §36; 23 in total), same
  URLs/statuses/JSON
  shapes (tagged scalars included), same CLI flags, same config
  precedence (CLI > `GNUMARUS_*` > stdin > file > default; file
  search: working dir → executable dir), same token auth. One module
  dep: modernc.org/sqlite (pure Go, no cgo); everything else is
  stdlib (`net/http`, `encoding/json`, `os/exec` with `Setpgid` kill
  parity, `crypto`, raw `statx` syscall for birth/ctime).
- Faithful details: exclusive create via `O_EXCL`, stdin read capped
  at 1 MB with TTY skip, real bound params (positional + named),
  `PRAGMA busy_timeout`, `?mode=ro` on `file:` URIs (bare paths
  swallow query params into the filename!), DDL `rowcount` -1,
  tilde via `os.UserHomeDir()`, `/etc/os-release` parsing.
- Known impl differences (documented, not bugs): `machine` is
  `amd64` (Go `GOARCH`); no `python_version` (see `go_version`).
- Verified: shared suite 233/233 against it (14 OAuth tests included,
  plus the WS/TLS ones), headless `file://` smoke (Online, 12
  endpoints). The suite caught 1 real Go bug (readonly DSN without
  `file:` prefix silently wrote a file literally named `t.db?mode=ro`).

## 23. Rust Port (`rust/`)

- Same service, same contract: all 12 routes, same URLs/statuses/JSON
  shapes (tagged scalars included), same CLI flags, same config
  precedence (CLI > `GNUMARUS_*` > stdin > file > default; file
  search: working dir → executable dir), same token auth. Deps:
  serde_json (JSON), sqlparser 0.63 (statement count + DML/DDL/rows
  classification), rusqlite 0.40/bundled (real SQLite, one fresh
  connection per request), libc (setsid/killpg/gethostname). Std for
  the rest: hand-rolled HTTP/1.1 server (TcpListener + threads),
  strict base64, percent-decoding, UTF-8 validation, token via
  /dev/urandom.
- Faithful details: exclusive create via `create_new`, stdin read
  capped at 1 MB with TTY skip, `setsid` spawn + `kill(-pid,SIGKILL)`
  (killpg parity), stdin closed at once, real bound params
  (positional + named with `:` prefix), `busy_timeout` pragma,
  `mode=ro` flag, DDL `rowcount` -1, tilde via
  `HOME`/`USERPROFILE`, `/etc/os-release` parsing, birth/ctime via
  raw `statx` (`BTIME|CTIME` mask-checked, struct sizeof-asserted
  256; NUL-terminated path for the FFI boundary).
- Notable bugs found by the suite: rusqlite named params need the
  full placeholder (`:a`, not `a`); `?mode=ro`-style query strings
  avoided entirely via `OpenFlags` (no filename-mangling class).
- Environment notes: plain IPv6 is broken here, so the build forces
  `CARGO_NET_IPV4_ONLY=true` for crates.io; `libc 1.x` stable does
  not exist (only alphas) so the dep is `libc 0.2`; `bin/` is a
  gitignored build output (only sources + Cargo.toml/lock committed).
- Known impl differences (documented, not bugs): `machine` is
  `x86_64` (matches Python here); no `python_version` (see
  `rust_version`); dangling symlinks are skipped in listings.
- Verified: shared suite 126/126 against it, plus headless `file://`
  smoke (Online, 12 endpoints).

## 25. Launcher (`start.sh`)

- `./start.sh --runtime <name> [server args...]` starts any runtime
  (implementation): `python`, `kotlin`, `php`, `ruby`, `nodejs`, `go`,
  `dotnet`, `rust`. The selector is `--runtime`, not `--port`, so the
  name never shares a flag with the server's own TCP-port option.
- Only the FIRST `--runtime` selects the implementation
  (`--runtime=name` also works); everything else is forwarded
  verbatim — `--port` is reserved exclusively for the server's TCP
  port (e.g. `--port 1423`), alongside `--token`, etc. stdin is never
  touched by the script (`exec` hands the process over, so Ctrl+C,
  pipes and exit codes behave as if the server was started directly).
- Missing artifact (e.g. unbuilt `go/gnumarus`): prints which file is
  missing plus the exact build command for that runtime, then starts
  the python runtime as fallback (forwarded args kept). Unknown or
  missing `--runtime` prints usage and exits 2 with no fallback.
- The script `cd`s to the repo root, so relative paths resolve the
  same no matter where it is invoked from.
- Examples: `./start.sh --runtime go --token secret --port 1423`,
  `./start.sh --runtime=kotlin --port 8080`,
  `echo "token = t" | ./start.sh --runtime rust --port 1423`.

## 26. Windows launchers (`start.bat`, `start.ps1`)

- Same contract as `start.sh`: the first `--runtime` selects the
  implementation (`--runtime=name` too), `--port` means the TCP port
  and is forwarded like any other server arg; missing artifact prints
  build instructions + python fallback, unknown name exits 2.
- `start.bat` (cmd): ASCII-only file, backslash paths, per-port
  Windows artifacts (`go\gnumarus.exe`, `kotlin\...\gnumarus.bat`,
  `rust\bin\gnumarus.exe`, `ruby\vendor\gems`), interpreter checks
  via `where`,
  goto-based parsing (no delayed expansion, so `!` in args is safe;
  `&|<>()^` in args remain fragile, as in any batch forwarder),
  `&`-free hint texts (carets don't escape inside quoted `set`;
  `^<` escaping only valid outside quotes), no leading-`(` echoes.
- `start.ps1` (stock PowerShell 5.1): no PS7-only syntax (audited:
  no ternary, `??`, `?.`), splatting `@allArgs` (args with spaces
  forward exactly — better than batch), exit via
  `exit $LASTEXITCODE`. Run as
  `powershell -ExecutionPolicy Bypass -File start.ps1 --runtime <name>`.
- Ctrl+C on a batch-launched server shows cmd's "Terminate batch job
  (Y/N)?" prompt — inherent to batch wrappers, not fixable in-script.
- Verification: `start.bat` EXECUTED under wine 10 cmd (help exit 0,
  unknown/missing/no-value exit 2, full fallback chain incl. build
  hints; fixed 1 quoted-caret bug this exposed). `start.ps1`
  EXECUTED under pwsh 7.5.4 (help/unknown/missing/no-value exits,
  selection + exact forwarding, stdin passthrough, fallback chain,
  exit-code 42 propagation). PS 5.1 itself not run here —
  compatibility by syntax audit only; please smoke-test both on real
  Windows.
- The selector was renamed from the first `--port <name>` to
  `--runtime <name>` in all three launchers (the old flag collided
  with the server's own TCP `--port`, forcing the "place it first"
  caveat). Re-verified after the rename: `start.sh` boots
  python/kotlin/go with a forwarded `--port` (and the `--runtime=name`
  form), plus help/missing/no-value/unknown exit 0/2/2/2; `start.bat`
  under wine (help 0, missing/no-value/unknown 2, fallback 1);
  `start.ps1` under pwsh 7.6.6 (help 0, missing/no-value/unknown 2,
  fallback-hint chain 1).
- The `ruby` name was added to both launchers (and `start.sh`) later:
  interpreter check + `ruby\vendor\gems` artifact check, same
  fallback/hint contract. `start.ps1 --runtime ruby` was then executed
  on pwsh 7.6.6 (selection, arg forwarding and an HTTP 200 from the
  launched server verified on Linux); the `start.bat` ruby branch is
  by symmetry with the other ports and was not re-run under wine.
## 27. C++ Port (`cpp/`)

- Same service, same contract: all 12 routes, same URLs/statuses/JSON
  shapes (tagged scalars included), same CLI flags, same config
  precedence (CLI > `GNUMARUS_*` > stdin > file > default; file
  search: working dir → executable dir), same token auth. Vendored in
  `cpp/third_party`: nlohmann/json 3.12 (single header) + SQLite
  3.54.0 amalgamation (compiled in, zero system deps). Std C++20
  (`<filesystem>`, `<thread>`, lambdas) + POSIX sockets/threads.
  Build `./cpp/build.sh` (cmake `build/` + copy to
  `cpp/bin/gnumarus`, both gitignored), run `cpp/bin/gnumarus`.
- Faithful details: exclusive create via `O_EXCL`, stdin read capped
  at 1 MB with TTY skip, fork + `setsid` + `kill(-pid)` (killpg
  parity), stdin closed at once, real C-API binding
  (`sqlite3_bind_*`, both `?` and `:named` native — no translation
  needed), `busy_timeout`, readonly open flags, DDL `rowcount` -1,
  tilde via `HOME`/`USERPROFILE`, `/etc/os-release` parsing, real
  birth/ctime via raw `statx` syscall (256-byte ABI asserted).
- Bugs found by the suite: feeder missing `off += n` (infinite stdin
  loop, every stdin test timed out); SIGPIPE death when a command
  exits before the feeder writes (now `SIG_IGN` at boot);
  nlohmann `dump()` throws on invalid UTF-8 (shell stdout/stderr now
  sanitized like file text mode).
- Known impl differences (documented, not bugs): `machine` is
  `x86_64` (uname, matches Python/Rust); no `python_version` (see
  `cpp_version`).
- Verified: shared suite 136/136 against it, plus headless `file://`
  smoke (Online, 12 endpoints).

## 28. Transparent TCP Socket Proxy (`/api/socket/*`)

- Motivation: browsers cannot open raw TCP/UDP/unix sockets, so a
  browser page (e.g. an email client speaking SMTP/IMAP) cannot talk
  to network peers directly. The service holds a real TCP client
  socket per session and exposes a byte-transparent tunnel over
  plain HTTP: `POST /api/socket/open` dials and returns an opaque
  session id; `POST /api/socket/send?id=` writes bytes; `GET
  /api/socket/recv?id=[&timeout][&max_bytes]` long-polls for bytes;
  `POST /api/socket/close?id=` releases the session. A native
  WebSocket transport now exists (§29) on every port: each owns a
  hijackable HTTP layer (Go/Python/Node/Rust/C++/.NET/Ruby, plus
  Kotlin's hand-rolled `ServerSocket` server and PHP's ReactPHP
  `HttpServer`), so the raw connection can be handed to the bridge.
- Contract (help 12 -> 16 routes, `socket_open/send/recv/close`):
  open takes JSON `{host (raw string), port ("i:<1-65535>", `f:`
  rejected), timeout? (tagged number, default `i:5`, (0, 30])}` and
  answers 201 `{id, kind: "tcp", host, port}`; connect/DNS failures
  are 400 with the OS error text (same precedent as SQLite engine
  errors). send takes raw bytes (non-JSON Content-Type) or JSON
  `{content, encoding: text|base64}` (same body rules as file
  create) and answers `{id, bytes}`. recv answers `{id, encoding:
  "base64", content, bytes, eof, timed_out}`: data returns
  immediately; empty content + `eof` only after the peer's FIN is
  drained; empty content + `timed_out` when nothing arrives within
  `timeout` (plain query seconds, default 5, max 60;
  `max_bytes` plain 1-1048576, default 65536, validated before the
  session lookup like the other endpoints). close answers `{id,
  closed}`. Unknown/expired/closed ids are 404; missing id is 400;
  wrong methods are 405. Tagged scalars kept (`i:`/`b:`); query
  params stay plain. Same token auth as everything except
  help/index. Sessions: server-minted 128-bit ids (base64url),
  max 128 open (400 beyond), 300 s idle expiry (swept on open +
  background reaper), explicit close (shutdown + release).
  SECURITY: like shell + open CORS, this is an open network proxy;
  localhost bind is the only protection (WARNING in help text).
- Per-port notes: Python (`select` + `create_connection`, reaper
  thread); Node (event-buffered `net.Socket`, pause at 4 MB buffered,
  concurrent-recv guard); Go (`DialTimeout`, read/write deadlines,
  `TryLock` recv guard); Rust (`connect_timeout` over all resolved
  addrs, `try_clone` per op, busy flag); C++ (non-blocking connect
  + `poll`, per-call `poll` recv, `SIGPIPE`-safe writes);
  .NET (`ConnectAsync` + `CancellationTokenSource`,
  `ReadAsync` cancel maps to `timed_out`); Kotlin
  (`InetSocketAddress` connect timeout, `soTimeout` recv,
  `SocketTimeoutException` maps to `timed_out`); PHP owns sessions
  in `php/socketd.php` because `php -S` workers share no memory or
  fds — a supervisor (unix-socket IPC, spawned + readiness-pinged
  by `server.php`, `GNUMARUS_SOCKETD` exported to workers) that
  validates + connects inline and then forks one child per session
  (fd inherited across fork; child serves blocking ops sequentially
  on its own session socket and self-reaps on close/idle). Router
  forwards open to the supervisor and send/recv/close straight to
  the session child; daemon/session unreachable reads as 503
  (open) or 404 (unknown/expired id). Requires pcntl/posix,
  documented; `tests/test_socket.py` skips on 503 (never skips
  here). `server.php` also gained setsid process-group supervision
  (`exec setsid` + `kill(-pgid)`) so SIGTERM reaps `php -S`
  master+workers and supervisor+session children — killing just the
  master used to orphan workers holding the port (found via a ~240
  process leak during testing).
- `filemanager.html`: socket panel (host/port, Open/Close, send
  textarea, Receive with 5 s long-poll, EOF/timeout notes) gated by
  all four capabilities, plus a reusable `SocketSession` JS helper
  class (`open/sendText/sendBase64/recv/close`, base64 via
  `atob`, text via `TextDecoder`) documented with a copy-paste
  SMTP-style example in a code comment.
- Tests: `tests/test_socket.py` (22 tests: echo roundtrips
  text/binary/raw/256KB/empty, recv timeout, FIN EOF, interleaved
  sessions, close-then-404, refused/missing/bad host/port/timeout,
  unknown id, missing id, bad recv/send params, all 405s, 401s,
  help names + count). Verified: shared suite 158/158 on all 8
  ports (Python/Node/Go/Rust/C++/.NET/Kotlin/PHP), headless
  `file://` smoke (Online, 16 endpoints, socket panel visible).
  Bugs caught by the suite: Go double-`readBody` (body consumed
  twice, always 400); Node `f:` port accepted (now `i:`-only);
  PHP `fread` returning `false` (not `""`) on receive timeout
  (now mapped via stream metadata); wrong `--server-cfg` for
  Kotlin in the harness invocation (install-root cfg, not
  `kotlin/config.cfg` — a harness mistake, not a code bug).

## 29. WebSocket Transport (`GET /api/socket/ws?id=<id>`)

- Motivation: HTTP send/recv polling works everywhere but is not the
  best socket the browser API offers. On ports with a hijackable HTTP
  layer, `GET /api/socket/ws?id=<sid>` with `Upgrade: websocket`
  attaches a real WebSocket to an existing session (opened via POST
  /api/socket/open; token rides `?token=` since browsers cannot set
  WS headers).
- Contract (help 16 -> 17, `socket_ws`, every port): after the
  101 handshake, WS message payloads flow transparently client->TCP
  (text frames as UTF-8 bytes, validated: 1007) and TCP bytes flow
  back as WS binary frames; ping gets an auto-pong; fragmented
  messages reassemble (cap 4 MB, close 1009); unmasked client frames,
  RSV bits, bad opcodes and fragmented control frames fail with
  1002/1003. A close from either side, or any TCP/protocol error,
  ends the whole session (a WS close IS a session close, exactly
  like HTTP close): there is a single owner per TCP stream, so no
  detach handoff can lose or duplicate bytes (an earlier detach
  design died to exactly that race: a stale pump stealing post-
  detach bytes — found by the suite as a hang). HTTP send/recv on
  an attached session are refused with 400; HTTP close still works
  and kills the bridge. Non-upgrade GET -> 400; unknown id -> 404;
  missing id -> 400; bad key/version -> 400; double attach or
  in-flight HTTP recv at attach -> 400; other methods -> 405.
  Attached sessions skip idle expiry. Fail echoes go out on a clean
  FIN: bounded violations are fully consumed before echoing (only
  unbounded length declarations abort mid-stream), because
  close() with kernel-unread data resets and eats the echo.
- Per-port notes: Go (reference: hand-rolled RFC 6455 over
  `http.Hijacker`, stdlib sha1/base64, zero new deps; write mutex +
  close-once + 2 s grace force-close); Python (manual 101, takes
  over `self.connection`, pump thread + handler-thread reader,
  per-session recv lock shared with HTTP recv); Node (`upgrade`
  event, incremental parser over buffered data events, reuses the
  evented TCP buffer + waiter slot, single-write frames);
  Rust (upgrade intercept in the hand-rolled server, hand-rolled
  SHA-1, `try_clone`d peer/WS handles + write mutex, 2 s grace
  `shutdown(Read)`; `shutdown(Both)` — not close — is what
  unblocks cloned readers); C++ (hijack hook in `http_serve`,
  hand-rolled SHA-1, `namespace sockws` split out of the anonymous
  one so `main.cpp` can reach it, strict fd discipline: WS fd
  closes only in call_once cleanup, peer fd only in
  remove-if-present session end — raw `close()` twice could hit a
  reused number); .NET (`HttpListenerContext.AcceptWebSocketAsync`,
  verified working on Linux/.NET 10 — the feared Windows-only
  limitation does not apply; framework handles ping/pong;
  `SemaphoreSlim` write gate, 2 s grace `Abort`); Ruby (manual
  RFC 6455 in `ruby/lib/gnumarus/sock_ws.rb` over the hand-rolled
  `http.rb` TCP server, hand-rolled accept key); PHP (hand-rolled
  RFC 6455 in `php/src/WsProto.php` over the ReactPHP `HttpServer`,
  see §31); Kotlin (RFC 6455 ported from Go into
  `kotlin/src/main/kotlin/SocketWs.kt`; the hand-rolled `Http.kt`
  server hands the raw `Socket` to the bridge with write mutex +
  close-once + 2 s grace force-close — the JDK `HttpServer` was
  dropped precisely because it exposes no raw socket for the
  same-URL upgrade).
  Every port now advertises `socket_ws` (help 18 routes; 23 on the Go
  reference once the Gmail OAuth2 routes, §36, are counted), so the
  suite's `socket_ws` capability probe no longer skips anywhere.
- `filemanager.html`: `SocketSession.openWs(host, port, hooks)`
  (open-then-attach, token from the stored header, native
  `WebSocket`, `onMessage({bytes, text})` / `onClose`) with
  `sendText`/`sendBase64` routing over the socket when attached;
  panel gains a WebSocket checkbox (enabled only with the
  capability, falls back to HTTP polling), push-append log and a
  disabled Receive button in WS mode.
- Tests: `tests/test_socket_ws.py` (stdlib raw client: masked
  frames, fragmentation, reassembly; 16 client tests: echo
  binary/text/fragmented/256KB, ping/pong, EOF->close 1000,
  close-ends-session (404 after echo, deterministic via
  delete-before-echo), HTTP-refused-while-attached x2,
  HTTP-close-kills-WS, unmasked->1002, unknown id 404, missing id
  400, no token 401, plain GET 400, POST 405, help names+count).
  `test_help_cors` accepts the optional 17th name; `recv_all`
  fails fast after 3 consecutive empty timeouts instead of hanging.
  Verified: the full suite runs the whole WS module on every port
  (214/214 with zero skips on Go and on Kotlin, and green on
  python/nodejs/rust/cpp/dotnet/php/ruby). Headless `file://` smoke:
  Online (18 endpoints), socket panel + WS checkbox visible.
  Live-browser WS
  could NOT be verified here: both headless Chromes in this
  container fail even a trivial echo WS (environmental: real-time
  sockets under the headless clock); protocol interop is proven by
  the raw-client suite plus a byte-identical Chrome-handshake
  replay (101 + echo OK). Please click through the socket panel in
  a real browser.
- Bugs caught by the suite: close-echo-before-delete race (fixed
  by delete-before-echo in Go/Python, mirrored everywhere); an
  unmasked frame has no mask key (drain must be conditional —
  found as a hang in Rust, then fixed in Go/Python too);
  close-with-unread-data RST eating the echo (fixed by
  drain-then-echo); `Host: x` in the test client (HttpListener
  prefix matching needs the real host); bodyless POST 411 on
  HttpListener (test now sends `Content-Length: 0`); a stale-pump
  design flaw (detach semantics replaced by close-wins).

## 30. Loopback-Only Bind + Host Allowlist

- Two mechanisms, both loopback-only. (1) `--host` accepts just
  `localhost` (any case), strict dotted quads in `127.0.0.0/8` and
  `::1` (brackets tolerated); anything else (`0.0.0.0`, LAN IPs,
  hostnames) fails fast with `config error: host ... must be a
  loopback address ...` and exit code 2, before listening. (2)
  Every request's `Host` header must name loopback the same way
  (port part ignored but required numeric when present; `[::1]`
  bracket form accepted; bare IPv6 rejected); violations get
  `403 {"error":"forbidden",...}` with CORS headers, checked first
  — before auth (bad Host + no token is 403, not 401), on every
  method and route including OPTIONS, open routes and the 9 WS
  upgrade paths (every port serves `socket_ws`).
- The literal rule is textual on purpose: stdlib IP parsers diverge
  (`IsLoopback` accepts IPv4-mapped IPv6, `127.1` shorthands, etc.
  on some stacks), so every port implements the same strict
  grammar instead of its native parser. What it stops: name-based
  reaching (`localhost` variants are allowed by default, rebound
  domains and LAN names are not). What it does NOT stop: direct
  `http://127.0.0.1` URLs from hostile pages (Host matches!) and
  non-browser clients (Host is forgeable) — the token remains the
  real protection there; this is defense in depth, most valuable in
  `--no-auth` mode.
- Per-port notes: Go (reference: `hostAllowed`/`bindAllowed` in
  api/config, gate split into `checkHost` so OPTIONS is covered);
  Python (`python3/hosts.py` shared by handler gate incl.
  `do_OPTIONS` + config); Node (`nodejs/src/hosts.ts`, gate at the
  top of `handle()` + WS upgrade path); Rust (`rust/src/hosts.rs`,
  gate first in `check_auth` + OPTIONS branch); C++
  (`cpp/src/hosts.{h,cpp}`, gate first in `check_auth` + OPTIONS,
  plus a `getaddrinfo` fallback in `http_serve` since `inet_pton`
  cannot resolve `localhost`); .NET (`CheckHost` + `BindAllowed`,
  gate first in `CheckAuth` + OPTIONS) behind a new internal
  reverse-proxy shim (`dotnet/Proxy.cs`): HttpListener 404s
  off-prefix Hosts itself before user code runs (and a wildcard
  prefix would bind all interfaces), so a loopback `TcpListener`
  frontend now parses each head, enforces the allowlist with our
  own 403 JSON, and forwards byte-transparent to an ephemeral
  backend HttpListener (Host rewritten, `Connection: close`
  forced except upgrades, bodies re-framed, 1xx/101 handled);
  Kotlin (`hostAllowed`/`bindAllowed` top-level, gate first in
  `checkAuth` + OPTIONS); PHP (`g_host_allowed`/
  `g_bind_allowed` in `Config.php`, gate first in
  `GApi::checkAuth` + OPTIONS branch).
- Tests: `tests/test_host.py` (12 tests: allow matrix incl.
  case/`127.0.0.2`/`[::1]`/ignored-port, deny matrix incl.
  trailing-dot/empty/missing/`0177…`, 403-before-401, POST without
  side effects, OPTIONS deny, WS-upgrade deny, bind accept/reject
  via real sub-servers) + `test_config.py` bind cases. Verified:
  green on every port; the WS-upgrade deny now runs everywhere too
  (214/214 with zero skips on Go and Kotlin).

## 31. PHP on ReactPHP (no more php -S)
- The PHP port no longer uses `php -S` workers: `php/server.php`
  is a single long-lived CLI process on a ReactPHP event loop with
  a manual HTTP/1.1 server (`php/src/HttpServer.php` on
  `react/socket`, one request per connection, `Connection: close`;
  heads capped at 1 MB, bodies at 128 MB, chunked bodies
  de-chunked). Same 17 routes as the WS ports — PHP gains
  `socket_ws` (hand-rolled RFC 6455 in `php/src/WsProto.php`:
  incremental decoder, fragmentation, 4 MB cap, ping/pong,
  delete-before-echo, 2 s grace on peer EOF, 1011 on dead TCP).
- `php/router.php` and `php/socketd.php` are deleted; socket
  sessions live in memory (`php/src/SocketStore.php`: async
  connect via `Connector`, data-event buffering, timer long-poll
  recv, 128-session cap, 300 s idle sweep, sticky EOF, one pending
  recv, HTTP refused while attached). Shell runs async via
  `react/child-process` (`exec setsid`, stdin feed, streaming
  caps, `kill(-pid)` timeout with rc 124) so long commands never
  stall the loop; sqlite stays blocking pdo_sqlite (short local
  queries, documented). Handlers take `GRequest` and return
  `GResponse` (or a Promise for shell/socket-open/recv) in
  `php/src/Http.php` — no SAPI globals anywhere.
- Contract preserved byte-for-byte: same CLI flags, config
  precedence and boot stdout (the harness parses it), Host gate
  first (incl. OPTIONS and WS), token auth, CORS, tagged scalars;
  `G_VERSION` fixed 0.9.0 -> 0.10.0 (stale since the tagging
  iteration). Build is `./php/build.sh` (`composer install`;
  `php/vendor/` gitignored, `composer.lock` committed);
  `start.sh` checks the autoload file. ReactPHP resolves bind
  names itself, so `localhost` is resolved to 127.0.0.1 at bind
  (config still only accepts loopback).
- ReactPHP lessons (apply to any port on this stack):
  `Connection::write()` false is backpressure (64 KB soft limit),
  not failure; `close()` drops the write buffer (use `end(frame)`
  before FIN); the session store must not close the bridge-owned
  WS connection (delete-before-echo); `SocketServer` needs literal
  IPs; recv param validation runs before session lookup (Go order).
- Verified: 186/186 (+1 crtime skip) x3 stable runs vs PHP
  (the blob flake seen once is the known pre-existing one,
  green on re-run); `start.sh --runtime php` smoke-tested (17
  routes, `socket_ws` advertised).

## 32. Showcase Filemanager (`showcase/filemanager/`)
- Pure HTML/CSS/JS file manager (index.html + styles.css + app.js,
  no build, no external URLs — works from file:// offline), using
  ONLY the low-level webservice functions: GET /, GET /api/help
  (capability gate on `read_directory`), GET /api/dir (breadcrumbs
  + table with per-entry type/size/atime/mtime/ctime/crtime/mode),
  GET /api/file (text preview, raw=1 download), POST/PUT/DELETE
  /api/file (create/save-as-overwrite/delete), DELETE /api/dir
  (empty only), GET /api/sysinfo (header line). Tagged scalars
  parsed client-side (`tv()`); server+token bar persisted to
  localStorage; default target http://127.0.0.1:1423
  (`./start.sh --runtime go`).
- Verified: tests/test_showcase_filemanager.py (static purity +
  node --check + end-to-end app flow vs Go) — 192/192 with the
  backend suite; headless Chromium smoke Online (27 root entries
  rendered, zero JS errors).

## 33. Showcase Email Client (`showcase/emailclient/`)
- Pure HTML/CSS/JS read-only IMAP inbox (index.html + styles.css +
  app.js, no build, no external URLs), using ONLY the socket proxy
  (+ /api/help for the socket_ws cap): POST /api/socket/open,
  WS attach preferred with HTTP send/recv long-poll fallback,
  POST /api/socket/close, LOGOUT on disconnect. Two authentication
  modes: Password (`LOGIN`) or Google OAuth2 (`AUTHENTICATE
  XOAUTH2`, see §36 — the page calls POST /api/oauth2/start,
  GET /api/oauth2/poll, POST /api/oauth2/refresh and POST
  /api/oauth2/close and never sees a Google URL); as soon as the
  poll reports the consent is done the page connects by itself
  (`autoConnectAfterOAuth`, only when host+user are filled and no
  session is open), so a Google sign-in lands on the inbox exactly
  like a password Connect. The Auth mode drives the Provider:
  picking Google OAuth2 selects the Gmail preset and fills
  `imap.gmail.com:993` at once, so the auto-connect can never aim at
  a leftover preset (the Proton Bridge default `127.0.0.1:1143` was
  the trap); a saved OAuth2 mode is aligned the same way on load,
  except for a deliberately chosen Custom host, and the Provider
  select itself is persisted. The sign-in popup is closed by the
  service's callback page (which scripts its own `window.close`), not
  by the opener, so Chrome logs no COOP warning. Provider presets
  (gmail/outlook/icloud/yahoo/proton-bridge + custom host/port);
  read-only EXAMINE INBOX (never SELECT), paginated UID
  listing (UID SORT REVERSE DATE with UID SEARCH fallback),
  BODY.PEEK header fetch per page, BODY.PEEK[] open with a MIME
  walk (text/plain preferred, text/html stripped, attachments
  listed by name only). No send/delete/flag/store commands exist
  anywhere in the code. Literal-aware byte-buffer framing
  ({n} counts bytes, latin1 lines, per-part decode: base64,
  quoted-printable, RFC2047, charset via TextDecoder).
- Server-shape tolerance (the UID/SORT/FETCH parsers read the whole
  response, not a fixed line): the untagged `* SORT`/`* SEARCH` line
  is picked from ANY line (Gmail piggybacks an unsolicited
  `* CAPABILITY` on the AUTHENTICATE completion), an OK-but-empty
  SORT falls back to `UID SEARCH ALL` just like a BAD one (Gmail
  advertises no SORT capability), and the per-message `UID` token is
  read wherever it appears in the FETCH record (attribute order is
  not guaranteed), so a UID placed after the header literal still
  yields a row. Command failures surface the verb only —
  `IMAP error (UID SEARCH): ...` — never the LOGIN password or the
  XOAUTH2 token.
- XOAUTH2 details: the SASL initial response is
  base64(`user=<addr>\x01auth=Bearer <token>\x01\x01`) sent on the
  AUTHENTICATE line; on rejection the server sends a `+ <error>`
  continuation and the client answers an empty line before the
  tagged NO (readResponse grew an `onPlus` hook exactly for that).
  The token is refreshed through the service when older than ~50 min.
- Honest TLS scope (shown in-app): a raw TCP proxy cannot
  terminate provider TLS in pure JS (no CA store, no verifiable
  handshake — passwords would be exposed), so TLS presets carry a
  bridge notice (local stunnel/Bridge forward + Custom, or Proton
  Bridge at 127.0.0.1:1143 which is plaintext natively); TLS
  garbage fails fast at the greeting with guidance.
- Verified: tests/test_showcase_emailclient.py (static purity +
  route/verb allowlists incl. preset hosts and the XOAUTH2 verb,
  node --check, a node harness for the SASL payload + continuation
  abort + the SORT/SEARCH/FETCH parsers + autoConnectAfterOAuth +
  the Auth->Provider alignment against Gmail-shaped inputs, scripted
  fake IMAP server through the
  Go proxy over BOTH HTTP and WS: greeting/LOGIN/EXAMINE/
  SORT-fallback/SEARCH/literal FETCH/LOGOUT, plus AUTHENTICATE
  XOAUTH2 success and the rejection-continuation abort, plus a
  Gmail-shaped session with no SORT and an unsolicited CAPABILITY
  after AUTHENTICATE) — 233/233 with the backend suite;
  headless smoke (6 providers, Proton note, zero JS errors); a second
  headless pass drives the real `change` listener to prove the
  Auth->Provider alignment and re-runs the end-to-end page load
  (fake IMAP -> Go proxy -> 2 rows rendered, no JS errors).

## 34. TLS Socket Sessions (all ports)
- TLS is a transport property of a socket session (kind tcp|tls),
  not a parallel API: POST /api/socket/open accepts tls (b:),
  sni (raw string, default host) and ca_pem (raw PEM bundle
  appended to the system roots); POST /api/socket/starttls?id=
  upgrades a plaintext session in place for STARTTLS-style
  protocols (optional JSON {sni?, ca_pem?}, server name defaults
  to the session host). Both paths verify against the system
  store with hostname check and SNI (TLS 1.2 floor); failures
  are 400 with the engine error text. No insecure flag exists.
- Semantics: send/recv/close/WS operate on the cleartext stream
  unchanged; a failed open-handshake creates no session, a
  failed starttls leaves the plaintext session untouched (still
  closable); starttls is refused while WS-attached, while a recv
  is pending, or when already TLS (400s). Help 17 -> 18
  (`socket_starttls`) on every port
  (go/rust/python/nodejs/dotnet/php/ruby/kotlin/cpp); now that Kotlin
  also owns its TCP layer and serves `socket_ws`, the
  STARTTLS-while-WS-attached refusal is exercised on all of them.
- Per-port TLS stacks: go crypto/tls stdlib (reference), rust
  `openssl` crate (shared Arc<Mutex> stream ends + 100 ms pump
  polling so a blocked TLS read never pins the bridge), python
  `ssl` stdlib (VERIFY_X509_STRICT cleared to match the
  reference verifier), nodejs `node:tls` (`ca` = bundled roots +
  extra, data listener detached across STARTTLS), kotlin JVM
  `SSLSocket` + KeyStore from JRE cacerts + manual RFC 6125
  hostname check (the default verifier rejected the fixture,
  IP SAN included), dotnet `SslStream` + custom chain
  (CustomRootTrust over system roots + extra, NoCheck) + manual
  SAN parse (context-specific tags!), php `tls://` Connector
  for implicit + blocking `stream_socket_enable_crypto` for
  STARTTLS (+ buffered-bytes flush at WS attach), C++ system
  OpenSSL (`SSL_set1_host`, pending-aware recv, `MSG_NOSIGNAL`
  preserved on the plaintext path).
- Verified: tests/test_socket_tls.py (12 tests, local PKI via
  openssl into ./tmp, skip if absent): help advertisement,
  direct-TLS echo roundtrip, CA-required negative, SNI mismatch,
  TLS-against-plaintext, open validation 400s, STARTTLS happy
  path incl. post-upgrade echo, already-TLS, failed-upgrade
  keeps session, unknown/missing id, 405, WS-attached refusal.
  Green vs every port (the suite now asserts
  Sec-WebSocket-Accept too, so the handshake GUID cannot rot
  again). The emailclient TLS presets now open with
  tls:true (plaintext presets unchanged); its TLS notice says
  verification happens in the proxy service.

## 35. Ruby Port (`ruby/`)
- Scope: a full-parity Ruby implementation of the service (all 18
  routes, incl. `socket_ws` and `socket_starttls`), same CLI flags,
  same config precedence, byte-identical help catalog and 400 texts.
  Ruby 3.3 stdlib only (`socket`, `openssl`, `json`, `fiddle`,
  `securerandom`, `base64`, `etc`); the single gem (`sqlite3`) is
  installed into `ruby/vendor/gems` by `ruby/build.sh` (gitignored
  like `php/vendor/`, `node_modules/`, `vendor/`).
- Layout: `ruby/server.rb` (CLI + config + accept loop) and
  `ruby/lib/gnumarus/{tags,util,http,routes,fsops,shell,sysinfo,
  sqlite,sock,sock_ws,api,config,statx}.rb`. `Gem.paths` is pointed at
  `ruby/vendor/gems` at boot (default gems stay resolvable), so a
  missing driver degrades to the usual 503 instead of breaking boot.
- HTTP: hand-rolled HTTP/1.1 over `TCPServer#accept` (one request per
  connection, `Connection: close`, `Content-Length`), because the WS
  route needs to hijack the socket exactly like Go/Python/PHP. Bodies
  are read by `Content-Length` (no chunked encoding). CORS, reason
  phrases, `label()` and the `Connection: close` header are emitted on
  every response (including errors and the 204 preflight).
- Dispatch: Host allowlist first (403 before auth and before any side
  effect), then `OPTIONS` (204, no auth), then token auth (`GET /` and
  `GET /api/help` stay open; Bearer header preferred, `?token=`
  fallback, constant-time compare), then method+path routing with the
  same 404 `Unknown route: <path>` / 405 texts as the reference.
  `/api/help` returns `{version, count (i:), routes, auth:{required
  (b:), open_routes, how}}`.
- Config: `--host/--port/--token/--no-auth/--auth` (plus `--flag=value`
  and `-h/--help`), `GNUMARUS_*` env, stdin `key=value` lines (skipped
  when stdin is a TTY), then `config.cfg` from the working directory or
  the script's directory — the second candidate is what makes
  `--server-cfg ruby/config.cfg` work when launched from the repo root.
  Binary/invalid-UTF-8 input is ignored wholesale; config errors print
  `config error: ...` and exit 2; the flushed `config sources: host=...
  port=... token=... no_auth=...` line matches the other ports.
- Wire format: `Tags.jenc` (`i:`, `f:` with Go `strconv.FormatFloat(f,
  'f', -1, 64)` semantics, `b:`, `null`), `Tags.jdec` with the same
  strict `INT_RE`/`FLOAT_RE` as Go, and `Tags.jtagstr` (`s:`) for sqlite
  row cells/params. `Util.json_generate` retries with a recursive scrub
  when a string carries invalid UTF-8 (Go replaces with U+FFFD).
- Filesystem: directory listing sorts names and reports
  `size/atime/mtime/ctime/crtime/mode` (real `ctime` from
  `File::Stat#ctime`; `crtime` from `statx`), create uses
  `O_CREAT|O_EXCL` (409 when present), overwrite is `File.binwrite`,
  delete-dir is `Dir.rmdir` (ENOTEMPTY → `Cannot delete directory:
  Directory not empty: '<abs>'`), and the "directory, not a file" gate
  mirrors the reference (a symlink to a directory is allowed through).
  Birth time: `File::Stat#birthtime` raises `NotImplementedError` on
  this Ruby/glibc build, so `statx.rb` calls libc `statx` through Fiddle
  with `STATX_BTIME` (256-byte struct, btime at offset 80, same ABI the
  Rust/C++/C# ports read) and returns nil when unavailable.
- Shell: `Process.spawn("bash", "-c", command, chdir:, in:, out:, err:,
  pgroup: true)`, a writer thread for stdin, two capped drain threads
  (`readpartial` so output is never pinned waiting for a full buffer),
  `waitpid2` in a thread with a timeout, then `Process.kill("KILL",
  -pid)` (process group) and returncode 124. Truncation flags and the 1
  MB default / 100 MB hard cap match the reference.
- SQLite: `sqlite3` gem (`execute2` for row statements,
  `execute` + `changes`/`last_insert_row_id` for writes, `busy_timeout`,
  `readonly:` option), one fresh connection per request (stateless), a
  full port of the Go single-statement scanner (`scan_sql`), the named
  placeholder rewriter (`:name/@name/$name` → `?`, `?` with object
  params rejected) and tagged param binding. BLOB cells (ASCII-8BIT)
  are base64-encoded with an `s:` prefix, INTEGER/REAL/NULL/TEXT map to
  `i:`/`f:`/`null`/`s:`. DDL first words report `rowcount: -1`.
- Sockets: session map + mutex (max 128, 300 s idle sweeper in a
  background thread, `SecureRandom.urlsafe_base64(16)` ids), nonblocking
  connect/read/write with deadlines (`Socket.tcp(connect_timeout:)`,
  `read_nonblock(exception: false)` + `IO.select`), `Connection: close`
  HTTP semantics. `recv` serializes per session with `Mutex#try_lock`
  (400 `Another recv is already pending on this session.`) and keeps
  the `eof` flag sticky.
- TLS: `OpenSSL::SSL::SSLSocket` with `VERIFY_PEER` against
  `X509::Store#set_default_paths` plus any `ca_pem` certificates
  (zero parsable certs → `Field 'ca_pem' must be a valid PEM CA
  bundle.`), TLS 1.2 floor, SNI only for non-IP names, and a manual
  RFC 6125 check that parses `subjectAltName` (`DNS:` and `IP Address:`
  entries, CN fallback only when no DNS SANs). The manual check is
  required: Ruby's built-in `verify_hostname` does not match IP SANs,
  and the test fixture signs `DNS:localhost, IP:127.0.0.1`.
  `starttls` builds a new `SSLSocket` over the live `Socket` with
  `sync_close = false`, so a failed handshake leaves the plaintext
  session usable (the raw socket is never closed by the failed layer).
- WebSocket: `GET /api/socket/ws?id=` checks the upgrade headers and
  version 13, probes the per-session recv mutex, marks the session
  `ws_attached`, writes the 101 with a hand-rolled
  SHA-1/base64 accept key, then runs two pumps: client frames →
  TCP payloads (text/continuation rules, UTF-8 validation, inline pong,
  masking/RSV/control-frame checks, 4 MB message cap) and TCP bytes →
  binary frames. A WS close is a session close; HTTP close of an
  attached session ends the bridge too.
- Two Ruby-specific hazards found while testing (and fixed):
  1. `OpenSSL::SSL::SSLZeroReturnError` does not exist in this
     ruby/openssl build — naming it in a `rescue` clause raised
     `NameError` *while handling* the real `IOError`, masking the close
     of an HTTP-closed session. The rescue now catches `SSLError` and
     maps "unexpected eof" to `eof`.
  2. `IO.select` on an IO closed by another thread is not reliably
     interrupted, so the TCP→WS pump reads with a 250 ms timeout and
     checks `Sock.live?` (session still mapped and transport open)
     between reads; that is what turns an HTTP close into the 1011 WS
     close frame.
- Verified: `python3 tests/run_all.py --server-cmd "ruby ruby/server.rb"
  --server-cfg ruby/config.cfg` → 214 tests OK, 0 skipped (all 12 TLS
  tests exercised), three consecutive runs green; sqlite probe reports
  "yes"; the 503 fallback was verified by moving `ruby/vendor/gems`
  aside. `./start.sh --runtime ruby` launches it (falls back with build
  instructions when the interpreter or the vendored gem dir is
  missing), and `--runtime ruby` was wired into `start.bat`/`start.ps1`
  too (`start.ps1` re-verified on pwsh 7.6.6: selection, forwarding
  and an HTTP 200).

## 36. Gmail OAuth2 Service Helper (`/api/oauth2/*`, Go reference)
- Why it lives in the service: a browser page cannot finish Google's
  OAuth flow on its own — the consent page sends no CORS headers and
  the token exchange needs a client secret (Desktop-app clients are
  public, but Google still rejects a secret-less exchange). The local
  service acts like a native desktop client instead, so the showcase
  stays offline-pure and the secret + refresh token never reach the
  page. Implemented on all nine ports (Go reference plus Rust, Python,
  Node.js, PHP, Ruby, Kotlin, .NET and C++).
- Five routes (Go: `go/oauth2.go` + `go/routes.go` + `go/api.go`; Rust: `rust/src/oauth2.rs`; Python: `python3/handler_oauth2.py`; C++: `cpp/src/api_oauth2.inc`; and equivalents on the other ports):
  - `POST /api/oauth2/start` (oauth2_start) — body
    `{client_id, client_secret?, auth_url?, token_url?, scope?}`
    (defaults: `https://accounts.google.com/o/oauth2/v2/auth`,
    `https://oauth2.googleapis.com/token`,
    `https://mail.google.com/`); returns 201
    `{id, auth_url, redirect_uri, scope}`. It mints a random state and
    a PKCE `code_verifier` (32 random bytes) with an S256 challenge,
    and appends `response_type=code`, `access_type=offline` and
    `prompt=consent`. The redirect URI is built from the request Host
    (`http://<Host>/api/oauth2/callback`), i.e. the loopback the
    browser is already using.
  - `GET /api/oauth2/callback?code=&state=` (oauth2_callback) — the
    provider's redirect target; an **open route** (Google cannot send
    our token; the random state is the bearer of trust, matched
    against the pending sessions). It exchanges the code for tokens
    server-side with `client_secret` + `code_verifier`, marks the
    session authorized, and always answers a small self-contained
    HTML page (escaped, no external resources). A second delivery of
    the same callback is a no-op ("already processed") so the code is
    never replayed.
  - `GET /api/oauth2/poll?id=` (oauth2_poll) — `{id, status}` where
    status is `pending|exchanging|authorized|error`; on `authorized` it
    adds `{access_token, token_type, expires_in, scope}`, on `error`
    `{error}`. The refresh token is deliberately never exposed.
  - `POST /api/oauth2/refresh?id=` (oauth2_refresh) — mints a fresh
    access token from the kept refresh token (400 before the first
    authorization).
  - `POST /api/oauth2/close?id=` (oauth2_close) — drops the session.
- Sessions mirror the socket proxy: opaque ids, 64 max, 600 s idle
  sweep (background ticker), lookup bumps the idle timer; unknown ids
  are 404 and a missing id is 400, matching the rest of the service.
- Why "Desktop app" at Google: that client type accepts the loopback
  redirect `http://127.0.0.1:<port>/...` on any port **without**
  registering a redirect URI, which is exactly a local service's
  shape. The exchange is `POST application/x-www-form-urlencoded`
  (1 MB response cap, 20 s timeout) and errors surface the provider's
  `error_description`.
- Google setup for the user: create a project, an External/Testing
  consent screen with the `https://mail.google.com/` scope and the
  Gmail address as a test user, then a **Desktop app** client id +
  secret; paste both into the showcase. (Testing mode shows the
  unverified-app warning and expires refresh tokens after 7 days.)
- Tests: `tests/test_oauth2.py` (capability-gated) runs the whole flow
  against a local fake Google that verifies the PKCE S256 verifier,
  the code and the redirect URI, plus the validation/error/404/405/
  idempotency/refresh paths; `tests/test_help_cors.py` treats the five
  names as optional so non-Go ports still pass. Verified: 233 tests OK
  vs every port (14 OAuth tests exercised on each).
