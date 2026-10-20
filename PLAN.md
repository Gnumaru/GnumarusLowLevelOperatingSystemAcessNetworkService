# PLAN — Gnumarus Python Native Operations HTTP Service

## Goal
Build a Python 3 CLI app that starts a web server exposing a REST HTTP API for
low-level operations on the host machine (filesystem, shell, etc.).
Initial version scope (this iteration): read operations only —
list directories + read files (text and binary) — plus `help` endpoint and
open CORS.

## Constraints (from AGENTS.md)
- Language of code/comments/docs: English. Conversation with user: Portuguese.
- Linux Ubuntu 26.04, Wayland/GNOME, bash, no root. No `pip` available —
  therefore implementation must use Python 3 stdlib ONLY (no Flask/FastAPI).
- Never use `/tmp`; use local `./tmp` for temp artifacts.
- Never use subagents; work in main session.
- Never use git write operations (no checkout/commit); only read (log/status).
- Keep `PLAN.md` + `CHECKLIST.md` updated during work.
- Keep `FEATURES_SUMMARY.md` + `FEATURES_DETAIL.md` updated.
- Verify solution by execution (run server + real HTTP tests).

## Architecture Decision
- Single-file server `server.py` (stdlib only):
  - `http.server.ThreadingHTTPServer` + custom `BaseHTTPRequestHandler`.
  - `argparse` CLI: `--host`, `--port`.
  - JSON everywhere (`application/json; charset=utf-8`), except optional raw file bytes.
  - Open CORS: `Access-Control-Allow-Origin: *` + full OPTIONS preflight handling.
  - No auth/sandbox in v1 (documented as intentional; localhost use).

## API Design (v1 — read + write + delete)
- `GET /api/help` — list of all routes with url, name, description, params.
- `GET /api/dir?path=<abs-or-relative-path>` — list directory entries.
  - Response: `{ path, absolute_path, count, entries: [{ name, path, type, size, mtime, mode }] }`.
  - Errors: 400 missing path, 404 not found, 400 not-a-directory, 403 permission.
- `GET /api/file?path=<file-path>[&encoding=text|base64|auto][&raw=1]`
  - (unchanged, see FEATURES_DETAIL.md)
- `POST /api/file?path=<path>` — create file (empty if no body).
  - Body: optional JSON `{ content, encoding: text|base64 }` (default text)
    or raw bytes (any non-JSON Content-Type).
  - Success: 201 `{ path, absolute_path, size, mime_type }`.
  - Errors: 400 missing path / bad body, 404 parent dir missing,
    409 already exists, 403 permission.
- `PUT /api/file?path=<path>` — overwrite file content (same body rules).
  - Success: 200 `{ path, absolute_path, size, mime_type }`.
  - Errors: 400 missing path / is-a-directory / bad body, 404 not found,
    403 permission.
- `DELETE /api/file?path=<path>` — delete a file.
  - Success: 200 `{ path, absolute_path, deleted: true }`.
  - Errors: 400 missing path / is-a-directory, 404 not found, 403 permission.
- `DELETE /api/dir?path=<path>` — rmdir, empty directories only.
  - Success: 200 `{ path, absolute_path, deleted: true }`.
  - Errors: 400 missing path / not-a-directory / not-empty,
    404 not found, 403 permission.
- `GET /` — small JSON index pointing to `/api/help`.
- `OPTIONS *` — CORS preflight, 204.
- All errors: JSON `{ error, message, path? }`.

## Stack Decision (confirmed)
- Keep stdlib only (no Flask/FastAPI): zero-install CLI is the better fit
  for a local native-ops tool; frameworks add install friction for little
  gain at this scale. Revisit only if the API grows much larger.

## Implementation Steps
1. Write `PLAN.md` (this file) + `CHECKLIST.md`.
2. Implement `server.py`.
3. Manual verification: start server on test port, exercise endpoints with
   `python3 -c http.client` / curl:
   - `/api/help`, `/` , CORS headers, OPTIONS.
   - `/api/dir` on `./tmp` fixture + error cases.
   - `/api/file` text file, binary file, base64 mode, raw mode, error cases.
4. Write `FEATURES_SUMMARY.md` + `FEATURES_DETAIL.md`.
5. Final `git status` (read-only) + report in Portuguese.

## Iteration 2 (write + delete + tests/)
6. Extend `server.py`: `POST /api/file` (create), `PUT /api/file`
   (overwrite), `DELETE /api/file`, `DELETE /api/dir` (rmdir, empty only).
7. Create `tests/` with stdlib-only scripts (helpers + run_all.py +
   test_*.py per resource). Runner starts the server on a free port,
   runs unittest discovery, shuts down. Every test uses an isolated
   workspace under `./tmp/test_workspace`.
8. Run `python3 tests/run_all.py` (full regression suite) and fix issues.
9. Update `FEATURES_SUMMARY.md` + `FEATURES_DETAIL.md`, report.

## Iteration 3 (default port + single-file web file manager)
10. Change default port 8080 → 1423 (`server.py` argparse + docs).
11. Create `filemanager.html` (single file, inline CSS+JS, no build step):
    - Talks to `http://127.0.0.1:1423`; works hosted anywhere and via `file://`.
    - On load fetches `/api/help`: offline → error banner + all operations
      disabled; online → capability set from route `name`s gates the UI
      (list/view/download/create/overwrite/delete shown only if present).
    - Features: path nav + breadcrumbs, dir listing, text view/edit,
      binary download, create text file, delete file / empty dir.
    - All errors rendered in the UI.
12. Verify: syntax-check embedded JS (node --check if available), run
    full regression suite, boot server on default port 1423 and exercise
    the same endpoints the page uses (help/dir/file) via HTTP.
13. Update docs, report.

## Iteration 4 (shell execution)
14. New endpoint `POST /api/shell` (exec_shell): JSON body
    `{ command, timeout?, cwd? }` run via `bash -c` with stdout/stderr
    captured on separate pipes; process-group kill on timeout.
    Response `{ command, cwd, returncode, stdout, stderr, timed_out }`.
    Capture semantics: the API sees what the shell process emits on its
    own fds, so in-command redirections apply (`> f` empties captured
    stdout, `2>&1` merges into captured stdout, last pipeline element
    decides the returncode — all plain bash semantics).
15. Security note: CORS * + shell exec means any visited website could
    call it; binds 127.0.0.1 by default, documented, auth/allowlist stays
    in Future.
16. Tests: new `tests/test_shell.py` (rc/stdout/stderr separation, pipes,
    redirections to file, last-element rc, timeout kill, error cases) +
    bump help count 8 → 9. Shell console panel in `filemanager.html`
    gated by the `exec_shell` capability.
17. Verify: full suite, live exercise on :1423 with the user's example
    commands, headless file:// smoke test, docs, report.

## Iteration 5 (shell output limits)
18. `POST /api/shell` gains `max_stdout` / `max_stderr` (bytes, int,
    default 1 MB, hard cap 100 MB → 400 beyond). Commands always run to
    completion; capture is a streaming drain that keeps only the first N
    bytes per stream (memory stays bounded even for GB outputs).
    Response adds `stdout_truncated` / `stderr_truncated` + echoes the
    limits, so clients can show "truncated at N bytes".
19. `filemanager.html` shell panel gains both fields (default 1048576)
    and shows truncation notes.
20. Tests: truncation of each stream independently, full execution
    despite truncation (side-effect file complete), defaults echoed,
    invalid/over-cap → 400. Full suite + live + headless, docs, report.

