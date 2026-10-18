<?php
// RFC 6455 framing helpers (shared by the WS upgrade path).
//
// Same contract as the Go/Python ports: text frames arrive as UTF-8
// bytes, binary frames opaque; ping gets an auto-pong; control
// frames are max 125 bytes and never fragmented; client frames MUST
// be masked; assembled client->server messages are capped at 4 MB
// (close 1009); any protocol violation ends with 1002/1003/1007.

const G_WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const G_WS_MAX_MESSAGE = 4 * 1024 * 1024;

const G_WS_CONT = 0x0;
const G_WS_TEXT = 0x1;
const G_WS_BIN = 0x2;
const G_WS_CLOSE = 0x8;
const G_WS_PING = 0x9;
const G_WS_PONG = 0xA;

function g_ws_accept_key(string $key): string {
    return base64_encode(
        hash('sha1', $key . G_WS_GUID, true));
}

/** Match one comma-separated token, case-insensitively. */
function g_ws_has_token(string $headerVal, string $token): bool {
    foreach (explode(',', $headerVal) as $part) {
        if (strtolower(trim($part)) === $token) return true;
    }
    return false;
}

/** Encode one server frame (never masked). */
function g_ws_encode_frame(int $opcode, string $payload): string {
    $len = strlen($payload);
    if ($len < 126) {
        $hdr = chr(0x80 | $opcode) . chr($len);
    } elseif ($len < 65536) {
        $hdr = chr(0x80 | $opcode) . chr(126) . pack('n', $len);
    } else {
        // 64-bit length: high word is always 0 here (cap is 4 MB,
        // but TCP->WS chunks stay small anyway).
        $hdr = chr(0x80 | $opcode) . chr(127) .
            pack('N', 0) . pack('N', $len);
    }
    return $hdr . $payload;
}

function g_ws_encode_close(int $code, string $reason = ''): string {
    return g_ws_encode_frame(G_WS_CLOSE, pack('n', $code) . $reason);
}

class GWsProtoError extends Exception {
}

/**
 * Incremental client-frame decoder.
 *
 * Feed raw bytes via feed(); complete data messages come out of
 * pollMessages() as [opcode, payload] pairs while control frames
 * are answered through the $emit callback (pong/close bytes to
 * write). Protocol violations throw GWsProtoError AFTER the fail
 * echo has been emitted (clean FIN, no RST); an unbounded length
 * declaration throws immediately (abortive).
 *
 * A clean peer close is reported as ['close', code, reason] so the
 * caller can end the session BEFORE echoing (delete-before-echo).
 */
class GWsDecoder {
    private string $buf = '';
    private bool $started = false;
    private int $msgOp = 0;
    private string $msg = '';
    /** @var callable(string): void */
    private $emit;

    /** @param callable(string): void $emit */
    public function __construct(callable $emit) {
        $this->emit = $emit;
    }

    private function fail(int $code): never {
        ($this->emit)(g_ws_encode_close($code));
        throw new GWsProtoError("websocket protocol error ($code)");
    }

    /**
     * @return list<array{0:string,1:mixed,2:mixed}>
     *   each item is ['message', opcode, payload] or
     *   ['close', code, reason].
     */
    public function feed(string $chunk): array {
        $this->buf .= $chunk;
        $out = [];
        while (true) {
            $frame = $this->tryFrame();
            if ($frame === null) break;
            [$fin, $opcode, $payload, $isCtl] = $frame;
            if ($isCtl) {
                if ($opcode === G_WS_CLOSE) {
                    if (strlen($payload) === 1) $this->fail(1002);
                    $code = 1000;
                    $reason = '';
                    if (strlen($payload) >= 2) {
                        $code = unpack('n', substr($payload, 0, 2))[1];
                        $reason = substr($payload, 2);
                    }
                    $out[] = ['close', $code, $reason];
                    continue;
                }
                if ($opcode === G_WS_PING) {
                    ($this->emit)(
                        g_ws_encode_frame(G_WS_PONG, $payload));
                    continue;
                }
                if ($opcode === G_WS_PONG) continue;
                $this->fail(1002);
            }
            if ($opcode === G_WS_CONT) {
                if (!$this->started) $this->fail(1002);
            } elseif ($opcode === G_WS_TEXT || $opcode === G_WS_BIN) {
                if ($this->started) $this->fail(1002);
                $this->started = true;
                $this->msgOp = $opcode;
            } else {
                $this->fail(1003);
            }
            if (strlen($this->msg) + strlen($payload) >
                    G_WS_MAX_MESSAGE) {
                // Emit before throwing (clean FIN): the session is
                // doomed anyway, but the peer deserves the code.
                ($this->emit)(g_ws_encode_close(1009));
                throw new GWsProtoError('message too large');
            }
            $this->msg .= $payload;
            if ($fin) {
                if ($this->msgOp === G_WS_TEXT &&
                        !mb_check_encoding($this->msg, 'UTF-8')) {
                    $this->fail(1007);
                }
                $out[] = ['message', $this->msgOp, $this->msg];
                $this->started = false;
                $this->msg = '';
            }
        }
        return $out;
    }

    /** @return array{0:bool,1:int,2:string,3:bool}|null */
    private function tryFrame(): ?array {
        if (strlen($this->buf) < 2) return null;
        $b1 = ord($this->buf[0]);
        $b2 = ord($this->buf[1]);
        $fin = ($b1 & 0x80) !== 0;
        $opcode = $b1 & 0x0F;
        $rsv = ($b1 & 0x70) !== 0;
        $masked = ($b2 & 0x80) !== 0;
        $length = $b2 & 0x7F;
        $off = 2;
        if ($length === 126) {
            if (strlen($this->buf) < 4) return null;
            $length = unpack('n', substr($this->buf, 2, 2))[1];
            $off = 4;
        } elseif ($length === 127) {
            if (strlen($this->buf) < 10) return null;
            $hi = unpack('N', substr($this->buf, 2, 4))[1];
            $lo = unpack('N', substr($this->buf, 6, 4))[1];
            if ($hi !== 0 || $lo > G_WS_MAX_MESSAGE) {
                // Unbounded declaration: abort mid-stream (1009).
                ($this->emit)(g_ws_encode_close(1009));
                throw new GWsProtoError('frame too large');
            }
            $length = $lo;
            $off = 10;
        }
        $isCtl = $opcode >= 0x8;
        if ($length > G_WS_MAX_MESSAGE) $this->fail(1009);
        if ($masked) {
            if (strlen($this->buf) < $off + 4) return null;
            $mask = substr($this->buf, $off, 4);
            $off += 4;
        } else {
            $mask = null;
        }
        if (strlen($this->buf) < $off + $length) return null;
        $payload = substr($this->buf, $off, $length);
        $this->buf = substr($this->buf, $off + $length);
        if ($mask !== null) {
            for ($i = 0; $i < $length; $i++) {
                $payload[$i] = $payload[$i] ^ $mask[$i % 4];
            }
        }
        // Validation AFTER full parse (bounded), so fail echoes go
        // out on a clean FIN.
        if ($rsv) $this->fail(1002);
        if ($mask === null) $this->fail(1002); // clients MUST mask
        if ($isCtl && (!$fin || $length > 125)) $this->fail(1002);
        return [$fin, $opcode, $payload, $isCtl];
    }
}
