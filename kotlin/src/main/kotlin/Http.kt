// Hand-rolled HTTP/1.1 server (one request per connection, always
// `Connection: close`) so the socket proxy can hijack the raw TCP
// connection for WebSocket upgrades. The JDK `HttpServer` exposes no
// raw socket, which is the only reason the Kotlin port lacked
// `socket_ws`; owning the TCP layer (like Ruby/PHP/C++) removes that
// limitation without adding any dependency.
//
// The handler code talks to a tiny `Exchange` that mirrors the exact
// subset of `com.sun.net.httpserver.HttpExchange` it used to call
// (`requestMethod`, `requestHeaders.getFirst`, `requestURI.path` /
// `.rawQuery`, `requestBody.readAllBytes`, `responseHeaders.set`,
// `sendResponseHeaders`, `responseBody`, `close`), plus `hijack()`.

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.Executors

/** Case-insensitive header bag; preserves insertion order on output. */
class HeaderMap {
    private val items = ArrayList<Pair<String, String>>()

    fun set(name: String, value: String) {
        items.removeAll { it.first.equals(name, ignoreCase = true) }
        items.add(name to value)
    }

    fun add(name: String, value: String) {
        items.add(name to value)
    }

    /** First match; repeated request headers are joined with ", ". */
    fun getFirst(name: String): String? {
        val matches = items.filter { it.first.equals(name, ignoreCase = true) }
        if (matches.isEmpty()) return null
        return matches.joinToString(", ") { it.second }
    }

    fun all(): List<Pair<String, String>> = items
}

/** Buffered byte reader/writer over one accepted client connection. */
class Conn(val sock: Socket) {
    private val input: InputStream = sock.getInputStream()
    private val output: OutputStream = sock.getOutputStream()
    private val buf = ByteArray(16384)
    private var pos = 0
    private var len = 0

    /** Next byte (0-255), or -1 at EOF. */
    fun readByte(): Int {
        if (pos >= len) {
            len = try {
                input.read(buf)
            } catch (e: Exception) {
                -1
            }
            pos = 0
            if (len <= 0) return -1
        }
        return buf[pos++].toInt() and 0xFF
    }

    /** One CRLF- (or LF-) terminated line; null at EOF with no data. */
    fun readLine(): String? {
        val sb = StringBuilder()
        while (true) {
            val b = readByte()
            if (b < 0) return if (sb.isEmpty()) null else sb.toString()
            if (b == '\n'.code) {
                if (sb.isNotEmpty() && sb.last() == '\r') sb.setLength(sb.length - 1)
                return sb.toString()
            }
            sb.append(b.toChar())
        }
    }

    /** Exactly n bytes (drains buffered bytes first). */
    fun readN(n: Int): ByteArray {
        val out = ByteArrayOutputStream(n)
        var remaining = n
        val buffered = minOf(remaining, len - pos)
        if (buffered > 0) {
            out.write(buf, pos, buffered)
            pos += buffered
            remaining -= buffered
        }
        val tmp = ByteArray(65536)
        while (remaining > 0) {
            val r = try {
                input.read(tmp, 0, minOf(remaining, tmp.size))
            } catch (e: Exception) {
                -1
            }
            if (r <= 0) break
            out.write(tmp, 0, r)
            remaining -= r
        }
        return out.toByteArray()
    }

    /** Bytes already read past the request head (WebSocket pipelining). */
    fun leftover(): ByteArray =
        if (pos < len) buf.copyOfRange(pos, len) else ByteArray(0)

    fun write(data: ByteArray) {
        output.write(data)
        output.flush()
    }

    fun close() {
        try {
            sock.close()
        } catch (_: Exception) {
        }
    }
}

/** Minimal request-URI view (the subset the routing code reads). */
class ReqUri(val path: String, val rawQuery: String?)

fun reason(code: Int): String = when (code) {
    101 -> "Switching Protocols"
    200 -> "OK"
    201 -> "Created"
    204 -> "No Content"
    400 -> "Bad Request"
    401 -> "Unauthorized"
    403 -> "Forbidden"
    404 -> "Not Found"
    405 -> "Method Not Allowed"
    409 -> "Conflict"
    500 -> "Internal Server Error"
    503 -> "Service Unavailable"
    else -> "Error"
}

/**
 * One request/response pair over [conn]. Response bytes are buffered
 * and written on [close] (or not at all when [hijack] took the socket),
 * so the order of `sendResponseHeaders`/body writes never matters.
 */
