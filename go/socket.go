package main

// Transparent TCP socket proxy: the service holds a real TCP client
// socket per session and exposes a byte-transparent tunnel over
// plain HTTP, so browser pages (which cannot open raw sockets) can
// speak arbitrary TCP protocols through the local service.

import (
	"bytes"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	sockDefaultConnectTimeout = 5.0
	sockMaxConnectTimeout     = 30.0
	sockDefaultRecvTimeout    = 5.0
	sockMaxRecvTimeout        = 60.0
	sockDefaultMaxBytes       = 65536
	sockMaxBytes              = 1024 * 1024
	sockMaxSessions           = 128
	sockIdleTimeout           = 300 * time.Second
)

type sockSession struct {
	conn net.Conn
	host string
	port int
	// kind is "tcp" or "tls": TLS is a transport property of the
	// session, negotiated at open (implicit) or via starttls, and
	// verified against the system roots (+ optional ca_pem) with
	// hostname check and SNI. send/recv/close/WS see cleartext.
	kind       string
	serverName string
	last       time.Time
	eof        bool
	mu         sync.Mutex // serializes recv waits on one session
	// wsAttached marks a live WebSocket bridge: the idle sweeper
	// skips it and HTTP send/recv are refused while set.
	wsAttached bool
}

var (
	sockSessions = map[string]*sockSession{}
	sockMu       sync.Mutex
	sockSweep    sync.Once
)

func sockSweepExpired() {
	now := time.Now()
	sockMu.Lock()
	defer sockMu.Unlock()
	for sid, sess := range sockSessions {
		if sess.wsAttached {
			continue
		}
		if now.Sub(sess.last) > sockIdleTimeout {
			delete(sockSessions, sid)
			sess.conn.Close()
		}
	}
}

func sockEnsureSweeper() {
	sockSweep.Do(func() {
		go func() {
			for {
				time.Sleep(60 * time.Second)
				sockSweepExpired()
			}
		}()
	})
}

func sockLookup(w http.ResponseWriter, q map[string][]string) (string, *sockSession, bool) {
	sid := firstQuery(q, "id")
	if sid == "" {
		sendErr(w, 400, "Missing required query parameter: id", "")
		return "", nil, false
	}
	sockMu.Lock()
	sess, ok := sockSessions[sid]
	if ok {
		sess.last = time.Now()
	}
	sockMu.Unlock()
	if !ok {
		sendErr(w, 404, "Unknown socket session: "+sid, "")
		return "", nil, false
	}
	return sid, sess, true
}

func sockMintID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return base64.RawURLEncoding.EncodeToString(b[:])
}