## Iteration 6 (SQLite)
21. `POST /api/sqlite/create?path=<path>` (sqlite_create): creates a new
    SQLite file, 409 if it exists, 404 if parent missing. 201 with
    `{ path, absolute_path, size, sqlite_version }`.
22. `POST /api/sqlite/query?path=<db>` (sqlite_query): body
    `{ sql, params?, timeout?, readonly? }`. One statement per request
    (multi-statement → 400). SELECT-style → `{ columns, rows,
    rowcount }` (BLOBs as base64); writes commit → `{ rowcount,
    lastrowid }`. Fresh connection per request, always closed. Busy
    timeout default 5 s. SQLite error text surfaced in 400 messages.
23. Tests: `tests/test_sqlite.py` (create +409/404, stateless
    multi-request roundtrip CREATE/INSERT/SELECT, bound params,
    blob→base64, all 400s/404/405) + help count 9 → 11. SQLite panel in
    `filemanager.html` gated by the new caps.
24. Verify: suite, live exercise on :1423, headless smoke, docs, report.

## Iteration 7 (token auth)
25. Auth: server mints a random token at startup (or `--token TEXT`),
    printed to stdout. Every route except `GET /` and `GET /api/help`
    (plus `OPTIONS` preflight) requires it via
    `Authorization: Bearer <token>` (preferred) or `?token=`.
    Constant-time compare, 401 JSON + `WWW-Authenticate: Bearer`.
    `--no-auth` disables the requirement explicitly (loud warning).
    `/api/help` gains an `auth` block describing the policy.
26. `filemanager.html`: token field (password input, localStorage),
    sent on every request; 401 → "unauthorized, paste the token" state
    (controls stay enabled so the user can fix + Retry).
27. Tests: helpers auto-attach `GNATIVE_TEST_TOKEN` (run_all mints and
    passes `--token`); new `tests/test_auth.py` (401s, Bearer/query
    token, help/index/OPTIONS open, `--no-auth` sub-server).
28. Verify: suite, live (401/token/no-auth), headless (401 UX path),
    docs, report.

## Iteration 8 (system info)
    (NOTE: the runtime-version field became port-specific later - each
    port reports its own runtime, native ones report none; see
    Iteration 40.)
29. `GET /api/sysinfo` (system_info, no params): `{ system,
    system_name, release, version, machine, processor, hostname,
    python_version, cpu_count, distro?, windows?, mac? }` via stdlib
    `platform` (+ `/etc/os-release` parsing on Linux). Token required
    like everything except help/index.
30. Tests: `tests/test_sysinfo.py` (structure, Linux distro fields,
    401, 405) + help count 11 → 12. Small sys readout in
    `filemanager.html` gated by the cap.
31. Verify: suite, live on :1423, headless smoke, docs, report.

## Iteration 9 (multi-source configuration)
32. Every behavior (host/port/token/no_auth) resolves CLI flag >
    `GNUMARUS_*` env > stdin key=value > `config.cfg` (fixed name,
    script dir, INI-style) > default. argparse defaults become None so
    "flag defined" is detectable; `--auth`/`--no-auth` tri-state pair.
33. Defensive reads: NUL-byte or non-UTF-8 input → whole source
    ignored; max 1 MB read, rest ignored; stdin skipped when TTY.
    Invalid values fail fast (exit 2), empty token/env rejected.
34. Startup prints `config sources:` summary (per-key origin, flushed).
    `run_all.py` + auth sub-server get `stdin=DEVNULL` so harness
    pipes can't block startup.
35. Tests: `tests/test_config.py` (full precedence chain via HTTP +
    summary, no_auth via cfg, binary/oversize stdin+cfg, invalid port,
    backup/restore of a pre-existing config.cfg).
36. Verify: suite, live, docs, report.

## Iteration 12 (Linux birth time via statx)
37. Measured: `stat` subprocess ~6.7 ms/file vs lstat ~4 us/file
    (~1600x), plus newline-in-filename breaks line parsing. Rejected
    per-entry subprocess and whole-dir `stat`+parse (fragile, plus
    BSD/macOS flag divergence — moot, they already have
    st_birthtime — and no `stat` on Windows).
38. Implemented `_birthtime()`: `st_birthtime` when present, else
    Linux-only ctypes `statx(AT_SYMLINK_NOFOLLOW, STATX_BTIME)` with
    `stx_mask` check; any failure (ENOSYS/ENOENT/odd struct size) →
    None. Verified numerically against `stat -c %W`, incl. negative
    case (/proc → mask lacks BTIME → None) and struct sizeof == 256.
39. Tests: `test_crtime_matches_stat` (skipUnless linux; compares with
    `stat`, expects None when Birth unknown). Help/docs reworded
    (crtime often present on Linux now). Suite + live + docs + report.

