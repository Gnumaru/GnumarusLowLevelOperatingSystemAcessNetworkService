// WebSocket (RFC 6455) transport for socket sessions.
//
// GET /api/socket/ws?id=<sid> with `Upgrade: websocket` attaches a
// WebSocket to an existing session (opened via POST /api/socket/open;
// token rides `?token=` since browsers cannot set WS headers). After
// the 101 handshake, WS message payloads flow transparently
// client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back as
// binary frames; ping gets an auto-pong. A close from either side, or
// any TCP/protocol error, ends the whole session (a WS close is a
// session close, exactly like HTTP close): only one owner ever holds
// the TCP stream, so no detach handoff can lose or duplicate bytes.
// HTTP send/recv/starttls on an attached session are refused (400);
// HTTP close still works and kills the bridge.
//
// Ported from the Go reference (go/socket_ws.go) so the client-visible
// behavior is byte-for-byte the same as the other ports. The upgrade
// hijacks the raw client connection owned by Http.kt.

import java.net.Socket
import java.security.MessageDigest
import java.util.Base64
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

const val WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
const val WS_MAX_MESSAGE = 4 * 1024 * 1024 // assembled client->server cap

const val WS_CONT = 0x0
const val WS_TEXT = 0x1
const val WS_BIN = 0x2
const val WS_CLOSE = 0x8
const val WS_PING = 0x9
const val WS_PONG = 0xA

class WsProtoError(msg: String) : Exception(msg)

fun wsAcceptKey(key: String): String {
    val digest = MessageDigest.getInstance("SHA-1")
        .digest((key + WS_GUID).toByteArray(Charsets.US_ASCII))
    return Base64.getEncoder().encodeToString(digest)
}

/** Matches one comma-separated token, case-insensitively. */
fun wsHasToken(headerValue: String?, token: String): Boolean =
    (headerValue ?: "").split(",").any { it.trim().equals(token, ignoreCase = true) }

fun wsValidUtf8(bytes: ByteArray): Boolean = try {
    java.nio.charset.StandardCharsets.UTF_8.newDecoder()
        .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
        .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
        .decode(java.nio.ByteBuffer.wrap(bytes))
    true
} catch (e: Exception) {
    false
}

class WsFrame(val fin: Boolean, val opcode: Int, val payload: ByteArray, val isCtl: Boolean)

class WsMessage(
    val opcode: Int, val payload: ByteArray,
    val closed: Boolean, val code: Int, val reason: ByteArray
)

/** Exact-length reader that first drains bytes the HTTP layer buffered. */
class WsReader(private val sock: Socket, initial: ByteArray) {
    private val input = sock.getInputStream()
    private var buf = initial.copyOf()
    private var off = 0

    private fun fill(): Boolean {
        val tmp = ByteArray(65536)
        val n = try {
            input.read(tmp)
        } catch (e: Exception) {
            -1
        }
        if (n <= 0) return false
        val remain = buf.size - off
        val nb = ByteArray(remain + n)
        System.arraycopy(buf, off, nb, 0, remain)
        System.arraycopy(tmp, 0, nb, remain, n)
        buf = nb
        off = 0
        return true
    }

    /** Exactly n bytes, or null on EOF before n arrived. */
    fun readExact(n: Int): ByteArray? {
        if (n == 0) return ByteArray(0)
        while (buf.size - off < n) {
            if (!fill()) return null
        }
        val out = buf.copyOfRange(off, off + n)
        off += n
        return out
    }
}

/** Owns a hijacked client connection; [wmu] serializes every write. */
class WsBridge(private val sock: Socket, initial: ByteArray) {
    private val reader = WsReader(sock, initial)
    private val output = sock.getOutputStream()
    private val wmu = Any()
    @Volatile private var closed = false

    private fun writeFrame(opcode: Int, payload: ByteArray) {
        val header: ByteArray
        if (payload.size < 126) {
            header = byteArrayOf((0x80 or opcode).toByte(), payload.size.toByte())
        } else if (payload.size < 65536) {
            header = ByteArray(4)
            header[0] = (0x80 or opcode).toByte()
            header[1] = 126
            header[2] = ((payload.size ushr 8) and 0xFF).toByte()
            header[3] = (payload.size and 0xFF).toByte()
        } else {
            header = ByteArray(10)
            header[0] = (0x80 or opcode).toByte()
            header[1] = 127
            var v = payload.size.toLong()
            for (i in 9 downTo 2) {
                header[i] = (v and 0xFF).toByte()
                v = v ushr 8
            }
        }
        output.write(header)
        if (payload.isNotEmpty()) output.write(payload)
        output.flush()
    }