func sockParseJSON(w http.ResponseWriter, r *http.Request) (map[string]any, bool) {
	body, err := readBody(r)
	if err != nil {
		sendErr(w, 400, "Cannot read body.", "")
		return nil, false
	}
	obj := map[string]any{}
	if len(bytes.TrimSpace(body)) == 0 {
		return obj, true
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	if err := dec.Decode(&obj); err != nil {
		sendErr(w, 400, "Invalid JSON body.", "")
		return nil, false
	}
	if obj == nil {
		sendErr(w, 400, "JSON body must be an object.", "")
		return nil, false
	}
	return obj, true
}

// sockParseTLSIdentity validates the optional sni/ca_pem body fields
// shared by socket_open and socket_starttls. Absent (or explicit
// null) means "not given". Returns sni, caPEM, or an error message.
func sockParseTLSIdentity(obj map[string]any) (string, string, string) {
	sni := ""
	if v, present := obj["sni"]; present && v != nil {
		s, isStr := v.(string)
		if !isStr || strings.TrimSpace(s) == "" {
			return "", "", "Field 'sni' must be a non-empty string."
		}
		sni = strings.TrimSpace(s)
	}
	caPEM := ""
	if v, present := obj["ca_pem"]; present && v != nil {
		s, isStr := v.(string)
		if !isStr || strings.TrimSpace(s) == "" {
			return "", "", "Field 'ca_pem' must be a PEM CA bundle string."
		}
		caPEM = s
	}
	return sni, caPEM, ""
}

// sockCertPool builds a root pool from the system store plus an
// optional extra PEM bundle (corporate intercepts, test PKIs).
func sockCertPool(pemData string) (*x509.CertPool, error) {
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	if !pool.AppendCertsFromPEM([]byte(pemData)) {
		return nil, fmt.Errorf("no valid PEM certificates")
	}
	return pool, nil
}

// sockTLSWrap runs a client handshake over an established TCP
// connection. The deadline bounds the whole handshake. On failure
// the raw connection is left open: the caller decides (open drops
// it, starttls keeps the plaintext session).
func sockTLSWrap(raw net.Conn, serverName string, rootCAs *x509.CertPool, timeout time.Duration) (net.Conn, error) {
	cfg := &tls.Config{
		ServerName: serverName,
		RootCAs:    rootCAs,
		MinVersion: tls.VersionTLS12,
	}
	tc := tls.Client(raw, cfg)
	if err := tc.SetDeadline(time.Now().Add(timeout)); err != nil {
		return nil, err
	}
	if err := tc.Handshake(); err != nil {
		_ = tc.SetDeadline(time.Time{})
		return nil, err
	}
	_ = tc.SetDeadline(time.Time{})
	return tc, nil
}

func handleSocketOpen(w http.ResponseWriter, r *http.Request) {
	sockEnsureSweeper()
	if !strings.Contains(r.Header.Get("Content-Type"), "application/json") {
		sendErr(w, 400, "Content-Type must be application/json with body { host, port, timeout? }.", "")
		return
	}
	obj, ok := sockParseJSON(w, r)
	if !ok {
		return
	}
	host, _ := obj["host"].(string)
	if strings.TrimSpace(host) == "" {
		sendErr(w, 400, "Missing or empty required field: host.", "")
		return
	}
	pv, present := obj["port"]
	if !present || pv == nil {
		sendErr(w, 400, "Missing required field: port (i:<1-65535>).", "")
		return
	}
	ps, isStr := pv.(string)
	if !isStr || !strings.HasPrefix(ps, "i:") {
		sendErr(w, 400, "Field 'port' must be an integer 1-65535 (i:<int>).", "")
		return
	}
	t, err := JDec(ps)
	if err != nil {
		sendErr(w, 400, "Field 'port' must be an integer 1-65535 (i:<int>).", "")
		return
	}
	n, isInt := toInt(t)
	if !isInt || n < 1 || n > 65535 {
		sendErr(w, 400, "Field 'port' must be an integer 1-65535 (i:<int>).", "")
		return
	}
	port := n
	timeout := sockDefaultConnectTimeout
	if v, present := obj["timeout"]; present && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		t, err := JDec(s)
		if err != nil {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		f, ok := toFloat(t)
		if !ok {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		timeout = f
	}
	if !(timeout > 0 && timeout <= sockMaxConnectTimeout) {
		sendErr(w, 400, fmt.Sprintf("Field 'timeout' must be within (0, %d].", int(sockMaxConnectTimeout)), "")
		return
	}
	tlsMode := false
	if v, present := obj["tls"]; present && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'tls' must be b:true or b:false.", "")
			return
		}
		t, err := JDec(s)
		if err != nil {
			sendErr(w, 400, "Field 'tls' must be b:true or b:false.", "")
			return
		}
		b, isBool := t.(bool)
		if !isBool {
			sendErr(w, 400, "Field 'tls' must be b:true or b:false.", "")
			return
		}
		tlsMode = b
	}
	sni, caPEM, idErr := sockParseTLSIdentity(obj)
	if idErr != "" {
		sendErr(w, 400, idErr, "")
		return
	}
	if sni == "" {
		sni = host
	}
	var rootCAs *x509.CertPool
	if caPEM != "" {
		pool, err := sockCertPool(caPEM)
		if err != nil {
			sendErr(w, 400, "Field 'ca_pem' must be a valid PEM CA bundle.", "")
			return
		}
		rootCAs = pool
	}
	sockSweepExpired()
	sockMu.Lock()
	full := len(sockSessions) >= sockMaxSessions
	sockMu.Unlock()
	if full {
		sendErr(w, 400, fmt.Sprintf("Too many open socket sessions (%d).", sockMaxSessions), "")
		return
	}
	timeoutDur := time.Duration(timeout * float64(time.Second))
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("%s:%d", host, port), timeoutDur)
	if err != nil {
		sendErr(w, 400, fmt.Sprintf("Cannot connect to %s:%d: %v", host, port, err), "")
		return
	}
	kind := "tcp"
	if tlsMode {
		tc, herr := sockTLSWrap(conn, sni, rootCAs, timeoutDur)
		if herr != nil {
			conn.Close()
			sendErr(w, 400, fmt.Sprintf("TLS handshake with %s:%d failed: %v", host, port, herr), "")
			return
		}
		conn = tc
		kind = "tls"
	}
	sid := sockMintID()
	sockMu.Lock()
	sockSessions[sid] = &sockSession{conn: conn, host: host, port: port, kind: kind, serverName: sni, last: time.Now()}
	sockMu.Unlock()
	sendJSON(w, 201, map[string]any{
		"id": sid, "kind": kind, "host": host, "port": mustEnc(int64(port)),
	})
}

