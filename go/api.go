package main

import (
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

const version = "0.10.0"
const serviceName = "GnumarusPythonNativeOperationsHttpService"

// openRoutes are the GET routes reachable without a token: the index,
// the catalog itself, and the OAuth loopback callback (the provider's
// redirect cannot carry our token; it is correlated by the random
// state instead).
var openRoutes = map[string]bool{
	"/": true, "/api/help": true, oauthCallbackPath: true,
}

const (
	shellDefaultTimeout  = 30.0
	shellMaxTimeout      = 300.0
	shellDefaultMaxOut   = 1024 * 1024
	shellMaxOutput       = 100 * 1024 * 1024
	sqliteDefaultTimeout = 5.0
	sqliteMaxTimeout     = 300.0
)

// Opts carries per-server state (auth + working directory).
type Opts struct {
	AuthRequired bool
	AuthToken    string
	Cwd          string
}

// ---------------------------------------------------------------------------
// plumbing
// ---------------------------------------------------------------------------

func setCORS(w http.Header) {
	w.Set("Access-Control-Allow-Origin", "*")
	w.Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
	w.Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
	w.Set("Access-Control-Max-Age", "86400")
}

func sendJSON(w http.ResponseWriter, status int, obj any) {
	body, err := json.Marshal(obj)
	if err != nil {
		body = []byte(`{"error":"error","message":"JSON encoding failed"}`)
		status = 500
	}
	setCORS(w.Header())
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func sendRaw(w http.ResponseWriter, status int, body []byte, contentType string) {
	setCORS(w.Header())
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func label(status int) string {
	switch status {
	case 400:
		return "bad_request"
	case 401:
		return "unauthorized"
	case 403:
		return "forbidden"
	case 404:
		return "not_found"
	case 405:
		return "method_not_allowed"
	case 409:
		return "conflict"
	default:
		return "error"
	}
}

func sendErr(w http.ResponseWriter, status int, message, path string) {
	obj := map[string]any{"error": label(status), "message": message}
	if path != "" {
		obj["path"] = path
	}
	sendJSON(w, status, obj)
}

// quadLoopback matches loopback literals textually: strict dotted
// quads (no shorthands, no leading zeros) in 127.0.0.0/8, or exactly
// "::1". Deliberately NOT net.ParseIP/IsLoopback, whose accepted
// shapes (IPv4-mapped IPv6, etc.) differ per stack.
func quadLoopback(name string) bool {
	if name == "::1" {
		return true
	}
	parts := strings.Split(name, ".")
	if len(parts) != 4 {
		return false
	}
	for _, p := range parts {
		if p == "" || (len(p) > 1 && p[0] == '0') {
			return false
		}
		for i := 0; i < len(p); i++ {
			if p[i] < '0' || p[i] > '9' {
				return false
			}
		}
		n, _ := strconv.Atoi(p)
		if n > 255 {
			return false
		}
	}
	return parts[0] == "127"
}
func allDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

func hostAllowed(hostHeader string) bool {
	h := strings.TrimSpace(hostHeader)
	if h == "" {
		return false
	}
	var name string
	if strings.HasPrefix(h, "[") {
		end := strings.Index(h, "]")
		if end < 0 {
			return false
		}
		if rest := h[end+1:]; rest != "" {
			if !strings.HasPrefix(rest, ":") || !allDigits(rest[1:]) {
				return false
			}
		}
		name = h[1:end]
	} else {
		if strings.Count(h, ":") > 1 {
			return false // bare IPv6 must be bracketed
		}
		if i := strings.LastIndex(h, ":"); i >= 0 {
			if !allDigits(h[i+1:]) {
				return false
			}
			name = h[:i]
		} else {
			name = h
		}
	}
	if name == "" {
		return false
	}
	if strings.EqualFold(name, "localhost") {
		return true
	}
	return quadLoopback(name)
}

func checkHost(r *http.Request, w http.ResponseWriter) bool {
	if hostAllowed(r.Host) {
		return true
	}
	setCORS(w.Header())
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(403)
	_, _ = w.Write([]byte(`{"error":"forbidden","message":"Host not allowed (use 127.0.0.1, localhost or ::1)."}`))
	return false
}

func checkAuth(r *http.Request, w http.ResponseWriter, opts Opts) bool {
	if !opts.AuthRequired {
		return true
	}
	if r.Method == "GET" && openRoutes[r.URL.Path] {
		return true
	}
	supplied := ""
	if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
		supplied = strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
	}
	if supplied == "" {
		supplied = r.URL.Query().Get("token")
	}
	a, b := []byte(supplied), []byte(opts.AuthToken)
	if opts.AuthToken != "" && len(a) == len(b) && subtle.ConstantTimeCompare(a, b) == 1 {
		return true
	}
	setCORS(w.Header())
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("WWW-Authenticate", "Bearer")
	w.WriteHeader(401)
	_, _ = w.Write([]byte(`{"error":"unauthorized","message":"Valid API token required: send Authorization: Bearer <token> or ?token=."}`))
	return false
}

func resolvePath(rawPath, cwd string) string {
	p := rawPath
	if p == "~" || strings.HasPrefix(p, "~/") {
		if home, err := os.UserHomeDir(); err == nil && home != "" {
			p = home + p[1:]
		}
	}
	if !filepath.IsAbs(p) {
		p = filepath.Join(cwd, p)
	}
	return filepath.Clean(p)
}

func lexists(p string) bool {
	if li, err := os.Lstat(p); err == nil {
		_ = li
		return true
	}
	return false
}

func entryType(p string) string {
	if li, err := os.Lstat(p); err == nil {
		if li.Mode()&os.ModeSymlink != 0 {
			return "symlink"
		}
	} else {
		return "other"
	}
	if st, err := os.Stat(p); err == nil {
		if st.IsDir() {
			return "dir"
		}
		if st.Mode().IsRegular() {
			return "file"
		}
	}
	return "other"
}

func modeString(p string) string {
	st, err := os.Lstat(p)
	if err != nil {
		return "0o644"
	}
	return "0o" + strconv.FormatUint(uint64(st.Mode().Perm()), 8)
}

func epochSec(sec int64, nsec int64) float64 {
	return float64(sec) + float64(nsec)/1e9
}

var mimeByExt = map[string]string{
	"txt": "text/plain", "html": "text/html", "htm": "text/html",
	"json": "application/json", "xml": "text/xml", "css": "text/css",
	"js": "text/javascript", "png": "image/png", "jpg": "image/jpeg",
	"jpeg": "image/jpeg", "gif": "image/gif", "pdf": "application/pdf",
	"zip": "application/zip", "csv": "text/csv", "md": "text/markdown",
}

func guessMime(p string) string {
	ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(p), "."))
	if ext == "" {
		return "application/octet-stream"
	}
	if m, ok := mimeByExt[ext]; ok {
		return m
	}
	return "application/octet-stream"
}