    fun sendFrame(opcode: Int, payload: ByteArray): Boolean {
        synchronized(wmu) {
            if (closed) return false
            return try {
                writeFrame(opcode, payload)
                true
            } catch (e: Exception) {
                false
            }
        }
    }

    fun sendClose(code: Int, reason: ByteArray) {
        synchronized(wmu) {
            if (closed) return
            closed = true
            val payload = ByteArray(2 + reason.size)
            payload[0] = ((code ushr 8) and 0xFF).toByte()
            payload[1] = (code and 0xFF).toByte()
            System.arraycopy(reason, 0, payload, 2, reason.size)
            try {
                writeFrame(WS_CLOSE, payload)
            } catch (_: Exception) {
            }
        }
    }

    private fun fail(code: Int): Nothing {
        sendClose(code, ByteArray(0))
        throw WsProtoError("websocket protocol error")
    }

    /**
     * One client frame. Parsing runs to completion BEFORE validation
     * (bounded by the cap), so a fail echo goes out on a clean FIN
     * instead of an RST. Only an unbounded declaration aborts
     * mid-stream.
     */
    fun readFrame(): WsFrame {
        val hdr = reader.readExact(2) ?: throw WsProtoError("websocket read error")
        val b0 = hdr[0].toInt() and 0xFF
        val b1 = hdr[1].toInt() and 0xFF
        val fin = (b0 and 0x80) != 0
        val opcode = b0 and 0x0F
        val rsv = (b0 and 0x70) != 0
        val masked = (b1 and 0x80) != 0
        var length = (b1 and 0x7F).toLong()
        if (length == 126L) {
            val ext = reader.readExact(2) ?: throw WsProtoError("websocket read error")
            length = (((ext[0].toInt() and 0xFF) shl 8) or (ext[1].toInt() and 0xFF)).toLong()
        } else if (length == 127L) {
            val ext = reader.readExact(8) ?: throw WsProtoError("websocket read error")
            var v = 0L
            for (b in ext) v = (v shl 8) or (b.toInt() and 0xFF).toLong()
            length = v
            if (length < 0 || length > WS_MAX_MESSAGE) fail(1009) // abortive: cannot bound
        }
        val isCtl = opcode >= 0x08
        if (length > WS_MAX_MESSAGE) fail(1009) // abortive: cannot bound
        val payload = if (masked) {
            val key = reader.readExact(4) ?: throw WsProtoError("websocket read error")
            val body = reader.readExact(length.toInt()) ?: throw WsProtoError("websocket read error")
            for (i in body.indices) {
                body[i] = (body[i].toInt() xor (key[i % 4].toInt() and 0xFF)).toByte()
            }
            body
        } else {
            reader.readExact(length.toInt()) ?: throw WsProtoError("websocket read error")
        }
        if (rsv) fail(1002)
        if (!masked) fail(1002) // clients MUST mask
        if (isCtl && (!fin || length > 125)) fail(1002)
        return WsFrame(fin, opcode, payload, isCtl)
    }

    /**
     * Reassembles one full data message across fragments, answering
     * ping inline. Protocol violations are echoed inline (the session
     * is doomed anyway). A clean peer close is NOT echoed here: it is
     * returned so the caller can end the session BEFORE echoing — the
     * echo then implies "gone".
     */
    fun readMessage(): WsMessage {
        var msgOp = 0
        val parts = ArrayList<ByteArray>()
        var total = 0
        var started = false
        while (true) {
            val f = readFrame()
            if (f.isCtl) {
                when (f.opcode) {
                    WS_CLOSE -> {
                        if (f.payload.size == 1) {
                            sendClose(1002, ByteArray(0))
                            throw WsProtoError("bad close payload")
                        }
                        var code = 1000
                        var reason = ByteArray(0)
                        if (f.payload.size >= 2) {
                            code = ((f.payload[0].toInt() and 0xFF) shl 8) or
                                (f.payload[1].toInt() and 0xFF)
                            reason = f.payload.copyOfRange(2, f.payload.size)
                        }
                        return WsMessage(0, ByteArray(0), true, code, reason)
                    }
                    WS_PING -> {
                        if (!sendFrame(WS_PONG, f.payload)) throw WsProtoError("dead bridge")
                        continue
                    }
                    WS_PONG -> continue
                    else -> {
                        sendClose(1002, ByteArray(0))
                        throw WsProtoError("bad control opcode")
                    }
                }
            }
            if (f.opcode == WS_CONT) {
                if (!started) {
                    sendClose(1002, ByteArray(0))
                    throw WsProtoError("orphan continuation")
                }
            } else if (f.opcode == WS_TEXT || f.opcode == WS_BIN) {
                if (started) {
                    sendClose(1002, ByteArray(0))
                    throw WsProtoError("interleaved message")
                }
                started = true
                msgOp = f.opcode
            } else {
                sendClose(1003, ByteArray(0))
                throw WsProtoError("bad data opcode")
            }
            total += f.payload.size
            if (total > WS_MAX_MESSAGE) {
                sendClose(1009, ByteArray(0))
                throw WsProtoError("message too big")
            }
            parts.add(f.payload)
            if (f.fin) {
                val msg = ByteArray(total)
                var o = 0
                for (p in parts) {
                    System.arraycopy(p, 0, msg, o, p.size)
                    o += p.size
                }
                if (msgOp == WS_TEXT && !wsValidUtf8(msg)) {
                    sendClose(1007, ByteArray(0))
                    throw WsProtoError("bad utf-8")
                }
                return WsMessage(msgOp, msg, false, 0, ByteArray(0))
            }
        }
    }