func sockSendBytes(w http.ResponseWriter, raw []byte, ct string) ([]byte, bool) {
	if strings.Contains(ct, "application/json") {
		obj := map[string]any{}
		if len(bytes.TrimSpace(raw)) > 0 {
			dec := json.NewDecoder(bytes.NewReader(raw))
			if err := dec.Decode(&obj); err != nil {
				sendErr(w, 400, "Invalid JSON body.", "")
				return nil, false
			}
			if obj == nil {
				sendErr(w, 400, "JSON body must be an object.", "")
				return nil, false
			}
		}
		content := ""
		if v, present := obj["content"]; present && v != nil {
			s, isStr := v.(string)
			if !isStr {
				sendErr(w, 400, "'content' must be a string.", "")
				return nil, false
			}
			content = s
		}
		encoding := "text"
		if v, present := obj["encoding"]; present && v != nil {
			s, isStr := v.(string)
			if !isStr {
				sendErr(w, 400, "Invalid encoding: (use text|base64).", "")
				return nil, false
			}
			encoding = strings.ToLower(s)
		}
		switch encoding {
		case "text":
			return []byte(content), true
		case "base64":
			if len(content)%4 != 0 || !b64StrictRe.MatchString(content) {
				sendErr(w, 400, "Invalid base64 'content'.", "")
				return nil, false
			}
			decoded, err := base64.StdEncoding.DecodeString(content)
			if err != nil {
				sendErr(w, 400, "Invalid base64 'content'.", "")
				return nil, false
			}
			return decoded, true
		default:
			sendErr(w, 400, fmt.Sprintf("Invalid encoding: '%s' (use text|base64).", encoding), "")
			return nil, false
		}
	}
	return raw, true
}

func mustB64(s string) []byte {
	decoded, _ := base64.StdEncoding.DecodeString(s)
	return decoded
}

var b64StrictRe = strictB64()

func strictB64() *regexp.Regexp {
	return regexp.MustCompile(`^[A-Za-z0-9+/]*={0,2}$`)
}