func decodeStrict(data []byte) (string, bool) {
	s := string(data)
	i := 0
	for i < len(s) {
		c := s[i]
		var size int
		switch {
		case c < 0x80:
			size = 1
		case c>>5 == 0x6:
			size = 2
		case c>>4 == 0xE:
			size = 3
		case c>>3 == 0x1E:
			size = 4
		default:
			return "", false
		}
		if i+size > len(s) {
			return "", false
		}
		for _, b := range []byte(s[i+1 : i+size]) {
			if b>>6 != 0x2 {
				return "", false
			}
		}
		i += size
	}
	return s, true
}

func hasBinaryControls(s string) bool {
	for _, r := range s {
		if r < 32 && r != '\t' && r != '\n' && r != '\r' {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// dispatch
// ---------------------------------------------------------------------------

// Handler routes every request (method + path dispatch).
func Handler(opts Opts) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				sendErr(w, 500, fmt.Sprintf("Internal error: %v", rec), "")
			}
		}()
		if !checkHost(r, w) {
			return
		}
		if r.Method == "OPTIONS" {
			setCORS(w.Header())
			w.WriteHeader(204)
			return
		}
		if !checkAuth(r, w, opts) {
			return
		}
		path := r.URL.Path
		q := r.URL.Query()
		switch r.Method {
		case "GET":
			doGet(w, r, path, q, opts)
		case "POST":
			doPost(w, r, path, q, opts)
		case "PUT":
			doPut(w, r, path, q, opts)
		case "DELETE":
			doDelete(w, r, path, q, opts)
		default:
			sendErr(w, 405, fmt.Sprintf("Method %s not supported.", r.Method), "")
		}
	}
}