## Iteration 10 (~/ documented in help)
37. `~/` already worked everywhere via `os.path.expanduser` (verified
    live: list/create/read/delete under the server user's home).
    Documented it in the six file-endpoint `path` params in
    `/api/help`; added `test_tilde_expands_to_home` (read-only) and
    `TestTildePaths` (self-cleaning create/read/delete under
    `~/.gnumarus_tilde_test/`).
38. Verify: suite, docs, report.

## Iteration 11 (dir times + python3/ move)
39. Adapt to the move (`server.py` → `python3/server.py`): spawner
    paths (run_all, test_auth, test_config incl. `CFG_PATH`), doc
    commands, filemanager hint. `config.cfg` now resolves next to the
    script (`python3/`) automatically via `__file__`.
40. `GET /api/dir` entries gain `atime`, `ctime`, `crtime` (mtime kept):
    `crtime` from `st_birthtime` when the OS provides it, else null
    (Linux). Help response text documents all four times.
41. Tests: entry-keys assertion extended + `test_all_times_present`
    (utime-pinned atime/mtime, ctime float, crtime null-or-float).
42. Verify: suite, docs, report.

## Iteration 19 (Rust port in rust/)
43. Env: no toolchain -> rustup stable/minimal (user-level). User
    vetoed hand-written JSON/SQL parsers -> deps: serde_json 1.0.154
    (JSON), sqlparser 0.63 (statement count + classification),
    rusqlite 0.40/bundled (real SQLite, native binding), libc
    (setsid/killpg/gethostname). Everything else std (TcpListener
    HTTP/1.1, threads, token via /dev/urandom).
44. rust/{Cargo.toml,.cargo/config.toml,build.sh,src/*.rs}: all 12
    routes, token auth, CORS, multi-source config, shell with
    streaming caps, sysinfo, tilde, tagged scalars. Build config:
    target-dir ~/.rust_target_dirs/<PascalCase>/ + build.sh copies
    the release binary to rust/bin/GnumarusOSBridge.
45. Verified: shared suite 126/126 vs Rust, headless file:// smoke
    (Online, 12 endpoints). Parity bugs fixed: rusqlite named params
    need ":a" prefix (was 400 Invalid parameter name); config-cfg
    candidate for install layout (rust/bin/). Nota bene: libc 1.x
    stable does not exist (only alphas) -> libc 0.2; IPv6 broken
    here -> CARGO_NET_IPV4_ONLY in build.sh; scratch probe removed.

## Iteration 21 (split python3/server.py into modules)
46. Layout (flat, zero deps kept): version.py, tags.py (wire tags),
    config.py (multi-source), fsutil.py (paths/mime), times.py
    (os-release + statx birth), routes.py (ROUTES + OPEN_ROUTES),
    handler.py (plumbing/auth/dispatch/help + Handler wiring),
    handler_fs/shell/sqlite/sysinfo.py (endpoint mixins). Entry
    python3/server.py stays thin (CLI + boot); invocation unchanged.
47. Split tool: ./tmp/split_py.py (anchor-asserted verbatim moves;
    had 1 bug: loop var shadowed docstring -> fixed by reconstructing
    server.py and diffing CLI against git: IDENTICAL). Structural bug
    found by the suite: col-0 defs after the class header orphan
    indented methods (shell/sqlite mixins fixed + missing imports).
48. Proof: 126/126 + old-vs-new differential (13 read + 12 write
    calls, only mtimes differ). Docs updated, version kept 0.10.0
    (pure refactor).

## Iteration 20 (native SQL binding in 5 ports)
46. Probes: node:sqlite takes ? + :named natively (rejects bool);
    PDO takes both (bare or colon keys); modernc takes ? +
    sql.Named (bool ok, converted anyway for parity); MS.Data.Sqlite
    REJECTS ? (must translate to $pN, keep :named).
47. Per port: keep the string/comment-aware scanner ONLY for
    single-statement enforcement + rows/write classification (+
    placeholder-style translation where the driver lacks a style:
    Kotlin :named->?, Go :named->?, .NET ?->$pN). Placeholder
    rewrites touch tokens only, never values. Values go through the
    driver's native binding (PreparedStatement/PDOStatement/run-all/
    db.Query|Exec/SqliteCommand) with bool->1/0 for Python parity.
    Drop literal-substitution (SqlLiteral/bindParams/finalSql).
48. Verify: suite x5, headless, docs, report.

## Iteration 24 (C++ port in cpp/)
49. Env: g++ 15.2 + cmake 4.2.3 + ninja, no conan -> vendor in
    cpp/third_party (nlohmann/json 3.12 single-header + SQLite
    3.54.0 amalgamation, compiled in). Std C++20 (<filesystem>,
    <thread>) + POSIX sockets/threads; statx raw syscall like Go.
50. Layout like other ports (main/config/tags/http/codec/api split +
    sqlite scan + sysinfo + routes). Native SQLite binding via C API
    (? + :named both native, no translation needed); scan kept only
    for single-statement + rows/write + placeholder-style checks.
    Build ./cpp/build.sh (cmake build/ + copy to cpp/bin/GnumarusOSBridge).
51. Bugs found by the suite: feeder missing `off += n` (infinite
    stdin loop -> timeouts); SIGPIPE death on early-exit commands
    (now ignored at boot); nlohmann dump() throws on invalid UTF-8
    (shell stdout/stderr now sanitized like file text mode).
    Verified: 136/136 vs C++, headless smoke, docs, report.

## Iteration 23 (shell stdin input)
49. New body fields `stdin` (raw string, default "") + `stdin_encoding`
    (text|base64, default text) on POST /api/shell, all 7 ports;
    response gains tagged `stdin_bytes`. Feeding: writer/feeder thread
    (Python/Kotlin/Rust/.NET-task/PHP-select-write-set/Node-end/
    Go-bytes.Reader) with EOF close; stdin pipe replaces DEVNULL;
    timeout path closes stdin to unblock feeders. PHP bug found:
    fd 0 needs ['pipe','r'] (child reads), 'w' gave EBADF.
50. Tests: TestShellStdin (10 tests: text/pipe/sort/binary/empty/
    ignored-by-non-reader/256KB-no-deadlock/3x400). Filemanager:
    stdin textarea + encoding select + stdin_bytes note. Suites 136
    OK x7 (one PHP blob flake + one dotnet config flake, both pass on
    re-run), headless smoke, docs, report.

## Iteration 22 (start.sh launcher)
    (NOTE: the selector was renamed to `--runtime <name>` later, see
    the "Launcher flag rename" entry in CHECKLIST.md; `cpp` was added
    afterwards too.)
49. Root start.sh: first `--port <name>` selects
    (python/kotlin/php/nodejs/go/dotnet/rust, `--port=` too), rest
    forwarded verbatim (incl. server's own --port), stdin untouched
    via exec (signals/exit codes propagate). Missing artifact ->
    build instructions + python fallback (args kept); unknown/missing
    name -> usage, exit 2. Verified: all 7 boot, stdin fwd, fallback
    (go binary moved aside + restored), exit codes, no strays.

## Iteration 18 (Go port in go/)
43. Env: Go 1.26.0, module proxy reachable. Deps: modernc.org/sqlite
    only (pure Go, no cgo); everything else is stdlib (net/http,
    encoding/json, os/exec with Setpgid kill parity, crypto,
    node-style birthtime via raw statx syscall + manual decode).
44. go/{go.mod,*.go} (package main), build `go build -o
    go/GnumarusOSBridge ./go`, run `go/GnumarusOSBridge` (the artifact
    is named GnumarusOSBridge on every port). The same 24 routes as
    every other port today, token auth, CORS,
    multi-source config (CWD -> exe dir), sysinfo, tilde, tagged
    scalars. SQLite via database/sql + modernc (fresh connection per
    request, real binding, DDL rowcount -1).
45. Verify: shared suite vs Go, fix parity, headless smoke, docs,
    report.

## Iteration 17 (C#/.NET port in dotnet/)
43. Env: .NET 10 SDK, NuGet reachable. Deps: Microsoft.Data.Sqlite
    only (HttpListener/JSON/FS/crypto all in shared framework).
    Probes first: HttpListener on Linux, File.GetCreationTimeUtc
    birth support, DllImport("libc") kill for process-group kill.
44. dotnet/ console app (publish -> dotnet/publish/GnumarusOSBridge.dll,
    run `dotnet dotnet/publish/GnumarusOSBridge.dll`): all 12 routes
    (24 today), token auth,
    CORS, multi-source config (CWD -> exe dir), shell via
    setsid+bash with libc kill(-pid) (killpg parity), sysinfo,
    tilde, tagged scalars. SQLite via Microsoft.Data.Sqlite (fresh
    connection per request, real binding, DDL rowcount -1).
45. Verify: shared suite vs .NET, fix parity, headless smoke, docs,
    report.

## Iteration 16 (Node.js/TypeScript port in nodejs/)
43. Env: Node v22.22.1 + npm 9.2.0. Probes: `node:sqlite` works
    flag-free (bundled sqlite 3.46.1, same as Python's), fs
    birthtimeMs returns real Linux birth with sub-ms precision,
    os.release gives the kernel. Deps: typescript + @types/node
    devDeps only (runtime is node core).
44. nodejs/{package.json,tsconfig.json,build.sh,src/*.ts} compiled to
    dist/, run `node nodejs/dist/server.js` (same CLI flags). All 12
    routes, token auth, CORS, multi-source config (CWD -> dist/ ->
    project root), shell via detached spawn + kill(-pid) (killpg
    parity), sysinfo, tilde, tagged scalars. SQLite via node:sqlite
    (fresh connection per request, real binding, DDL rowcount -1).
45. Verify: shared suite vs Node, fix parity, headless smoke, docs,
    report.

## Iteration 15 (tagged scalars: no JSON numbers/bools/nulls)
43. Wire rule (user chose value-tagging): JSON carries only
    string/array/object. Dynamic VALUE fields use tagged strings:
    `i:<int>`, `f:<float>`, `b:true|false`, `null` (bare). Fixed
    vocabulary + free text stay raw (names, paths, codes, messages,
    contents, column names, query params). Two fully-tagged positions
    (heterogeneous, need per-element typing): sqlite ROW CELLS and
    `params` VALUES, where strings take an `s:` prefix
    (`"s:ada"`, `"s:"` empty; `"s:i:5"` != int 5).
44. Optional inputs: missing key == default; explicit `"null"` ==
    omitted. Strict numeric grammar (`-?(0|[1-9][0-9]*)`, finite
    floats); `max_*` require `i:`, timeout takes `i:`/`f:`,
    `readonly` takes `b:`. Anything else -> 400.
45. Shared invariant test: every JSON response walked after parsing —
    no int/float/bool/None instance anywhere. Helpers gain `tv()`
    (parse tagged) for assertions; rows compared as exact wire
    strings. filemanager parses tags where numeric/bool needed.
46. Implement in Python (reference) + tests, then Kotlin, PHP,
    filemanager. Wire bump 0.9.0 -> 0.10.0 x3. Suites x3 + headless,
    docs, report.

## Iteration 13 (Kotlin port in kotlin/)
43. Env: Java 26.0.2 present; system kotlinc 1.3 broken on JDK 26;
    installed Kotlin 2.4.21 via SDKMAN (user-level, no root). User
    then allowed ecosystem deps: Gradle build (application plugin,
    installDist) + Gson 2.14.0 (JSON) + sqlite-jdbc 3.53.4.0 (real
    JDBC SQLite) + JNA 5.19.1 (Linux statx ctime; NIO lacks it).
44. kotlin/src/main/kotlin/{Main,Config,Api}.kt: all 12 routes, token
    auth, CORS, multi-source config (CWD -> jar dir -> install root),
    shell with streaming caps, sysinfo, tilde expansion. Run:
    kotlin/build/install/GnumarusOSBridge/bin/GnumarusOSBridge (same CLI flags).
45. Parity bugs found by the shared suite and fixed: Gson 2.14 drops
    explicit JsonNull members (-> serializeNulls), Redirect.DISCARD
    is output-only (-> close PIPE stdin instead), config search missed
    installDist layout, python_version is null on JVM (test asserts
    presence only; the field was later replaced by `jvm_version`, see
    Iteration 40). filemanager.html unchanged (same contract).
46. Verification: full Python suite vs Kotlin 123/123 OK (sqlite via
    JDBC, no sqlite3 CLI needed), vs Python 123/123 OK, headless
    file:// smoke vs Kotlin OK. Docs + report.

## Iteration 14 (PHP port in php/)
47. Env: PHP 8.5.4 CLI (json/hash/mbstring/fileinfo/pcntl/posix,
    PHP_CLI_SERVER_WORKERS). No composer deps needed. pdo_sqlite
    ABSENT (no .so; needs root apt php8.5-sqlite3) -> 503 +
    suite-skip until user installs. FFI blocked (ffi.enable=preload)
    -> crtime null on Linux (documented divergence).
48. Layout: php/server.php (CLI/config/CORS?/spawn php -S with
    resolved env) + php/router.php (front controller, never returns
    false) + php/src/{Config,Api}.php. pcntl signal forwarding,
    workers=8. Same contract incl. setsid-based process-group kill.
49. run_all --server-cfg becomes required with --server-cmd.
    Full suite vs PHP, fix parity. Docs + report (+ ask user:
    apt install php8.5-sqlite3).

## Future (revised)
- mkdir, rename/move, copy, meta-info endpoint.
- Auth/token, path sandboxing / command allowlist, HTTPS, logging options.
- Range requests / streaming for huge files.
- Shell: PTY/interactive sessions, per-command env vars, base64 output mode.
- Socket proxy: native WebSocket upgrade fast-path (same session protocol
  over WS binary messages) where the stack allows hijacking the connection.

## Iteration 25 (transparent socket proxy over HTTP sessions)
52. Goal: let a browser page open TCP/UDP/unix sockets through the
    local service (e.g. a browser email client speaking SMTP/IMAP to
    a TCP peer, or to a local daemon over a unix socket). Browsers
    cannot open raw sockets, so the service holds the real socket
    and exposes a transparent byte tunnel over plain HTTP (works on
    all 8 ports with existing plumbing; a native WebSocket upgrade
    fast-path stays Future — PHP's `php -S` workers and Kotlin's
    JDK HttpServer cannot hijack connections, so WS cannot be the
    uniform contract).
53. SCOPE (user): TCP only (no UDP/unix in v1); HTTP session
    tunnel + JS helper class (no native WS in v1); PHP via
    `socketd.php` session daemon; filemanager demo panel + helper.
54. API (4 routes, help 12 -> 16): POST /api/socket/open
    {host, port (i:), timeout?} -> 201 {id, kind:tcp, host, port};
    POST /api/socket/send?id= (raw bytes or JSON {content,encoding})
    -> {id, bytes}; GET /api/socket/recv?id=[&timeout][&max_bytes]
    (long-poll, JSON {content base64, bytes, eof, timed_out});
    POST /api/socket/close?id= -> {closed}. Tagged scalars kept
    (i:/b:); query params stay plain. Connect failures -> 400 with
    the OS error text (same precedent as SQLite engine errors).
    Sessions: server-minted opaque ids, cap on count/size, idle
    expiry, explicit close. Same token auth; localhost-bind WARNING
    like shell (this is an open network proxy by design).
54. Ports: reference in Python first, then Kotlin/PHP/Node/Go/.NET/
    Rust/C++. Known hard point: PHP workers share no memory/fds ->
    a small `php/socketd.php` session daemon (unix-socket IPC)
    spawned by `php/server.php`; router forwards /api/socket/*.
55. Verify: new tests/test_socket.py (echo roundtrips, binary +
    256KB, EOF, refused, unknown id, 400s, 401, 405s) green x8;
    headless file:// smoke; filemanager socket panel + JS helper
    class; docs; report. STATUS: done — 158/158 x8 (Python, Node,
    Go, Rust, C++, .NET, Kotlin, PHP), headless smoke (Online, 16
    endpoints, socket panel visible). Notable finds: Go
    double-readBody; Node f:-port; PHP fread-false-on-timeout +
    php -S worker-orphan shutdown bug (fixed with setsid group
    kill) + socketd tmp-path lesson; wrong Kotlin --server-cfg in
    one harness call (install-root cfg is correct).

## Reference port
- Go (`go/`) is the main reference port from here on. Same
  features and client-visible behavior everywhere (no more, no
  less); "reference" means: protocol ambiguities resolve in Go's
  favor, `tests/run_all.py` runs the Go server by default, and new
  features land in Go first.
- The build artifact of every port is named `GnumarusOSBridge`
  (`go/GnumarusOSBridge`, `rust/bin/GnumarusOSBridge`,
  `kotlin/build/install/GnumarusOSBridge/bin/GnumarusOSBridge`,
  `dotnet/publish/GnumarusOSBridge.dll`,
  `cpp/bin/GnumarusOSBridge`); the launchers, the test runner and the
  docs all use those paths.

## Iteration 27 (loopback-only bind + Host allowlist)
60. Scope (user): allowlist {127.0.0.1, localhost, ::1} (implemented
    as loopback range 127.0.0.0/8 + localhost + ::1 for bind/Host
    coherence); enforced always (every request incl. OPTIONS, open
    routes and WS upgrades, before auth); bind refuses non-loopback
    --host with config error (exit 2); violations get 403 JSON.
61. Implement: shared host-parsing rules per stack (strict IP parse,
    strip :port, [::1] brackets, case-insensitive localhost);
    config validation in all 8 resolvers; Host check as first line
    of each auth gate (+ explicit call in the 6 WS paths where they
    bypass it). No help JSON change; no socket-family rework
    (v6 listen where the stack already supports it).
62. Verify: new tests/test_host.py (allow/deny matrix, ordering vs
    token, no side effects, WS deny) + test_config.py bind cases;
    green x8 (186/170 each); docs; report. STATUS: done — 186/186
    go/python/nodejs/rust/cpp/dotnet, 170/170 (+skips) kotlin/php.
    Notable finds: OPTIONS bypasses gates (fixed per port);
    C++ inet_pton can't resolve "localhost" (getaddrinfo fallback);
    HttpListener 404s off-prefix Hosts itself (dotnet gained an
    internal reverse-proxy shim); IsLoopback-family parsers diverge
    (::ffff:, shorthands — replaced by strict textual rule
    everywhere); spawn_host helper raced poll() (wait-for-exit
    fix); PHP blob flake (re-run green).

## Iteration 26 (WebSocket transport for the socket proxy)
56. Goal: let the browser use a real WebSocket (the best socket the
    browser API offers) as the byte transport to a proxied TCP
    session, instead of HTTP send/recv polling.
57. Contract: `GET /api/socket/ws?id=<id>` with
    `Upgrade: websocket` attaches the WS to an existing session
    (opened via POST /api/socket/open; token via `?token=` since
    browsers cannot set WS headers). After 101, WS binary/text
    message payloads flow transparently client->TCP (text as UTF-8
    bytes), and TCP bytes flow back as WS binary frames; ping ->
    auto-pong; a close from either side, or any TCP/protocol error,
    ends the whole session (WS close IS session close, like HTTP
    close — a single owner per TCP stream, so no handoff can lose
    bytes; a stale pump stealing post-detach bytes was the first
    bug found). HTTP send/recv on an attached session -> 400; HTTP
    close kills the WS too. Non-upgrade GET -> 400; unknown id ->
    404; missing id -> 400; wrong method -> 405. Attached sessions
    skip idle expiry; message cap 4 MB (close 1009). Help 16 -> 17
    (`socket_ws`) where implemented.
58. Ports: Go (reference, hand-rolled RFC 6455 over http.Hijacker,
    stdlib only), Python (manual 101 + take over self.connection),
    Node (`upgrade` event), Rust + C++ (own TCP servers), .NET
    (HttpListener.AcceptWebSocketAsync — verify on Linux, fall
    back to document-only). NOT possible on the same URL/contract:
    Kotlin (JDK HttpServer exposes no raw socket) and PHP (php -S
    workers cannot hijack) — leave as-is, document, suite skips.
59. Verify: new tests/test_socket_ws.py (stdlib raw WS client;
    echo binary/text/fragmented/256KB, ping, EOF->close,
    close-detaches, HTTP-refused-while-attached, HTTP-close-kills-
    WS, 404/401/400/405, help names) green on WS ports, skipped
    elsewhere; run_all default server -> Go; filemanager
    SocketSession.openWs + WS toggle; headless smoke; docs,
    report. STATUS: done — 174/174 go/python/nodejs/rust/cpp/
    dotnet, 158/158 (+WS skip) kotlin/php; run_all defaults to Go;
    filemanager openWs + toggle, smoke Online (17). Live-browser
    WS unverifiable in this container (both headless Chromes fail
    even trivial echo WS — environmental); protocol proven by the
    raw suite + byte-identical Chrome-handshake replay.

## Iteration 28 (PHP port to ReactPHP, no more php -S)
63. Goal: replace `php -S` workers + `router.php` + `socketd.php`
    daemon with a single long-lived PHP CLI process on ReactPHP
    (`react/http` + `react/socket` + `react/child-process`,
    manual RFC 6455 handshake like Go/Python). Same 17 routes as
    the WS ports (PHP gains `socket_ws`), same CLI/config/auth/
    CORS/Host-gate/tagged-scalars contract; socket sessions become
    an in-memory map (no IPC daemon); shell runs via
    `react/child-process` so long commands don't stall the loop;
    sqlite stays blocking pdo_sqlite (short queries, documented).
64. Steps: composer.json + php/build.sh (vendor/ gitignored) ->
    spike (loop + 1 route + raw WS upgrade proof) -> full
    server.php rewrite (same boot stdout for the harness) ->
    Api Request/Response refactor -> socket in-memory -> shell
    async -> WS -> suite 186 vs PHP green -> start.sh hint update
    -> docs. STATUS: done — 186/186 (1 crtime skip) x3 runs,
    stable. Notable finds: react/socket write()=false is
    backpressure not failure (soft-limit 64KB); Connection::close()
    drops the write buffer (use end(frame) before FIN);
    endSession must not close the bridge-owned wsConn
    (delete-before-echo); SocketServer needs literal IPs
    ("localhost" resolved at bind); recv param validation must run
    before session lookup (Go order: 400 beats 404).

## Iteration 29 (showcase/filemanager webapp, Go assumed)
65. Goal: pure HTML/CSS/JS file manager in showcase/filemanager/
    (index.html + styles.css + app.js, no build, no external URLs)
    using ONLY the low-level webservice functions: server/token
    bar (localStorage), help-driven caps, breadcrumbs + dir table
    with per-entry metadata (type/size/atime/mtime/ctime/crtime/
    mode), text preview/edit/save, binary download (raw=1),
    create/overwrite/delete file, delete empty dir, sysinfo line.
    Default SERVER http://127.0.0.1:1423, hint ./start.sh --runtime go.
66. Verify: tests/test_showcase_filemanager.py (files exist,
    offline-pure, node --check when available, end-to-end app flow
    vs Go) + headless file:// smoke; docs. STATUS: implementing.

## Iteration 30 (showcase/emailclient IMAP webapp, Go assumed)
67. Goal: pure HTML/CSS/JS IMAP inbox client in showcase/emailclient/
    using ONLY the socket proxy (+ /api/help for the socket_ws
    cap): WS transport preferred, HTTP send/recv fallback. Presets
    gmail/outlook/icloud/yahoo/proton + custom host/port; LOGIN,
    read-only EXAMINE INBOX, paginated UID listing (UID SORT with
    SEARCH fallback), BODY.PEEK header fetch per page, BODY.PEEK[]
    open with MIME walk (text/plain preferred, html stripped,
    attachments listed by name). No send/delete/reply anywhere
    (not implemented by design). Honest TLS scope: raw TCP cannot
    terminate provider TLS in pure JS (no CA store, no verifiable
    handshake), so TLS presets carry a bridge notice; Proton
    Bridge (127.0.0.1:1143 plaintext) and any plaintext IMAP work
    directly; TLS garbage fails fast at the greeting with guidance.
68. Verify: tests/test_showcase_emailclient.py (static purity,
    node --check, route allowlist socket/help only, scripted fake
    IMAP server through the Go proxy over BOTH HTTP and WS:
    greeting/LOGIN/EXAMINE/SEARCH/FETCH incl. literals) +
    headless smoke; docs. STATUS: implementing.
68b. STATUS: done — 201/201 vs Go (192 + 9 new); node harness
    (12 assertions: rfc2047/mime/ByteBuf) green; headless smoke
    (6 providers, Proton note, no JS errors).

## Iteration 31 (TLS socket sessions, Go only for now)
69. Scope (user): Go port only; other ports later in the usual
    order. TLS as a session property (kind tcp|tls), not parallel
    methods: POST /api/socket/open gains tls? (b:), sni? (raw),
    ca_pem? (raw PEM); new POST /api/socket/starttls?id= upgrades
    a plaintext session in place (optional JSON {sni?, ca_pem?}).
    Handshake + system-store verification with hostname check and
    SNI in both paths; failures are 400 with the engine error text
    (same precedent as connect/SQLite errors). No insecure flag.
    send/recv/close/WS operate on the cleartext stream unchanged;
    starttls refused while WS-attached or recv-pending, or when
    already TLS. Help 17 -> 18 (socket_starttls) on Go.
70. Verify: new tests/test_socket_tls.py gated on the
    socket_starttls capability (skips elsewhere): local PKI via
    openssl into ./tmp (skip if absent), direct-TLS echo
    roundtrip, ca_pem positive/negative, SNI mismatch, garbage
    server, already-TLS, unknown id, 405, validation 400s,
    STARTTLS happy path + failure-keeps-session + WS-attached
    refusal. Full suite green vs Go; emailclient presets use
    tls:true (TLS notice reworded); docs. STATUS: implementing.

## Iteration 32 (WS handshake GUID fix — real-browser failures)
71. Root cause (user-reported: every browser failed
`GET /api/socket/ws` with `Incorrect 'Sec-WebSocket-Accept' header
value`, then HTTP close 404): the RFC 6455 handshake GUID was
mistyped in every hand-rolled implementation as `...-C05AB0DC85B6`
instead of `...-C5AB0DC85B11`. curl/python passed because nothing
validated the accept value — the suite's `upgrade()` asserted only
`101`, and `WS_GUID` sat unused. Fix: correct GUID in go/rust/
python/nodejs/php/cpp + tests helper; `upgrade()` now asserts
`Sec-WebSocket-Accept == expected_accept(key)`; new
server-independent `TestWsAcceptVector` pins the RFC 1.3 vector
(`dGhl...` -> `s3pP...`). Adjacent pre-existing PHP bug found by the
same run: bytes buffered between open and upgrade (e.g. an IMAP
greeting) were never flushed at attach (only future `onTcpData`
pumped) — `SocketStore::pumpBuffered` called after the 101 write.
72. Verify: 202 green vs python/rust/node/cpp, 202 green vs php (was
1 error), stale go binary now fails exactly the 12 accept assertions
(proves the guard); go binary itself needs a rebuild (no Go toolchain
in this container). STATUS: done pending go rebuild + browser retest.

## Iteration 33 (TLS socket sessions on all ports)
73. Scope (user): replicate the Go-only TLS sessions (iteration 31) on
rust, python, nodejs, kotlin, dotnet, php, c++ in that order. Same
contract everywhere: open gains tls?/sni?/ca_pem?, new
POST /api/socket/starttls?id=, same 400 texts ("Field 'tls' must be
b:true or b:false.", "Field 'sni' must be a non-empty string.",
"Field 'ca_pem' must be a valid PEM CA bundle.",
"TLS handshake with h:p failed: ...", "already TLS", "attached",
"Another recv is already pending"), failed starttls keeps the
plaintext session, help gains socket_starttls + open params.
Per-port TLS stacks: rust openssl crate (crates.io reachable),
python ssl stdlib, node tls stdlib, kotlin JVM SSLSocket +
custom TrustManager, .NET SslStream + validation callback, PHP
stream_socket_enable_crypto + temp cafile, C++ system OpenSSL
(-lssl -lcrypto, headers present).
74. Verify: tests/test_socket_tls.py runs unskipped vs each port
(docstring de-goed); full run_all green per port; docs. STATUS:
implementing.

## Iteration 34 (Ruby port)
75. Scope (user): add a `ruby/` port with full parity (all 18
    routes incl. `socket_ws` + `socket_starttls`), same CLI flags,
    same multi-source config precedence, same help catalog and 400
    texts. Where stdlib is not enough, use the customary manager
    (gem/bundler) into `ruby/vendor/` (gitignored like php/vendor);
    ask before any apt package. start.sh gains the `ruby` name.
    Port order rule puts ruby after php.
76. Stack: Ruby 3.3 stdlib (`socket`/`openssl`/`json`/`fiddle`) with
    a hand-rolled HTTP/1.1 server (raw TCP so WS can hijack, like
    Go/Python/PHP); TLS via `OpenSSL::SSL::SSLSocket` with a manual
    RFC 6125 hostname check (Ruby's `verify_hostname` does not match
    IP SANs); birth time via the `statx` syscall through Fiddle
    (`File::Stat#birthtime` is unimplemented on this Ruby/glibc);
    SQLite via the `sqlite3` gem installed into `ruby/vendor/gems`
    by `ruby/build.sh` (503 when unavailable, like the other ports).
78. Implemented: `ruby/server.rb` + `ruby/lib/gnumarus/{tags,util,
    http,routes,fsops,shell,sysinfo,sqlite,sock,sock_ws,api,config,
    statx}.rb` + `ruby/build.sh` + `ruby/config.cfg` (commented
    template). Raw `TCPServer` accept loop (one request per
    connection, `Connection: close`) so WS can hijack; hand-rolled
    RFC 6455; manual RFC 6125 hostname check (Ruby's built-in misses
    IP SANs); `statx` birth time via Fiddle; `bash -c` + pgroup +
    `KILL -pid`; sqlite3 gem with full ScanSql/named-param port and
    503 fallback; config candidates = cwd/config.cfg AND
    script-dir/config.cfg (so `--server-cfg ruby/config.cfg` is
    honoured). start.sh gained `ruby` after php.
79. Fixed while testing: (a) `OpenSSL::SSL::SSLZeroReturnError` does
    not exist here — naming it in a rescue raised NameError while
    handling the real IOError (masked HTTP-close-closes-WS); (b)
    `SockWs::Bridge` calling the module_function `utf8_valid?`
    unqualified → NoMethodError on every TEXT frame;
    (c) `IO.select` is not reliably interrupted by another thread
    closing the IO, so the TCP→WS pump polls at 250 ms and checks
    `Sock.live?` between reads.
80. Verify: `python3 tests/run_all.py --server-cmd "ruby ruby/server.rb"
    --server-cfg ruby/config.cfg` → 214 tests, OK, 0 skipped (all 12
    TLS tests ran), three consecutive green runs; sqlite probe "yes";
    503 fallback verified with the gem dir moved aside; `./start.sh
    --runtime ruby` works; `--runtime ruby` added to start.bat/start.ps1
    (start.ps1 re-verified on pwsh 7.6.6: selection + HTTP 200); docs
    updated. STATUS: done.

## Iteration 35 (WebSocket support in Kotlin — own the TCP layer)
81. Why: Kotlin is the only port without `socket_ws` (17 of 18 routes).
    Root cause is the HTTP stack, not the socket code: `Main.kt` uses
    `com.sun.net.httpserver.HttpServer`, whose `HttpExchange` exposes no
    raw `java.net.Socket`. Every reference port hijacks the connection
    (Go `http.Hijacker`, Python `self.connection`, Node `upgrade`, Ruby/
    C++/PHP own TCP servers, .NET `HttpListener.AcceptWebSocketAsync`).
82. Scope (user chose option A): replace the JDK `HttpServer` with a
    small hand-rolled HTTP/1.1 server over `ServerSocket` (the same
    design Ruby/PHP already use), keeping the existing routing/handler
    code by introducing a tiny `Exchange` abstraction that mirrors the
    exact subset of `HttpExchange` in use (`requestMethod`,
    `requestHeaders.getFirst`, `requestURI.path`/`rawQuery`,
    `requestBody.readAllBytes`, `responseHeaders.set`,
    `sendResponseHeaders`, `responseBody`, `close`). No new deps.
83. Then port the WS transport (bridge + RFC 6455 framing) from the Go
    reference: `GET /api/socket/ws?id=` with `Upgrade: websocket`
    (token via `?token=`); after 101, WS message payloads flow
    client->TCP (text as UTF-8) and TCP bytes come back as binary
    frames; ping auto-pong; close from either side ends the whole
    session; HTTP send/recv/starttls refused while attached; HTTP close
    kills the bridge; non-upgrade GET -> 400; unknown id -> 404;
    missing id -> 400; wrong method -> 405. Help 17 -> 18 (`socket_ws`).
84. Verify: suite vs Kotlin green with the WS-gated skips now running
    (test_socket_ws + host deny + TLS-attached + emailclient WS):
    "Ran 214 tests ... OK" with zero skips (Go run also 214, OK);
    `./start.sh --runtime kotlin` still boots and serves; docs updated.
    STATUS: done.

## Environment note (temporary)
- The Kotlin toolchain was missing from this container (no `gradle`, no
  `kotlinc`, no `~/.sdkman`). Reinstalled Gradle 9.8.1 user-level under
  `./tmp/gradle-9.8.1` with `GRADLE_USER_HOME=./tmp/gradle-home`
  (self-contained, no system/home change). `kotlin/build.sh` is
  unchanged (it still prefers a `gradle` on PATH or SDKMAN).

## Kotlin run/verify commands
- Build: `GRADLE_USER_HOME=./tmp/gradle-home ./tmp/gradle-9.8.1/bin/gradle
  -p kotlin installDist -q`
- Suite: `python3 tests/run_all.py --server-cmd
  "kotlin/build/install/GnumarusOSBridge/bin/GnumarusOSBridge" --server-cfg
  kotlin/build/install/GnumarusOSBridge/config.cfg` (the install-root cfg is the
  correct one; `kotlin/config.cfg` is not a server candidate).

## Iteration 36 (launcher flag: `--runtime` selects the implementation, `--port` is TCP-only)
85. Problem: `start.sh`/`start.bat`/`start.ps1` used the FIRST
    `--port <name>` to select the implementation while a second
    `--port <n>` set the server's TCP port — one flag meaning two
    things, with a "place it first" caveat. User decision: `--port`
    means the TCP port only; the implementation is selected with
    `--runtime <name>`.
86. Implemented in all three launchers: the first `--runtime <name>`
    (or `--runtime=<name>`) selects the runtime; every other argument
    (incl. `--port <n>`, `--token`, stdin) is forwarded verbatim;
    usage text, unknown/missing/needs-value messages and the python
    fallback text updated. Flag/name plumbing only — no behavior
    change to the servers themselves.
87. Verified: `start.sh` boots python/kotlin/go with a forwarded
    `--port` (both `--runtime x` and `--runtime=x` forms) and
    help/missing/no-value/unknown exit 0/2/2/2; `start.bat` under wine
    (help 0, missing/no-value/unknown 2, `--runtime=go` fallback 1);
    `start.ps1` under pwsh 7.6.6 (help 0, missing/no-value/unknown 2,
    fallback-hint chain 1; a kernel boot is blocked here only because
    `python` is not on PATH and the Windows artifact checks do not
    match Linux paths — unchanged from before). Docs and the
    filemanager "start it with" hints updated. STATUS: done.

## Iteration 37 (Gmail OAuth2 via the local service — Go reference only)
88. Goal: let `showcase/emailclient/` log into Gmail with OAuth2
    (IMAP `AUTHENTICATE XOAUTH2`) while keeping the existing
    user/password (`LOGIN`) mode untouched. User decision: the
    *local service* performs the OAuth dance (like Thunderbird), so
    the browser page never talks to Google and the showcase stays
    offline-pure; implement it in the **Go reference port only** for
    now (other ports keep the 18 shared routes and skip the new
    tests via capability probe).
89. Why the service must do it: Google's consent page sends no CORS
    headers and the token exchange needs a `client_secret`
    (Desktop-app clients). The page cannot hold the secret; the
    local service can. Design: Authorization Code + PKCE (S256),
    Desktop-app client, loopback redirect onto the service itself
    (`http://<Host>/api/oauth2/callback`), refresh token kept
    server-side.
90. New Go module `go/oauth2.go` + 5 routes:
    - `POST /api/oauth2/start` (oauth2_start): body
      `{client_id, client_secret?, auth_url?, token_url?, scope?}`
      -> 201 `{id, auth_url, redirect_uri, scope}`; mints state +
      PKCE verifier/challenge (S256); Google defaults are
      `accounts.google.com/o/oauth2/v2/auth` + `oauth2.googleapis.com/token`
      + scope `https://mail.google.com/`.
    - `GET /api/oauth2/callback?code=&state=` (oauth2_callback): the
      browser redirect target; **open route** (no token; correlated
      by `state`); exchanges the code for tokens server-side and
      renders a self-contained HTML page.
    - `GET /api/oauth2/poll?id=` (oauth2_poll): `{status:
      pending|authorized|exchanging|error}`; on authorized returns
      the access token + expires_in.
    - `POST /api/oauth2/refresh?id=` (oauth2_refresh): new access
      token from the stored refresh token.
    - `POST /api/oauth2/close?id=` (oauth2_close): drop the session.
    Sessions: 64 max, 600 s idle sweep, like the socket sessions.
91. Showcase: auth-mode selector (Password/LOGIN vs Google
    XOAUTH2), client id/secret fields + "Sign in with Google"
    (popup held across the async start call so it is not blocked),
    poll + refresh, `AUTHENTICATE XOAUTH2 <base64>` with the
    continuation abort (`+ ...` -> empty line). LOGIN path is
    byte-for-byte unchanged; Gmail preset note updated.
92. Tests: new `tests/test_oauth2.py` — capability-gated; a local
    fake Google (`/token`) verifies the PKCE S256 challenge, the
    code, redirect_uri, client_id/secret, returns tokens; drives
    start -> simulated redirect/callback -> poll -> refresh ->
    close, plus error/404/405/validation cases. Extend
    `test_showcase_emailclient.py` (XOAUTH2 script over both
    transports; new route allowlist entries) and the optional-name
    set in `test_help_cors.py`. All gated so the other eight ports
    still pass.
93. Docs: FEATURES_SUMMARY/DETAIL (new §36 OAuth2 section + §33
    emailclient), PLAN/CHECKLIST.
94. Verified: full suite vs Go = 232 tests OK (14 OAuth tests
    exercised; was 214); vs Python = 232 OK with 14 skips (the OAuth
    tests, capability-gated) so the other eight ports stay green.
    Manual smoke of the whole flow (fake Google) + gofmt clean on the
    new/changed Go files. STATUS: done.

## Iteration 38 (Gmail OAuth2 sign-in now lands on the INBOX)
95. Goal: after a successful "Sign in with Google" the inbox must show
    up just like after a password Connect. The user reported that the
    token was minted ("Signed in (3599s token)") but the message list
    never appeared.
96. Cause 1 (UX): signing in and connecting were two separate actions,
    so the OAuth flow ended at the token and never drove the IMAP
    session. Fix: when the poll reports `authorized`, the page connects
    by itself (`autoConnectAfterOAuth`) if the host/user are filled and
    no session is open.
97. Cause 2 (parsers assumed a fixed line position). Gmail-shaped
    replies differ from the ones the app was written against:
    - Gmail answers AUTHENTICATE with an unsolicited `* CAPABILITY`
      line and does not advertise SORT (a `UID SORT` gets `BAD`), so
      reading the UIDs only from `lines[0]` could silently yield an
      empty list. `loadUids` now scans ANY untagged `* SORT`/`* SEARCH`
      line, and falls back to `UID SEARCH ALL` (newest first) when SORT
      returns OK without UIDs too.
    - `parseFetchHeaders` now finds the `UID` token anywhere in the
      FETCH record (attribute order is not guaranteed), so a UID placed
      after the header literal still produces a row.
    - `cmd`/`authXoauth2` errors now name the command
      (`IMAP error (UID SEARCH): ...`) without echoing the password or
      the token.
98. Tests: `tests/js_emailclient_auth.js` grows into a small harness for
    the OAuth2 + response-parsing pieces (`loadUids`,
    `parseFetchHeaders`, `autoConnectAfterOAuth`) with Gmail-shaped
    inputs; `tests/test_showcase_emailclient.py` gains a Gmail-shaped
    script (`FakeImap(gmail=True)`: no SORT, an unsolicited
    `* CAPABILITY` after AUTHENTICATE) walked through the app's exact
    sequence over the proxy.
99. Docs: FEATURES_SUMMARY/DETAIL (§33 emailclient, §22/§36 counts),
    PLAN/CHECKLIST.
100. Verified: the node harness passes 23/23 checks; the full suite
    vs Go = 233 tests OK (14 OAuth tests exercised, one new
    Gmail-shaped proxy script); vs Python = 233 OK with 14 skips
    (capability-gated); `node --check` clean. STATUS: done.

## Iteration 39 (OAuth2 follow-up: the Auth mode drives the Provider)
101. User report after 38: the sign-in now auto-connects, but it tried
    `127.0.0.1:1143` — the Auth selector was on Google OAuth2 while the
    Provider select still sat on its default (Proton Bridge), so the
    auto-connect aimed at the wrong host. The Provider choice was also
    not persisted, so a reload reset it to Proton.
102. Fix: `alignProviderWithAuth` keeps the Provider in step with the
    Auth mode. Picking "Google OAuth2 (XOAUTH2)" switches the Provider
    to Gmail and fills `imap.gmail.com:993` right away (the user sees
    the host/port change); on load a saved OAuth2 mode is aligned too,
    except for a deliberately chosen Custom host. The Provider select
    is now persisted (`showcase_imap_provider`) and restored on load.
103. Also: the "Cross-Origin-Opener-Policy would block the window.close
    call" console noise is gone — the opener no longer closes the popup
    (Google's consent page is cross-origin and severs the opener); the
    service's callback page already scripts its own `window.close`, so
    the page just drops the reference. Only a blank popup (the start
    call never navigated) is closed from the opener.
104. Tests: the node harness grows to 29 checks (Auth->Provider
    alignment, the custom-host exception, password mode untouched); a
    headless Chrome pass drives the real `change` listener
    (proton -> gmail, `127.0.0.1:1143` -> `imap.gmail.com:993`) and the
    end-to-end page load (fake IMAP -> Go proxy -> 2 rows, no JS
    errors) is green again. Full suite vs Go = 233 OK. STATUS: done.

## Iteration 39 (OAuth2 on Rust and C++ ports)
- Implemented the 5 Gmail OAuth2 routes on the Rust and C++ ports, which
  previously kept 19 routes and skipped the OAuth tests.
  - Rust: new `rust/src/oauth2.rs` (session store + sweeper + HTTP client
    over TCP/TLS via the openssl crate), wired into `rust/src/api.rs`
    dispatch/check_auth/help and `rust/src/help.rs`.
  - C++: new `cpp/src/api_oauth2.inc` (session store + sweeper + HTTP
    client over the existing socket/TLS helpers), wired into
    `cpp/src/api.cpp` dispatch/check_auth/help and `cpp/src/routes.inc`.
- Verified: `tests/test_oauth2.py` now passes (14/14) against both Rust
  and C++ ports with no skips.

## Iteration 40 (contract alignment: artifact names, sysinfo, ctime, docs)
- Goal: make every port, launcher, test and document agree on one
  contract, after an audit of the Go reference against README.md,
  FEATURES_DETAIL.md, FEATURES_SUMMARY.md, PLAN.md and CHECKLIST.md.
- Build artifacts renamed to `GnumarusOSBridge` everywhere: Go already
  built that name but every doc/launcher/test looked for `go/gnumarus`
  (so `python3 tests/run_all.py` aborted with "default server ... is
  missing" and `./start.sh --runtime go` silently fell back to Python).
  Renamed: Rust (`[[bin]]` target), Kotlin (Gradle root project),
  .NET (`AssemblyName`), C++ (CMake target); the launchers
  (`start.sh`/`start.bat`/`start.ps1`), `tests/run_all.py` and
  `tests/test_host.py` now use the new paths. The Kotlin build script
  also picks a Gradle 7+ instead of whatever `gradle` is on PATH.
- `cpp` added to `start.sh`, `start.bat` and `start.ps1` (the port
  existed but was unreachable through the launchers).
- `/api/sysinfo` runtime-version rule: the port reports the version of
  the runtime that executes it and nothing else - `python_version`,
  `node_version`, `php_version`, `ruby_version`, `jvm_version`,
  `dotnet_version`. The native runtimes (Go, Rust, C++) report no
  version field at all: `go_version`, `rust_version` and `cpp_version`
  were removed (and Kotlin's `kotlin_version` with them, since it is a
  language version, not a runtime one).
- `ctime` bug in the Go reference: `go/statx.go` asked the kernel only
  for `STATX_BTIME` (0x800) and then read `stx_ctime` anyway, so every
  directory entry reported `ctime: f:0`. Fixed by requesting
  `STATX_CTIME` (0x80) as well and by returning nil when the kernel
  does not fill the field (so the mtime fallback applies). The same bug
  existed in the Kotlin port (`linuxCtime`) and was fixed there too.
- Help catalog: `delete_directory` now advertises the `shallow` query
  parameter (default `0`, allowed `0`/`1`) and `delete_directory_shallow`
  documents the aliases (`/api/shallow_dir`, `/api/dir_shallow`), on
  all nine ports; the `system_info` response text documents the
  runtime-version rule.
- Tests: the suite grew to 241 tests and now asserts the exact
  24-route catalog (not "at least"), the `shallow` parameter, the
  single runtime-version field of `/api/sysinfo`, and that `ctime`
  matches `stat -c %Z` (the regression gate for the statx mask bug).
- Docs: README, FEATURES_SUMMARY, FEATURES_DETAIL, PLAN and CHECKLIST
  updated (route counts 18/19/23 -> 24, test counts -> 241, artifact
  names, sysinfo fields, PHP port no longer described as `php -S` +
  `socketd.php`, launcher runtime list including `cpp`).
- Verified: 241/241 on all nine ports (PHP skips the 21 SQLite tests
  when pdo_sqlite is absent in the host); `start.sh --runtime <name>`
  boots all nine (including the new `cpp`).