func handleSocketSend(w http.ResponseWriter, r *http.Request, q map[string][]string) {
	sid, sess, ok := sockLookup(w, q)
	if !ok {
		return
	}
	sockMu.Lock()
	attached := sess.wsAttached
	sockMu.Unlock()
	if attached {
		sendErr(w, 400, "Socket session is attached to a websocket (HTTP send refused while attached).", "")
		return
	}
	raw, err := readBody(r)
	if err != nil {
		sendErr(w, 400, "Cannot read body.", "")
		return
	}
	data, ok := sockSendBytes(w, raw, r.Header.Get("Content-Type"))
	if !ok {
		return
	}
	sess.conn.SetDeadline(time.Now().Add(30 * time.Second))
	_, err = writeFull(sess.conn, data)
	sess.conn.SetDeadline(time.Time{})
	if err != nil {
		sendErr(w, 400, fmt.Sprintf("Socket error: %v", err), "")
		return
	}
	sockMu.Lock()
	if _, present := sockSessions[sid]; present {
		sockSessions[sid].last = time.Now()
	}
	sockMu.Unlock()
	sendJSON(w, 200, map[string]any{"id": sid, "bytes": mustEnc(int64(len(data)))})
}

func writeFull(conn net.Conn, data []byte) (int, error) {
	total := 0
	for total < len(data) {
		n, err := conn.Write(data[total:])
		total += n
		if err != nil {
			return total, err
		}
	}
	return total, nil
}

func handleSocketRecv(w http.ResponseWriter, q map[string][]string) {
	timeout := sockDefaultRecvTimeout
	if raw := firstQuery(q, "timeout"); raw != "" {
		f, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
		if err != nil || !(f > 0 && f <= sockMaxRecvTimeout) {
			sendErr(w, 400, fmt.Sprintf("Invalid timeout param (seconds within (0, %d]).", int(sockMaxRecvTimeout)), "")
			return
		}
		timeout = f
	}
	maxBytes := sockDefaultMaxBytes
	if raw := firstQuery(q, "max_bytes"); raw != "" {
		n, err := strconv.Atoi(strings.TrimSpace(raw))
		if err != nil || n < 1 || n > sockMaxBytes {
			sendErr(w, 400, fmt.Sprintf("Invalid max_bytes param (1-%d).", sockMaxBytes), "")
			return
		}
		maxBytes = n
	}
	sid, sess, ok := sockLookup(w, q)
	if !ok {
		return
	}
	sockMu.Lock()
	attached := sess.wsAttached
	sockMu.Unlock()
	if attached {
		sendErr(w, 400, "Socket session is attached to a websocket (HTTP recv refused while attached).", "")
		return
	}
	if !sess.mu.TryLock() {
		sendErr(w, 400, "Another recv is already pending on this session.", "")
		return
	}
	defer sess.mu.Unlock()
	if sess.eof {
		sendJSON(w, 200, map[string]any{
			"id": sid, "encoding": "base64", "content": "",
			"bytes": mustEnc(int64(0)), "eof": mustEnc(true), "timed_out": mustEnc(false),
		})
		return
	}
	buf := make([]byte, maxBytes)
	sess.conn.SetReadDeadline(time.Now().Add(time.Duration(timeout * float64(time.Second))))
	n, err := sess.conn.Read(buf)
	sess.conn.SetReadDeadline(time.Time{})
	sockMu.Lock()
	if _, present := sockSessions[sid]; present {
		sockSessions[sid].last = time.Now()
	}
	sockMu.Unlock()
	if err != nil {
		if ne, ok := err.(net.Error); ok && ne.Timeout() {
			sendJSON(w, 200, map[string]any{
				"id": sid, "encoding": "base64", "content": "",
				"bytes": mustEnc(int64(0)), "eof": mustEnc(false), "timed_out": mustEnc(true),
			})
			return
		}
		if err.Error() == "EOF" || strings.Contains(err.Error(), "EOF") {
			sockMu.Lock()
			if s, present := sockSessions[sid]; present {
				s.eof = true
			}
			sockMu.Unlock()
			sendJSON(w, 200, map[string]any{
				"id": sid, "encoding": "base64", "content": "",
				"bytes": mustEnc(int64(0)), "eof": mustEnc(true), "timed_out": mustEnc(false),
			})
			return
		}
		sendErr(w, 400, fmt.Sprintf("Socket error: %v", err), "")
		return
	}
	sendJSON(w, 200, map[string]any{
		"id": sid, "encoding": "base64",
		"content": base64.StdEncoding.EncodeToString(buf[:n]),
		"bytes":   mustEnc(int64(n)), "eof": mustEnc(false), "timed_out": mustEnc(false),
	})
}