func doGet(w http.ResponseWriter, r *http.Request, path string, q map[string][]string, opts Opts) {
	_ = r
	switch path {
	case "/api/help":
		sendJSON(w, 200, map[string]any{
			"version": version,
			"count":   mustEnc(len(routes())),
			"routes":  routes(),
			"auth": map[string]any{
				"required":    mustEnc(opts.AuthRequired),
				"open_routes": append([]string{"/", "/api/help", oauthCallbackPath}, "OPTIONS *"),
				"how":         "Authorization: Bearer <token> header (preferred) or ?token= query parameter.",
			},
		})
	case "/api/dir":
		handleReadDir(w, q, opts)
	case "/api/file":
		handleReadFile(w, q, opts)
	case "/":
		sendJSON(w, 200, map[string]any{
			"service": serviceName, "version": version, "help": "/api/help",
		})
	case "/api/shell", "/api/sqlite/create", "/api/sqlite/query",
		"/api/socket/open", "/api/socket/send", "/api/socket/close",
		"/api/socket/starttls",
		"/api/oauth2/start", "/api/oauth2/refresh", "/api/oauth2/close":
		sendErr(w, 405, "Method GET not supported for this route (use POST).", "")
	case "/api/socket/recv":
		handleSocketRecv(w, q)
	case "/api/socket/ws":
		handleSocketWs(w, r, q)
	case "/api/oauth2/poll":
		handleOAuth2Poll(w, q)
	case oauthCallbackPath:
		handleOAuth2Callback(w, q)
	case "/api/sysinfo":
		handleSysinfo(w)
	default:
		sendErr(w, 404, "Unknown route: "+path, "")
	}
}

func doPost(w http.ResponseWriter, r *http.Request, path string, q map[string][]string, opts Opts) {
	switch path {
	case "/api/file":
		handleCreateFile(w, r, q, opts)
	case "/api/shell":
		handleExecShell(w, r, opts)
	case "/api/sqlite/create":
		handleSqliteCreate(w, r, q, opts)
	case "/api/sqlite/query":
		handleSqliteQuery(w, r, q, opts)
	case "/api/socket/open":
		handleSocketOpen(w, r)
	case "/api/socket/send":
		handleSocketSend(w, r, q)
	case "/api/socket/close":
		handleSocketClose(w, q)
	case "/api/socket/starttls":
		handleSocketStarttls(w, r, q)
	case "/api/oauth2/start":
		handleOAuth2Start(w, r)
	case "/api/oauth2/refresh":
		handleOAuth2Refresh(w, q)
	case "/api/oauth2/close":
		handleOAuth2Close(w, q)
	case "/", "/api/help", "/api/dir", "/api/sysinfo", "/api/socket/recv",
		"/api/socket/ws", "/api/oauth2/poll", oauthCallbackPath:
		sendErr(w, 405, "Method POST not supported for this route.", "")
	default:
		sendErr(w, 404, "Unknown route: "+path, "")
	}
}

func doPut(w http.ResponseWriter, r *http.Request, path string, q map[string][]string, opts Opts) {
	if path == "/api/file" {
		handleOverwriteFile(w, r, q, opts)
		return
	}
	for _, known := range []string{"/", "/api/help", "/api/dir", "/api/shell",
		"/api/sqlite/create", "/api/sqlite/query", "/api/sysinfo",
		"/api/socket/open", "/api/socket/send", "/api/socket/recv",
		"/api/socket/close", "/api/socket/starttls", "/api/socket/ws",
		"/api/oauth2/start", "/api/oauth2/poll", "/api/oauth2/refresh",
		"/api/oauth2/close", oauthCallbackPath} {
		if path == known {
			sendErr(w, 405, "Method PUT not supported for this route.", "")
			return
		}
	}
	sendErr(w, 404, "Unknown route: "+path, "")
}

func doDelete(w http.ResponseWriter, r *http.Request, path string, q map[string][]string, opts Opts) {
	_ = r
	if path == "/api/file" {
		handleDeleteFile(w, q, opts)
		return
	}
	if path == "/api/dir" {
		handleDeleteDir(w, q, opts)
		return
	}
	for _, known := range []string{"/", "/api/help", "/api/shell",
		"/api/sqlite/create", "/api/sqlite/query", "/api/sysinfo",
		"/api/socket/open", "/api/socket/send", "/api/socket/recv",
		"/api/socket/close", "/api/socket/starttls", "/api/socket/ws",
		"/api/oauth2/start", "/api/oauth2/poll", "/api/oauth2/refresh",
		"/api/oauth2/close", oauthCallbackPath} {
		if path == known {
			sendErr(w, 405, "Method DELETE not supported for this route.", "")
			return
		}
	}
	sendErr(w, 404, "Unknown route: "+path, "")
}

func mustEnc(v any) string {
	s, err := JEnc(v)
	if err != nil {
		panic(err)
	}
	return s
}

func firstQuery(q map[string][]string, key string) string {
	if v, ok := q[key]; ok && len(v) > 0 {
		return v[0]
	}
	return ""
}

func readBody(r *http.Request) ([]byte, error) {
	defer r.Body.Close()
	return io.ReadAll(r.Body)
}

// -- read endpoints ----------------------------------------------------------

