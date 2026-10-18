<?php
// Manual HTTP/1.1 server on ReactPHP's socket component.
//
// Why manual instead of react/http: the contract needs raw-socket
// hijacking (WebSocket upgrades on the same URL), byte-exact
// status lines, and raw-body modes — the same reasons the
// Rust/C++ ports drive their own TCP servers. Only the event loop,
// TCP accept and timers come from ReactPHP; parsing, dispatch and
// WS framing are ours (see WsProto.php).
//
// One request per connection (Connection: close always), except an
// upgraded WebSocket which owns its connection until the session
// ends. Request heads are capped at 1 MB, bodies at 128 MB.

use React\EventLoop\Loop;
use React\Promise\PromiseInterface;
use React\Socket\ConnectionInterface;
use React\Socket\SocketServer;

require_once __DIR__ . '/Http.php';
require_once __DIR__ . '/Api.php';
require_once __DIR__ . '/WsProto.php';
require_once __DIR__ . '/SocketStore.php';

const G_HTTP_MAX_HEAD = 1048576;
const G_HTTP_MAX_BODY = 134217728;

class GHttpServer {
    private GApi $api;

    public function __construct(GApi $api) {
        $this->api = $api;
    }

    public function listen(string $host, int $port): SocketServer {
        // ReactPHP only binds literal IPs: resolve "localhost"
        // (config validation already restricted this to loopback).
        $bind = trim($host);
        if (str_starts_with($bind, '[') && str_ends_with($bind, ']') &&
                strlen($bind) > 2) {
            $bind = substr($bind, 1, -1);
        }
        if (strtolower($bind) === 'localhost') {
            $resolved = gethostbyname('localhost');
            $bind = $resolved !== 'localhost' ? $resolved : '127.0.0.1';
        }
        $addr = str_contains($bind, ':') ? "[$bind]:$port" : "$bind:$port";
        $server = new SocketServer($addr, []);
        $server->on('connection', function ($conn) {
            $this->serve($conn);
        });
        return $server;
    }

    private function serve(ConnectionInterface $conn): void {
        $buf = '';
        $done = false;
        $conn->on('data', function ($chunk) use ($conn, &$buf, &$done) {
            if ($done) return;
            $buf .= (string) $chunk;
            $this->onBytes($conn, $buf, $done);
        });
        $conn->on('error', function () use ($conn) {
            try {
                $conn->close();
            } catch (Throwable) {
            }
        });
    }

    private function onBytes(ConnectionInterface $conn, string &$buf,
            bool &$done): void {
        $pos = strpos($buf, "\r\n\r\n");
        if ($pos === false) {
            if (strlen($buf) > G_HTTP_MAX_HEAD) {
                $done = true;
                $conn->end(GResponse::err(400, 'Request head too large.')
                    ->toBytes());
            }
            return;
        }
        $done = true; // one request per connection from here on
        $head = substr($buf, 0, $pos);
        $rest = substr($buf, $pos + 4);
        $buf = '';
        [$req, $err] = $this->parseHead($head);
        if ($err !== null) {
            $conn->end($err->toBytes());
            return;
        }
        $this->readBody($conn, $req, $rest);
    }

    /** @return array{0:GRequest|null,1:GResponse|null} */
    private function parseHead(string $head): array {
        $lines = explode("\r\n", $head);
        $first = array_shift($lines);
        $parts = explode(' ', (string) $first);
        if (count($parts) !== 3) {
            return [null, GResponse::err(400, 'Malformed request line.')];
        }
        [$method, $target, $version] = $parts;
        if ($method === '' || $target === '' ||
                !str_starts_with($version, 'HTTP/')) {
            return [null, GResponse::err(400, 'Malformed request line.')];
        }
        $headers = [];
        foreach ($lines as $ln) {
            if ($ln === '') continue;
            $c = strpos($ln, ':');
            if ($c === false) {
                return [null, GResponse::err(400,
                    'Malformed header line.')];
            }
            $name = strtolower(trim(substr($ln, 0, $c)));
            if ($name === '' || !isset($headers[$name])) {
                if ($name !== '') {
                    $headers[$name] = trim(substr($ln, $c + 1));
                }
            }
        }
        $qpos = strpos($target, '?');
        if ($qpos === false) {
            $path = $target;
            $qs = '';
        } else {
            $path = substr($target, 0, $qpos);
            $qs = substr($target, $qpos + 1);
        }
        if ($path === '') $path = '/';
        $req = new GRequest(strtoupper($method), $path,
            self::parseQuery($qs), $headers, '');
        return [$req, null];
    }

    private static function parseQuery(string $qs): array {
        $out = [];
        if ($qs === '') return $out;
        foreach (explode('&', $qs) as $part) {
            if ($part === '') continue;
            $eq = strpos($part, '=');
            if ($eq === false) {
                $k = urldecode($part);
                $v = '';
            } else {
                $k = urldecode(substr($part, 0, $eq));
                $v = urldecode(substr($part, $eq + 1));
            }
            if (!array_key_exists($k, $out)) $out[$k] = $v;
        }
        return $out;
    }