class Exchange internal constructor(
    val conn: Conn,
    val requestMethod: String,
    val requestURI: ReqUri,
    val requestHeaders: HeaderMap,
    bodyBytes: ByteArray,
) {
    val requestBody: InputStream = ByteArrayInputStream(bodyBytes)
    val responseHeaders = HeaderMap()

    private var status = 0
    private var bodyBuf: ByteArrayOutputStream? = null
    private var closed = false
    private var hijacked = false

    fun sendResponseHeaders(code: Int, length: Long) {
        status = code
        if (bodyBuf == null) bodyBuf = ByteArrayOutputStream()
    }

    val responseBody: OutputStream
        get() {
            if (bodyBuf == null) bodyBuf = ByteArrayOutputStream()
            return bodyBuf!!
        }

    fun close() {
        if (closed) return
        closed = true
        if (hijacked) return
        val code = if (status == 0) 200 else status
        val body = bodyBuf?.toByteArray() ?: ByteArray(0)
        val head = StringBuilder()
        head.append("HTTP/1.1 ").append(code).append(' ')
            .append(reason(code)).append("\r\n")
        var hasLength = false
        for ((k, v) in responseHeaders.all()) {
            head.append(k).append(": ").append(v).append("\r\n")
            if (k.equals("Content-Length", ignoreCase = true)) hasLength = true
        }
        if (code != 204 && !hasLength) {
            head.append("Content-Length: ").append(body.size).append("\r\n")
        }
        head.append("Connection: close\r\n\r\n")
        val headBytes = head.toString().toByteArray(Charsets.ISO_8859_1)
        val out = if (code == 204) headBytes else headBytes + body
        try {
            conn.write(out)
        } catch (_: Exception) {
        }
        conn.close()
    }

    /** Take over the raw socket for an HTTP upgrade; [close] then no-ops. */
    fun hijack(): Socket {
        hijacked = true
        return conn.sock
    }

    /** Bytes already read past the request head (WS pipelining). */
    fun leftoverBytes(): ByteArray = conn.leftover()
}

/**
 * Accept loop: one virtual thread per connection, one request per
 * connection (the response carries `Connection: close`).
 */
class WebServer(val host: String, val port: Int, val handler: (Exchange) -> Unit) {
    private val server = ServerSocket()

    fun start() {
        server.reuseAddress = true
        server.bind(InetSocketAddress(host, port), 128)
        val exec = Executors.newVirtualThreadPerTaskExecutor()
        exec.execute {
            while (!server.isClosed) {
                val sock = try {
                    server.accept()
                } catch (e: Exception) {
                    break
                }
                try {
                    sock.tcpNoDelay = true
                } catch (_: Exception) {
                }
                exec.execute { serve(sock) }
            }
        }
    }

    private fun serve(sock: Socket) {
        val conn = Conn(sock)
        var ex: Exchange? = null
        try {
            ex = readRequest(conn)
            if (ex == null) {
                conn.close()
                return
            }
            handler(ex)
        } catch (e: Exception) {
            // The handler guards its own errors; this covers a malformed
            // request or a failure while parsing.
            try {
                writeBadRequest(conn)
            } catch (_: Exception) {
            }
        } finally {
            try {
                ex?.close()
            } catch (_: Exception) {
            }
        }
    }

    private fun writeBadRequest(conn: Conn) {
        val body = "{\"error\":\"bad_request\",\"message\":\"Malformed HTTP request.\"}"
            .toByteArray(Charsets.UTF_8)
        val head = ("HTTP/1.1 400 Bad Request\r\n" +
            "Content-Type: application/json; charset=utf-8\r\n" +
            "Content-Length: " + body.size + "\r\n" +
            "Connection: close\r\n\r\n").toByteArray(Charsets.ISO_8859_1)
        conn.write(head + body)
        conn.close()
    }

    private fun readRequest(conn: Conn): Exchange? {
        val requestLine = conn.readLine() ?: return null
        if (requestLine.isBlank()) return null
        val parts = requestLine.split(" ")
        if (parts.size < 2) return null
        val method = parts[0]
        val target = parts[1]

        val headers = HeaderMap()
        while (true) {
            val line = conn.readLine() ?: break
            if (line.isEmpty()) break
            val idx = line.indexOf(':')
            if (idx <= 0) continue
            headers.add(line.substring(0, idx).trim(), line.substring(idx + 1).trim())
        }

        var body = ByteArray(0)
        val cl = headers.getFirst("Content-Length")?.trim()
        val clLen = cl?.toIntOrNull()
        if (clLen != null && clLen > 0) {
            body = conn.readN(clLen)
        } else if (headers.getFirst("Transfer-Encoding")?.lowercase()?.contains("chunked") == true) {
            body = readChunked(conn)
        }

        val qIdx = target.indexOf('?')
        val rawPath = if (qIdx < 0) target else target.substring(0, qIdx)
        val rawQuery = if (qIdx < 0) null else target.substring(qIdx + 1)
        return Exchange(conn, method, ReqUri(decodePath(rawPath), rawQuery), headers, body)
    }

    private fun decodePath(p: String): String {
        if ('%' !in p) return p
        return try {
            java.net.URI(p).path ?: p
        } catch (e: Exception) {
            p
        }
    }

    private fun readChunked(conn: Conn): ByteArray {
        val out = ByteArrayOutputStream()
        while (true) {
            val sizeLine = conn.readLine() ?: break
            val size = sizeLine.trim().substringBefore(';').trim().toIntOrNull(16) ?: break
            if (size == 0) {
                while (true) {
                    val t = conn.readLine() ?: break
                    if (t.isEmpty()) break
                }
                break
            }
            out.write(conn.readN(size))
            conn.readLine() // CRLF after the chunk data
        }
        return out.toByteArray()
    }
}