func handleReadDir(w http.ResponseWriter, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if !lexists(abs) {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	st, err := os.Stat(abs)
	if err != nil {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	if !st.IsDir() {
		sendErr(w, 400, "Path is not a directory", abs)
		return
	}
	dir, err := os.Open(abs)
	if err != nil {
		sendErr(w, 403, "Cannot list directory", abs)
		return
	}
	names, err := dir.Readdirnames(-1)
	dir.Close()
	if err != nil {
		sendErr(w, 403, "Cannot list directory", abs)
		return
	}
	sort.Strings(names)
	var entries []any
	for _, name := range names {
		full := filepath.Join(abs, name)
		li, err := os.Lstat(full)
		if err != nil {
			continue
		}
		ctime, btime := StatxTimes(full)
		entries = append(entries, map[string]any{
			"name":   name,
			"path":   full,
			"type":   entryType(full),
			"size":   mustEnc(li.Size()),
			"atime":  mustEnc(epochOf(li, "atime")),
			"mtime":  mustEnc(epochOf(li, "mtime")),
			"ctime":  mustEnc(firstNonNil(ctime, epochOf(li, "mtime"))),
			"crtime": btimeTag(btime),
			"mode":   modeString(full),
		})
	}
	if entries == nil {
		entries = []any{}
	}
	sendJSON(w, 200, map[string]any{
		"path": raw, "absolute_path": abs,
		"count": mustEnc(len(entries)), "entries": entries,
	})
}

func firstNonNil(v *float64, fallback float64) float64 {
	if v == nil {
		return fallback
	}
	return *v
}

func btimeTag(v *float64) string {
	if v == nil {
		return "null"
	}
	s, _ := JEnc(*v)
	return s
}

func epochOf(li os.FileInfo, which string) float64 {
	sys, ok := li.Sys().(*syscall.Stat_t)
	if !ok || sys == nil {
		t := li.ModTime()
		return float64(t.Unix()) + float64(t.Nanosecond())/1e9
	}
	var ts syscall.Timespec
	switch which {
	case "atime":
		ts = sys.Atim
	case "ctime":
		ts = sys.Ctim
	default:
		ts = sys.Mtim
	}
	return float64(ts.Sec) + float64(ts.Nsec)/1e9
}

func handleReadFile(w http.ResponseWriter, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if !lexists(abs) {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	if st, err := os.Stat(abs); err == nil {
		if li, err2 := os.Lstat(abs); err2 == nil {
			if st.IsDir() && li.Mode()&os.ModeSymlink == 0 {
				sendErr(w, 400, "Path is a directory, not a file", abs)
				return
			}
		} else if st.IsDir() {
			sendErr(w, 400, "Path is a directory, not a file", abs)
			return
		}
	} else {
		sendErr(w, 404, "Path does not exist", abs)
		return
	}
	encoding := firstQuery(q, "encoding")
	if encoding == "" {
		encoding = "auto"
	}
	encoding = strings.ToLower(encoding)
	if encoding != "auto" && encoding != "text" && encoding != "base64" {
		sendErr(w, 400, fmt.Sprintf("Invalid encoding param: '%s' (use auto|text|base64)", encoding), "")
		return
	}
	rawMode := firstQuery(q, "raw") == "1"
	data, err := os.ReadFile(abs)
	if err != nil {
		if os.IsNotExist(err) {
			sendErr(w, 404, "Path does not exist", abs)
			return
		}
		if os.IsPermission(err) {
			sendErr(w, 403, "Permission denied", abs)
			return
		}
		sendErr(w, 403, fmt.Sprintf("Cannot read file: %v", err), abs)
		return
	}
	mtime := "null"
	if li, err := os.Lstat(abs); err == nil {
		mtime, _ = JEnc(epochOf(li, "mtime"))
	}
	if rawMode {
		sendRaw(w, 200, data, guessMime(abs))
		return
	}
	mime := guessMime(abs)
	meta := func(binary bool, enc, content string) map[string]any {
		return map[string]any{
			"path": raw, "absolute_path": abs,
			"size": mustEnc(int64(len(data))), "mtime": mtime,
			"mime_type": mime, "is_binary": mustEnc(binary),
			"encoding": enc, "content": content,
		}
	}
	if encoding == "text" {
		sendJSON(w, 200, meta(false, "utf-8", string(data)))
		return
	}
	if encoding == "base64" {
		sendJSON(w, 200, meta(true, "base64", base64.StdEncoding.EncodeToString(data)))
		return
	}
	if text, ok := decodeStrict(data); ok && !hasBinaryControls(text) {
		sendJSON(w, 200, meta(false, "utf-8", text))
	} else {
		sendJSON(w, 200, meta(true, "base64", base64.StdEncoding.EncodeToString(data)))
	}
}