    private function readBody(ConnectionInterface $conn, GRequest $req,
            string $rest): void {
        $te = $req->header('transfer-encoding');
        $chunks = array_map('trim', explode(',', strtolower($te)));
        if (in_array('chunked', $chunks, true)) {
            $this->readChunked($conn, $req, $rest);
            return;
        }
        $cl = $req->header('content-length');
        $need = 0;
        if ($cl !== '') {
            if (!preg_match('/^\d+$/', trim($cl))) {
                $conn->end(GResponse::err(400,
                    'Invalid Content-Length.')->toBytes());
                return;
            }
            $need = (int) trim($cl);
            if ($need > G_HTTP_MAX_BODY) {
                $conn->end(GResponse::err(413,
                    'Request body too large.')->toBytes());
                return;
            }
        }
        if (strlen($rest) >= $need) {
            $req->body = substr($rest, 0, $need);
            $this->dispatch($conn, $req);
            return;
        }
        $buf = $rest;
        $conn->on('data', function ($chunk) use (
                $conn, $req, $need, &$buf) {
            $buf .= (string) $chunk;
            if (strlen($buf) >= $need) {
                $req->body = substr($buf, 0, $need);
                // Replace ourselves with a no-op so extra bytes
                // (no pipelining supported) are ignored.
                $conn->removeAllListeners('data');
                $this->dispatch($conn, $req);
            } elseif (strlen($buf) > G_HTTP_MAX_BODY) {
                $conn->removeAllListeners('data');
                $conn->end(GResponse::err(413,
                    'Request body too large.')->toBytes());
            }
        });
    }

    private function readChunked(ConnectionInterface $conn, GRequest $req,
            string $rest): void {
        $buf = $rest;
        $body = '';
        $feed = null;
        $feed = function (string $chunk) use ($conn, $req, &$buf,
                &$body, &$feed) {
            $buf .= $chunk;
            while (true) {
                $eol = strpos($buf, "\r\n");
                if ($eol === false) return;
                $size = trim(substr($buf, 0, $eol));
                $semi = strpos($size, ';');
                if ($semi !== false) $size = substr($size, 0, $semi);
                if (!preg_match('/^[0-9a-fA-F]+$/', $size)) {
                    $conn->removeAllListeners('data');
                    $conn->end(GResponse::err(400,
                        'Invalid chunked body.')->toBytes());
                    return;
                }
                $n = hexdec($size);
                if (strlen($buf) < $eol + 2 + $n + 2) return;
                if ($n === 0) {
                    $req->body = $body;
                    $conn->removeAllListeners('data');
                    $this->dispatch($conn, $req);
                    return;
                }
                $body .= substr($buf, $eol + 2, $n);
                if (strlen($body) > G_HTTP_MAX_BODY) {
                    $conn->removeAllListeners('data');
                    $conn->end(GResponse::err(413,
                        'Request body too large.')->toBytes());
                    return;
                }
                $buf = substr($buf, $eol + 2 + $n + 2);
            }
        };
        $conn->on('data', function ($chunk) use ($feed) {
            $feed((string) $chunk);
        });
        // The whole body may already have arrived with the head.
        $feed('');
    }

    private function dispatch(ConnectionInterface $conn,
            GRequest $req): void {
        // WebSocket upgrades bypass normal dispatch (same URL, but
        // the connection is hijacked instead of answered).
        if ($req->path === '/api/socket/ws' &&
                g_ws_has_token($req->header('connection'), 'upgrade') &&
                strtolower($req->header('upgrade')) === 'websocket') {
            $this->serveWs($conn, $req);
            return;
        }
        try {
            $out = $this->api->handle($req);
        } catch (Throwable $e) {
            $out = GResponse::err(500,
                'Internal error: ' . $e->getMessage());
        }
        if ($out instanceof PromiseInterface) {
            $out->then(
                function ($resp) use ($conn) {
                    $this->answer($conn, $resp);
                },
                function ($e) use ($conn) {
                    $msg = $e instanceof Throwable ? $e->getMessage()
                        : (string) $e;
                    $this->answer($conn, GResponse::err(500,
                        'Internal error: ' . $msg));
                });
            return;
        }
        $this->answer($conn, $out);
    }

    private function answer(ConnectionInterface $conn, $resp): void {
        if (!$resp instanceof GResponse) {
            $resp = GResponse::err(500, 'Internal error.');
        }
        try {
            $conn->end($resp->toBytes());
        } catch (Throwable) {
        }
    }

    // -- WebSocket bridge --------------------------------------------

