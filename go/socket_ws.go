package main

// WebSocket (RFC 6455) transport for socket sessions.
//
// GET /api/socket/ws?id=<sid> with `Upgrade: websocket` attaches a
// WebSocket to an existing session (opened via POST /api/socket/open;
// token rides `?token=` since browsers cannot set WS headers). After
// the 101 handshake, WS message payloads flow transparently
// client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back
// as WS binary frames; ping gets an auto-pong. A close from either
// side, or any TCP/protocol error, ends the whole session (a WS
// close is a session close, exactly like HTTP close): there is only
// ever one owner of the TCP stream, so no detach handoff can lose or
// duplicate bytes. HTTP send/recv on an attached session are refused
// (400) so two writers cannot interleave one stream; HTTP close
// still works and kills the bridge. Stdlib only: the Hijacker
// interface plus sha1/base64 framing, no new dependencies.

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	wsGUID       = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	wsMaxMessage = 4 * 1024 * 1024 // assembled client->server cap
	wsWriteSecs  = 30              // per-frame write deadline
)

const (
	wsCont  = 0x0
	wsText  = 0x1
	wsBin   = 0x2
	wsClose = 0x8
	wsPing  = 0x9
	wsPong  = 0xA
)

func wsAcceptKey(key string) string {
	h := sha1.New()
	h.Write([]byte(key + wsGUID))
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

// wsHasToken matches one comma-separated token, case-insensitively
// ("keep-alive, Upgrade" must satisfy an "upgrade" check).
func wsHasToken(headerVal, token string) bool {
	for _, part := range strings.Split(headerVal, ",") {
		if strings.EqualFold(strings.TrimSpace(part), token) {
			return true
		}
	}
	return false
}

// wsBridge owns the hijacked client connection; wmu serializes every
// write so the TCP->WS pump and inline pong/close replies cannot
// interleave frames.
type wsBridge struct {
	conn   net.Conn
	br     *bufio.Reader
	wmu    sync.Mutex
	closed bool // close frame already sent
}

func wsWriteFrame(w io.Writer, opcode byte, payload []byte) error {
	var hdr []byte
	switch {
	case len(payload) < 126:
		hdr = []byte{0x80 | opcode, byte(len(payload))}
	case len(payload) < 65536:
		hdr = []byte{0x80 | opcode, 126, 0, 0}
		binary.BigEndian.PutUint16(hdr[2:], uint16(len(payload)))
	default:
		hdr = []byte{0x80 | opcode, 127, 0, 0, 0, 0, 0, 0, 0, 0}
		binary.BigEndian.PutUint64(hdr[2:], uint64(len(payload)))
	}
	if _, err := w.Write(hdr); err != nil {
		return err
	}
	if len(payload) > 0 {
		if _, err := w.Write(payload); err != nil {
			return err
		}
	}
	return nil
}

func (b *wsBridge) sendFrame(opcode byte, payload []byte) error {
	b.wmu.Lock()
	defer b.wmu.Unlock()
	if b.closed {
		return fmt.Errorf("websocket already closed")
	}
	b.conn.SetWriteDeadline(time.Now().Add(wsWriteSecs * time.Second))
	err := wsWriteFrame(b.conn, opcode, payload)
	b.conn.SetWriteDeadline(time.Time{})
	return err
}

func (b *wsBridge) sendClose(code int, reason string) {
	b.wmu.Lock()
	defer b.wmu.Unlock()
	if b.closed {
		return
	}
	b.closed = true
	payload := make([]byte, 2+len(reason))
	binary.BigEndian.PutUint16(payload, uint16(code))
	copy(payload[2:], reason)
	b.conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	_ = wsWriteFrame(b.conn, wsClose, payload)
	b.conn.SetWriteDeadline(time.Time{})
}

var errWsProto = fmt.Errorf("websocket protocol error")

// readFrame parses one client frame. Control frames and protocol
// violations are answered inline; data frames come back raw.
// Parsing runs to completion BEFORE validation (bounded by the cap),
// so a fail echo goes out on a clean FIN instead of an RST (closing
// with kernel-unread data resets). Only an unbounded (> cap)
// declaration aborts mid-stream.
func (b *wsBridge) readFrame() (fin bool, opcode byte, payload []byte, isCtl bool, err error) {
	fail := func(code int) (bool, byte, []byte, bool, error) {
		b.sendClose(code, "")
		return false, 0, nil, false, errWsProto
	}
	hdr := make([]byte, 2)
	if _, err := io.ReadFull(b.br, hdr); err != nil {
		return false, 0, nil, false, err
	}
	fin = hdr[0]&0x80 != 0
	opcode = hdr[0] & 0x0F
	rsv := hdr[0]&0x70 != 0
	masked := hdr[1]&0x80 != 0
	length := int64(hdr[1] & 0x7F)
	switch length {
	case 126:
		var ext [2]byte
		if _, err := io.ReadFull(b.br, ext[:]); err != nil {
			return false, 0, nil, false, err
		}
		length = int64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err := io.ReadFull(b.br, ext[:]); err != nil {
			return false, 0, nil, false, err
		}
		length = int64(binary.BigEndian.Uint64(ext[:]))
		if length < 0 || length > wsMaxMessage {
			return fail(1009) // abortive: cannot bound
		}
	}
	isCtl = opcode >= 0x8
	if length > wsMaxMessage {
		return fail(1009) // abortive: cannot bound
	}
	// Unmasked client frames carry no key: consume payload only.
	payload = make([]byte, int(length))
	if masked {
		var key [4]byte
		if _, err := io.ReadFull(b.br, key[:]); err != nil {
			return false, 0, nil, false, err
		}
		if _, err := io.ReadFull(b.br, payload); err != nil {
			return false, 0, nil, false, err
		}
		for i := range payload {
			payload[i] ^= key[i%4]
		}
	} else {
		if _, err := io.ReadFull(b.br, payload); err != nil {
			return false, 0, nil, false, err
		}
	}
	if rsv {
		return fail(1002)
	}
	if !masked {
		return fail(1002) // clients MUST mask
	}
	if isCtl && (!fin || length > 125) {
		return fail(1002)
	}
	return fin, opcode, payload, isCtl, nil
}

// readMessage reassembles one full data message across fragments,
// answering ping inline. Protocol violations are echoed inline (the
// session is doomed anyway). A clean peer close is NOT echoed here:
// it returns closed=true with the echo code/reason so the caller can
// end the session BEFORE echoing — the echo then implies "gone".
func (b *wsBridge) readMessage() (byte, []byte, bool, int, string, error) {
	var msgOp byte
	var msg []byte
	started := false
	for {
		fin, opcode, payload, isCtl, err := b.readFrame()
		if err != nil {
			return 0, nil, false, 0, "", err
		}
		if isCtl {
			switch opcode {
			case wsClose:
				if len(payload) == 1 {
					b.sendClose(1002, "")
					return 0, nil, false, 0, "", errWsProto
				}
				code := 1000
				var reason string
				if len(payload) >= 2 {
					code = int(binary.BigEndian.Uint16(payload))
					reason = string(payload[2:])
				}
				return 0, nil, true, code, reason, nil
			case wsPing:
				_ = b.sendFrame(wsPong, payload)
				continue
			case wsPong:
				continue
			default:
				b.sendClose(1002, "")
				return 0, nil, false, 0, "", errWsProto
			}
		}
		if opcode == wsCont {
			if !started {
				b.sendClose(1002, "")
				return 0, nil, false, 0, "", errWsProto
			}
		} else if opcode == wsText || opcode == wsBin {
			if started {
				b.sendClose(1002, "")
				return 0, nil, false, 0, "", errWsProto
			}
			started = true
			msgOp = opcode
		} else {
			b.sendClose(1003, "")
			return 0, nil, false, 0, "", errWsProto
		}
		if int64(len(msg))+int64(len(payload)) > wsMaxMessage {
			b.sendClose(1009, "")
			return 0, nil, false, 0, "", errWsProto
		}
		msg = append(msg, payload...)
		if fin {
			if msgOp == wsText && !utf8.Valid(msg) {
				b.sendClose(1007, "")
				return 0, nil, false, 0, "", errWsProto
			}
			return msgOp, msg, false, 0, "", nil
		}
	}
}

func touchSession(sid string, sess *sockSession) {
	sockMu.Lock()
	if s, present := sockSessions[sid]; present && s == sess {
		s.last = time.Now()
	}
	sockMu.Unlock()
}

func markSessionEof(sid string, sess *sockSession) {
	sockMu.Lock()
	if s, present := sockSessions[sid]; present && s == sess {
		s.eof = true
		s.last = time.Now()
	}
	sockMu.Unlock()
}

// tcpToWs pumps peer bytes to the client as binary frames; any end
// of the TCP side (EOF or error) ends with a server close frame.
func (b *wsBridge) tcpToWs(sid string, sess *sockSession) {
	buf := make([]byte, 32768)
	for {
		n, err := sess.conn.Read(buf)
		if n > 0 {
			if werr := b.sendFrame(wsBin, buf[:n]); werr != nil {
				return
			}
			touchSession(sid, sess)
		}
		if err != nil {
			if err == io.EOF || strings.Contains(err.Error(), "EOF") {
				markSessionEof(sid, sess)
				b.sendClose(1000, "")
			} else {
				b.sendClose(1011, "")
			}
			return
		}
	}
}

// wsToTcp forwards assembled client messages to the peer. It ends on
// client close, protocol error, TCP write failure, or a dead bridge.
func (b *wsBridge) wsToTcp(sid string, sess *sockSession) {
	for {
		_, payload, closed, code, reason, err := b.readMessage()
		if err != nil || closed {
			// End the session BEFORE echoing a clean close, so the
			// echo deterministically implies "gone" (no race with a
			// client that HTTP-probes right after the handshake).
			// Violation echoes already went out inline (code == 0).
			wsCloseSession(sid, sess)
			if closed {
				b.sendClose(code, reason)
			}
			return
		}
		sess.conn.SetDeadline(time.Now().Add(30 * time.Second))
		_, werr := writeFull(sess.conn, payload)
		sess.conn.SetDeadline(time.Time{})
		if werr != nil {
			return
		}
		touchSession(sid, sess)
	}
}

func wsUnmark(sid string, sess *sockSession) {
	sockMu.Lock()
	if s, present := sockSessions[sid]; present && s == sess {
		s.wsAttached = false
		s.last = time.Now()
	}
	sockMu.Unlock()
}

// wsCloseSession ends the TCP session when a WS bridge ends, for any
// reason and from either side: a WebSocket close is a session close
// (same as HTTP close). Idempotent: HTTP close may have won the race.
func wsCloseSession(sid string, sess *sockSession) {
	sockMu.Lock()
	if s, present := sockSessions[sid]; present && s == sess {
		s.wsAttached = false
		delete(sockSessions, sid)
	}
	sockMu.Unlock()
	sess.conn.Close() // unblocks the TCP pump; repeated Close is safe
}

func handleSocketWs(w http.ResponseWriter, r *http.Request, q map[string][]string) {
	if !wsHasToken(r.Header.Get("Connection"), "upgrade") ||
		!strings.EqualFold(strings.TrimSpace(r.Header.Get("Upgrade")), "websocket") {
		sendErr(w, 400, "This route requires a WebSocket upgrade (Upgrade: websocket).", "")
		return
	}
	if strings.TrimSpace(r.Header.Get("Sec-WebSocket-Version")) != "13" {
		sendErr(w, 400, "Unsupported WebSocket version (need 13).", "")
		return
	}
	if strings.TrimSpace(r.Header.Get("Sec-WebSocket-Key")) == "" {
		sendErr(w, 400, "Missing Sec-WebSocket-Key.", "")
		return
	}
	sid, sess, ok := sockLookup(w, q)
	if !ok {
		return
	}
	sockMu.Lock()
	if sess.wsAttached {
		sockMu.Unlock()
		sendErr(w, 400, "Socket session is already attached to a websocket.", "")
		return
	}
	if !sess.mu.TryLock() {
		sockMu.Unlock()
		sendErr(w, 400, "Another recv is already pending on this session.", "")
		return
	}
	sess.mu.Unlock()
	sess.wsAttached = true
	sess.last = time.Now()
	sockMu.Unlock()

	hj, ok := w.(http.Hijacker)
	if !ok {
		wsUnmark(sid, sess)
		sendErr(w, 500, "Internal error: connection hijack not supported.", "")
		return
	}
	conn, rw, err := hj.Hijack()
	if err != nil {
		wsUnmark(sid, sess)
		return
	}
	resp := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + wsAcceptKey(strings.TrimSpace(r.Header.Get("Sec-WebSocket-Key"))) + "\r\n\r\n"
	if _, err := rw.WriteString(resp); err != nil {
		conn.Close()
		wsUnmark(sid, sess)
		return
	}
	if err := rw.Flush(); err != nil {
		conn.Close()
		wsUnmark(sid, sess)
		return
	}

	sess.conn.SetDeadline(time.Time{}) // no leftover HTTP timeouts
	b := &wsBridge{conn: conn, br: rw.Reader}
	done := make(chan struct{})
	var once sync.Once
	forceClose := func() {
		once.Do(func() {
			close(done)
			conn.Close() // unblocks the WS reader; TCP reader drains via frame-write failure
		})
	}
	go func() {
		b.tcpToWs(sid, sess)
		// TCP side ended (close frame already sent): give the peer a
		// grace period to echo close, then force-detach.
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			forceClose()
		}
	}()
	b.wsToTcp(sid, sess)
	forceClose()
	// A WebSocket close is a session close (same as HTTP close): the
	// TCP session ends here no matter which side hung up first.
	wsCloseSession(sid, sess)
}