func handleSocketClose(w http.ResponseWriter, q map[string][]string) {
	sid, sess, ok := sockLookup(w, q)
	if !ok {
		return
	}
	sockMu.Lock()
	delete(sockSessions, sid)
	sockMu.Unlock()
	sess.conn.Close()
	sendJSON(w, 200, map[string]any{"id": sid, "closed": mustEnc(true)})
}

// handleSocketStarttls upgrades a plaintext session to TLS in place
// (STARTTLS-style protocols: the app drives the cleartext prelude
// itself, then calls this). Optional JSON body {sni?, ca_pem?};
// the server name defaults to the session host. The session lock is
// held across the handshake so a concurrent recv cannot interleave
// (a pending recv fails the upgrade with 400 instead). Refused
// while a WebSocket owns the connection, or when already TLS. A
// failed handshake leaves the plaintext session untouched.
func handleSocketStarttls(w http.ResponseWriter, r *http.Request, q map[string][]string) {
	sid, sess, ok := sockLookup(w, q)
	if !ok {
		return
	}
	sockMu.Lock()
	attached := sess.wsAttached
	alreadyTLS := sess.kind == "tls"
	host, port := sess.host, sess.port
	sockMu.Unlock()
	if attached {
		sendErr(w, 400, "Socket session is attached to a websocket (STARTTLS refused while attached).", "")
		return
	}
	if alreadyTLS {
		sendErr(w, 400, "Socket session is already TLS.", "")
		return
	}
	if !sess.mu.TryLock() {
		sendErr(w, 400, "Another recv is already pending on this session.", "")
		return
	}
	defer sess.mu.Unlock()
	serverName := host
	var rootCAs *x509.CertPool
	raw, err := readBody(r)
	if err != nil {
		sendErr(w, 400, "Cannot read body.", "")
		return
	}
	if len(bytes.TrimSpace(raw)) > 0 {
		if !strings.Contains(r.Header.Get("Content-Type"), "application/json") {
			sendErr(w, 400, "Content-Type must be application/json with body { sni?, ca_pem? }.", "")
			return
		}
		obj := map[string]any{}
		dec := json.NewDecoder(bytes.NewReader(raw))
		if err := dec.Decode(&obj); err != nil {
			sendErr(w, 400, "Invalid JSON body.", "")
			return
		}
		if obj == nil {
			sendErr(w, 400, "JSON body must be an object.", "")
			return
		}
		sni, caPEM, idErr := sockParseTLSIdentity(obj)
		if idErr != "" {
			sendErr(w, 400, idErr, "")
			return
		}
		if sni != "" {
			serverName = sni
		}
		if caPEM != "" {
			pool, err := sockCertPool(caPEM)
			if err != nil {
				sendErr(w, 400, "Field 'ca_pem' must be a valid PEM CA bundle.", "")
				return
			}
			rootCAs = pool
		}
	}
	tc, herr := sockTLSWrap(sess.conn, serverName, rootCAs,
		time.Duration(sockMaxConnectTimeout*float64(time.Second)))
	if herr != nil {
		sendErr(w, 400, fmt.Sprintf("TLS handshake with %s:%d failed: %v", host, port, herr), "")
		return
	}
	sockMu.Lock()
	if cur, present := sockSessions[sid]; present && cur == sess {
		sess.conn = tc
		sess.kind = "tls"
		sess.serverName = serverName
		sess.last = time.Now()
	}
	sockMu.Unlock()
	sendJSON(w, 200, map[string]any{"id": sid, "kind": "tls"})
}