    private function serveWs(ConnectionInterface $conn,
            GRequest $req): void {
        $deny = $this->api->checkAuth($req, '/api/socket/ws');
        if ($deny !== null) {
            try {
                $conn->end($deny->toBytes());
            } catch (Throwable) {
            }
            return;
        }
        if ($req->method !== 'GET') {
            try {
                $conn->end(GResponse::err(405,
                    "Method {$req->method} not supported.")->toBytes());
            } catch (Throwable) {
            }
            return;
        }
        if ($req->header('sec-websocket-version') !== '13') {
            try {
                $conn->end(GResponse::err(400,
                    'Unsupported WebSocket version (need 13).')
                    ->toBytes());
            } catch (Throwable) {
            }
            return;
        }
        $key = trim($req->header('sec-websocket-key'));
        if ($key === '') {
            try {
                $conn->end(GResponse::err(400,
                    'Missing Sec-WebSocket-Key.')->toBytes());
            } catch (Throwable) {
            }
            return;
        }
        [$sid, $sess, $lookupErr] = GSocketStore::lookup($req->query);
        if ($lookupErr !== null) {
            try {
                $conn->end($lookupErr->toBytes());
            } catch (Throwable) {
            }
            return;
        }
        if ($sess['wsAttached']) {
            try {
                $conn->end(GResponse::err(400, 'Socket session is ' .
                    'already attached to a websocket.')->toBytes());
            } catch (Throwable) {
            }
            return;
        }
        if ($sess['recvPending']) {
            try {
                $conn->end(GResponse::err(400, 'Another recv is ' .
                    'already pending on this session.')->toBytes());
            } catch (Throwable) {
            }
            return;
        }
        GSocketStore::setAttached($sid, true, $conn);
        try {
            $conn->write("HTTP/1.1 101 Switching Protocols\r\n" .
                "Upgrade: websocket\r\n" .
                "Connection: Upgrade\r\n" .
                "Sec-WebSocket-Accept: " . g_ws_accept_key($key) .
                "\r\n\r\n");
            // Bytes that arrived between open and upgrade (e.g. an
            // IMAP greeting) are buffered, not lost: flush them now
            // that the handshake is complete.
            GSocketStore::pumpBuffered($sid);
        } catch (Throwable) {
            GSocketStore::setAttached($sid, false);
            try {
                $conn->close();
            } catch (Throwable) {
            }
            return;
        }
        $ended = false;
        $watcher = null;
        $grace = null;
        // End the session BEFORE echoing a clean close, so the echo
        // deterministically implies "gone" (delete-before-echo).
        $finish = function (?int $code = null,
                string $reason = '') use ($sid, $conn, &$ended,
                &$watcher, &$grace) {
            if ($ended) return;
            $ended = true;
            foreach ([$watcher, $grace] as $timer) {
                if ($timer !== null) {
                    try {
                        Loop::cancelTimer($timer);
                    } catch (Throwable) {
                    }
                }
            }
            // The bridge owns the client connection: end the TCP
            // session but keep wsConn (echo goes out below, then
            // end() delivers it before FIN).
            GSocketStore::endSession($sid, false);
            try {
                if ($code !== null) {
                    $conn->write(g_ws_encode_close($code, $reason));
                }
                $conn->end();
            } catch (Throwable) {
            }
        };
        // TCP side ended first: the close frame already went out
        // from the store pump; give the peer a grace period to echo
        // close, then force-detach (mirrors the Go 2 s grace).
        $armGrace = function () use (&$grace, $finish) {
            if ($grace !== null) return;
            $grace = Loop::addTimer(2.0, function () use ($finish) {
                $finish();
            });
        };
        // Detect the store-driven close (peer EOF/error frames are
        // written straight to $conn by the store pump): watch the
        // session liveness and arm the grace timer once EOF lands.
        $watcher = Loop::addPeriodicTimer(0.2,
            function () use ($sid, $armGrace, $finish, &$watcher) {
                $sess = GSocketStore::get($sid);
                if ($sess === null || !$sess['wsAttached']) {
                    $finish();
                    return;
                }
                if ($sess['eof']) $armGrace();
            });
        $decoder = new GWsDecoder(function ($bytes) use ($conn) {
            try {
                $conn->write($bytes);
            } catch (Throwable) {
            }
        });
        $conn->on('data', function ($chunk) use ($sid, $decoder,
                $finish, $conn) {
            try {
                $events = $decoder->feed((string) $chunk);
            } catch (GWsProtoError) {
                // Fail echo already went out inline; now end.
                $finish();
                return;
            } catch (Throwable) {
                $finish();
                return;
            }
            foreach ($events as $ev) {
                if ($ev[0] === 'close') {
                    $finish($ev[1], (string) $ev[2]);
                    return;
                }
                // Transparent client->TCP forwarding (a false
                // return is just backpressure, not failure).
                $sess = GSocketStore::get($sid);
                if ($sess === null) {
                    $finish();
                    return;
                }
                try {
                    if (!$sess['conn']->isWritable()) {
                        $finish();
                        return;
                    }
                    $sess['conn']->write($ev[2]);
                } catch (Throwable) {
                    $finish();
                    return;
                }
                GSocketStore::touch($sid);
            }
        });
        $conn->on('close', function () use ($finish) {
            $finish();
        });
        $conn->on('error', function () use ($finish) {
            $finish();
        });
    }
}