    /** Peer bytes -> client binary frames; any TCP end closes the WS. */
    fun tcpToWs(sid: String, sess: SockSession) {
        val buf = ByteArray(32768)
        val inStream = try {
            sess.sock.getInputStream()
        } catch (e: Exception) {
            sendClose(1011, ByteArray(0))
            return
        }
        while (true) {
            val n = try {
                inStream.read(buf)
            } catch (e: Exception) {
                sendClose(1011, ByteArray(0))
                return
            }
            if (n > 0) {
                if (!sendFrame(WS_BIN, buf.copyOf(n))) return
                touchSession(sid, sess)
                continue
            }
            // n < 0: clean EOF from the peer.
            markSessionEof(sid, sess)
            sendClose(1000, ByteArray(0))
            return
        }
    }

    /** Client messages -> peer; ends on close, error or a dead bridge. */
    fun wsToTcp(sid: String, sess: SockSession) {
        while (true) {
            val msg = try {
                readMessage()
            } catch (e: Exception) {
                // Violation echoes already went out inline (or the
                // transport died: nothing to echo to).
                wsCloseSession(sid, sess)
                return
            }
            if (msg.closed) {
                // End the session BEFORE echoing, so the echo
                // deterministically implies "gone".
                wsCloseSession(sid, sess)
                sendClose(msg.code, msg.reason)
                return
            }
            try {
                val out = sess.sock.getOutputStream()
                out.write(msg.payload)
                out.flush()
            } catch (e: Exception) {
                wsCloseSession(sid, sess)
                return
            }
            touchSession(sid, sess)
        }
    }
}

fun touchSession(sid: String, sess: SockSession) {
    if (SOCK_SESSIONS[sid] === sess) sess.last = System.currentTimeMillis()
}

fun markSessionEof(sid: String, sess: SockSession) {
    if (SOCK_SESSIONS[sid] === sess) {
        sess.eof = true
        sess.last = System.currentTimeMillis()
    }
}

/** Clear the attached flag after a failed handshake (identity-checked). */
fun wsUnmark(sid: String, sess: SockSession) {
    if (SOCK_SESSIONS[sid] === sess) {
        sess.wsAttached = false
        sess.last = System.currentTimeMillis()
    }
}

/**
 * End the TCP session when a WS bridge ends, for any reason and from
 * either side: a WebSocket close is a session close (same as HTTP
 * close). Idempotent: HTTP close may have won the race.
 */
fun wsCloseSession(sid: String, sess: SockSession) {
    if (SOCK_SESSIONS[sid] === sess) {
        sess.wsAttached = false
        SOCK_SESSIONS.remove(sid)
    }
    try {
        sess.sock.close()
    } catch (_: Exception) {
    }
}

/**
 * Runs one attached WebSocket bridge: 101 already sent, [sock] is the
 * hijacked client connection. Returns when either side is done; the
 * session is always closed here.
 */
fun runWsBridge(sid: String, sess: SockSession, sock: Socket, leftover: ByteArray) {
    val bridge = WsBridge(sock, leftover)
    val done = CountDownLatch(1)
    val tcpThread = Thread {
        try {
            bridge.tcpToWs(sid, sess)
        } catch (_: Exception) {
        }
        // TCP ended (close frame already sent): give the peer a grace
        // period to echo close, then force-detach.
        try {
            done.await(2, TimeUnit.SECONDS)
        } catch (_: Exception) {
        }
        if (done.count > 0L) {
            try {
                sock.close()
            } catch (_: Exception) {
            }
        }
    }
    tcpThread.isDaemon = true
    tcpThread.start()
    try {
        bridge.wsToTcp(sid, sess)
    } catch (_: Exception) {
    } finally {
        done.countDown()
        try {
            sock.close()
        } catch (_: Exception) {
        }
        wsCloseSession(sid, sess)
    }
}
