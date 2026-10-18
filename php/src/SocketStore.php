<?php
// In-memory TCP socket sessions for the ReactPHP port.
//
// The service holds one real TCP client connection per session and
// exposes a byte-transparent tunnel over HTTP (see Go socket.go for
// the reference semantics): opaque ids, 128-session cap, 300 s idle
// expiry (attached sessions skip expiry), sticky EOF, one pending
// recv per session, and HTTP send/recv refused while a WebSocket is
// attached. No daemon IPC: this process owns the connections.

use React\EventLoop\Loop;
use React\Promise\Deferred;
use React\Promise\PromiseInterface;
use React\Socket\ConnectionInterface;

require_once __DIR__ . '/SocketProto.php';

class GSocketStore {
    /** @var array<string, array> sid => session record */
    private static array $sessions = [];
    private static bool $sweeper = false;

    public static function resetForTests(): void {
        foreach (self::$sessions as $sess) {
            try {
                $sess['conn']->close();
            } catch (Throwable) {
            }
        }
        self::$sessions = [];
    }

    // -- lookup ------------------------------------------------------

    /** @return array{0:string|null,1:array|null,2:GResponse|null} */
    public static function lookup(array $query): array {
        $sid = $query['id'] ?? '';
        if ($sid === '') {
            return [null, null,
                GResponse::err(400, 'Missing required query parameter: id')];
        }
        if (!isset(self::$sessions[$sid])) {
            return [null, null,
                GResponse::err(404, 'Unknown socket session: ' . $sid)];
        }
        self::$sessions[$sid]['last'] = microtime(true);
        return [$sid, self::$sessions[$sid], null];
    }

    public static function get(string $sid): ?array {
        return self::$sessions[$sid] ?? null;
    }

    public static function touch(string $sid): void {
        if (isset(self::$sessions[$sid])) {
            self::$sessions[$sid]['last'] = microtime(true);
        }
    }

    public static function markEof(string $sid): void {
        if (isset(self::$sessions[$sid])) {
            self::$sessions[$sid]['eof'] = true;
            self::$sessions[$sid]['last'] = microtime(true);
        }
    }

    /** End a session: unmark, delete, close the TCP connection.
     *
     * $closeWs controls the attached WS client connection (owned by
     * the bridge, not the store): the HTTP-close path kills it (with
     * a close frame, like the Go pump's 1011 on a dead TCP side),
     * while the WS finish path keeps it (the bridge echoes first,
     * then ends it itself: delete-before-echo).
     */
    public static function endSession(string $sid,
            bool $closeWs = true): void {
        if (!isset(self::$sessions[$sid])) return;
        $sess = self::$sessions[$sid];
        unset(self::$sessions[$sid]);
        // Fail any pending recv waiter as unknown (it will 404 on
        // re-lookup; here just resolve it as closed).
        if ($sess['waiter'] !== null) {
            $waiter = $sess['waiter'];
            try {
                $waiter['timer']->cancel();
            } catch (Throwable) {
            }
            $waiter['deferred']->resolve(GResponse::err(404,
                'Unknown socket session: ' . $sid));
        }
        try {
            $sess['conn']->close();
        } catch (Throwable) {
        }
        if ($closeWs && $sess['wsConn'] !== null) {
            try {
                // end() flushes the close frame before FIN (a bare
                // close() would drop it from the write buffer).
                $sess['wsConn']->end(g_ws_encode_close(1011));
            } catch (Throwable) {
            }
        }
    }

    // -- sweeper -----------------------------------------------------

    public static function sweepExpired(): void {
        $now = microtime(true);
        foreach (self::$sessions as $sid => $sess) {
            if ($sess['wsAttached']) continue;
            if ($now - $sess['last'] > G_SOCK_IDLE_SECS) {
                self::endSession($sid);
            }
        }
    }

    private static function ensureSweeper(): void {
        if (self::$sweeper) return;
        self::$sweeper = true;
        Loop::addPeriodicTimer(60.0, function () {
            self::sweepExpired();
        });
    }

    // -- open --------------------------------------------------------

    /** @return PromiseInterface<GResponse> */
    public static function open(string $contentType, string $raw): PromiseInterface {
        self::ensureSweeper();
        if (stripos($contentType, 'application/json') === false) {
            return \React\Promise\resolve(GResponse::err(400,
                'Content-Type must be application/json with body ' .
                '{ host, port, timeout? }.'));
        }
        if (trim($raw) === '') {
            return \React\Promise\resolve(GResponse::err(400,
                'Missing or empty required field: host.'));
        }
        $obj = json_decode($raw, true);
        if (json_last_error() !== JSON_ERROR_NONE) {
            return \React\Promise\resolve(GResponse::err(400,
                'Invalid JSON body.'));
        }
        if (!is_array($obj) || (array_is_list($obj) && count($obj) > 0)) {
            return \React\Promise\resolve(GResponse::err(400,
                'JSON body must be an object.'));
        }
        if (!isset($obj['host']) || !is_string($obj['host']) ||
                trim($obj['host']) === '') {
            return \React\Promise\resolve(GResponse::err(400,
                'Missing or empty required field: host.'));
        }
        [$port, $portErr] = g_sock_parse_port($obj['port'] ?? null);
        if ($portErr !== null) {
            return \React\Promise\resolve(
                GResponse::err(400, $portErr));
        }
        [$timeout, $timeoutErr] =
            g_sock_parse_body_timeout($obj['timeout'] ?? null);
        if ($timeoutErr !== null) {
            return \React\Promise\resolve(
                GResponse::err(400, $timeoutErr));
        }
        [$tlsMode, $tlsErr] = g_sock_parse_tls($obj['tls'] ?? null);
        if ($tlsErr !== null) {
            return \React\Promise\resolve(
                GResponse::err(400, $tlsErr));
        }
        [$sni, $caPem, $identErr] =
            g_sock_parse_tls_identity($obj);
        if ($identErr !== null) {
            return \React\Promise\resolve(
                GResponse::err(400, $identErr));
        }
        $host = $obj['host'];
        if ($sni === '') $sni = $host;
        $caFile = null;
        if ($tlsMode && $caPem !== '') {
            $caErr = g_sock_validate_ca_pem($caPem);
            if ($caErr !== null) {
                return \React\Promise\resolve(
                    GResponse::err(400, $caErr));
            }
            [$caFile, $caFileErr] = g_sock_write_cafile($caPem);
            if ($caFileErr !== null) {
                return \React\Promise\resolve(
                    GResponse::err(400, $caFileErr));
            }
        }
        self::sweepExpired();
        if (count(self::$sessions) >= G_SOCK_MAX_SESSIONS) {
            return \React\Promise\resolve(GResponse::err(400,
                'Too many open socket sessions (' .
                G_SOCK_MAX_SESSIONS . ').'));
        }
        $host = $obj['host'];
        $connector = $tlsMode
            ? new React\Socket\Connector(array_merge(
                ['timeout' => $timeout],
                ['tls' => array_merge(
                    ['verify_peer' => true,
                     'verify_peer_name' => true,
                     'SNI_enabled' => true,
                     'peer_name' => $sni,
                     'crypto_method' =>
                         STREAM_CRYPTO_METHOD_TLSv1_2_CLIENT |
                         STREAM_CRYPTO_METHOD_TLSv1_3_CLIENT],
                    $caFile !== null ? ['cafile' => $caFile] : [])]))
            : new React\Socket\Connector(['timeout' => $timeout]);
        $scheme = $tlsMode ? 'tls' : 'tcp';
        $isTls = $tlsMode;
        return $connector->connect("$scheme://$host:$port")->then(
            function (ConnectionInterface $conn) use (
                    $host, $port, $caFile, $isTls) {
                if ($caFile !== null) @unlink($caFile);
                $sid = rtrim(strtr(base64_encode(random_bytes(16)),
                    '+/', '-_'), '=');
                self::$sessions[$sid] = [
                    'conn' => $conn,
                    'host' => $host,
                    'port' => $port,
                    'kind' => $isTls ? 'tls' : 'tcp',
                    'last' => microtime(true),
                    'eof' => false,
                    'buf' => '',
                    'peerClosed' => false,
                    'connError' => '',
                    'waiter' => null,
                    'recvPending' => false,
                    'wsAttached' => false,
                    'wsConn' => null,
                ];
                $conn->on('data', function ($chunk) use ($sid) {
                    self::onTcpData($sid, (string) $chunk);
                });
                $conn->on('close', function () use ($sid) {
                    self::onTcpClose($sid);
                });
                $conn->on('error', function ($e) use ($sid) {
                    if (isset(self::$sessions[$sid]) &&
                        self::$sessions[$sid]['connError'] === '') {
                        self::$sessions[$sid]['connError'] =
                            $e instanceof Throwable ? $e->getMessage()
                                : (string) $e;
                    }
                });
                return GResponse::json(201, ['id' => $sid,
                    'kind' => $isTls ? 'tls' : 'tcp', 'host' => $host,
                    'port' => GApi::jenc($port)]);
            },
            function ($e) use ($host, $port, $caFile, $isTls) {
                if ($caFile !== null) @unlink($caFile);
                $msg = $e instanceof Throwable ? $e->getMessage()
                    : (string) $e;
                if ($isTls) {
                    return GResponse::err(400,
                        "TLS handshake with $host:$port failed: $msg");
                }
                // Match the reference shape: "Cannot connect to
                // h:p: <os error>".
                return GResponse::err(400,
                    "Cannot connect to $host:$port: $msg");
            });
    }

    private static function onTcpData(string $sid, string $chunk): void {
        if (!isset(self::$sessions[$sid])) return;
        $sess = &self::$sessions[$sid];
        $sess['buf'] .= $chunk;
        $sess['last'] = microtime(true);
        if ($sess['wsAttached']) {
            self::pumpToWs($sid);
            return;
        }
        if ($sess['waiter'] !== null) self::fulfillWaiter($sid);
    }

    private static function onTcpClose(string $sid): void {
        if (!isset(self::$sessions[$sid])) return;
        $sess = &self::$sessions[$sid];
        $sess['peerClosed'] = true;
        if ($sess['wsAttached']) {
            // Peer EOF over an attached WS: sticky EOF + server
            // close frame (the bridge finishes from here).
            $sess['eof'] = true;
            self::wsPeerEnded($sid, false);
            return;
        }
        if ($sess['waiter'] !== null) self::fulfillWaiter($sid);
    }

    // -- send --------------------------------------------------------

    public static function send(string $sid, string $contentType,
            string $raw): GResponse {
        [$data, $err] = self::sendBytes($raw, $contentType);
        if ($err !== null) return $err;
        if (!isset(self::$sessions[$sid])) {
            return GResponse::err(404, 'Unknown socket session: ' . $sid);
        }
        $sess = &self::$sessions[$sid];
        // Note: write() returning false only signals backpressure
        // (buffer over the soft limit), not failure — the bytes are
        // still accepted. Only a dead stream (or a throw) is an
        // error here.
        try {
            if (!$sess['conn']->isWritable()) {
                return GResponse::err(400,
                    'Socket error: connection lost');
            }
            $sess['conn']->write($data);
        } catch (Throwable) {
            return GResponse::err(400,
                'Socket error: connection lost');
        }
        $sess['last'] = microtime(true);
        return GResponse::json(200,
            ['id' => $sid, 'bytes' => GApi::jenc(strlen($data))]);
    }

    /** @return array{0:string|null,1:GResponse|null} */
    private static function sendBytes(string $raw,
            string $ct): array {
        if (stripos($ct, 'application/json') !== false) {
            if (trim($raw) === '') $obj = [];
            else {
                $obj = json_decode($raw, true);
                if (json_last_error() !== JSON_ERROR_NONE) {
                    return [null, GResponse::err(400,
                        'Invalid JSON body.')];
                }
            }
            if (!is_array($obj) ||
                    ($obj !== [] && array_is_list($obj))) {
                return [null, GResponse::err(400,
                    'JSON body must be an object.')];
            }
            $content = '';
            if (array_key_exists('content', $obj) &&
                    $obj['content'] !== null) {
                if (!is_string($obj['content'])) {
                    return [null, GResponse::err(400,
                        "'content' must be a string.")];
                }
                $content = $obj['content'];
            }
            $encoding = 'text';
            if (array_key_exists('encoding', $obj) &&
                    $obj['encoding'] !== null) {
                if (!is_string($obj['encoding'])) {
                    return [null, GResponse::err(400,
                        'Invalid encoding: (use text|base64).')];
                }
                $encoding = strtolower($obj['encoding']);
            }
            if ($encoding === 'text') return [$content, null];
            if ($encoding === 'base64') {
                $data = g_sock_b64decode($content);
                if ($data === false) {
                    return [null, GResponse::err(400,
                        "Invalid base64 'content'.")];
                }
                return [$data, null];
            }
            return [null, GResponse::err(400,
                "Invalid encoding: '$encoding' (use text|base64).")];
        }
        return [$raw, null];
    }

    // -- recv (async long-poll) --------------------------------------

    /** @return PromiseInterface<GResponse>|GResponse */
    public static function recv(array $query) {
        // Param validation first (Go order): bad timeout/max_bytes
        // is 400 even for an unknown id.
        [$timeout, $timeoutErr] =
            g_sock_parse_query_timeout($query['timeout'] ?? null);
        if ($timeoutErr !== null) {
            return GResponse::err(400, $timeoutErr);
        }
        [$max, $maxErr] = g_sock_parse_max_bytes(
            $query['max_bytes'] ?? null);
        if ($maxErr !== null) {
            return GResponse::err(400, $maxErr);
        }
        [$sid, $sess, $lookupErr] = self::lookup($query);
        if ($lookupErr !== null) return $lookupErr;
        if ($sess['wsAttached']) {
            return GResponse::err(400, 'Socket session is attached ' .
                'to a websocket (HTTP recv refused while attached).');
        }
        $sid = (string) $sid;
        $sess = &self::$sessions[$sid];
        if ($sess['eof']) {
            return GResponse::json(200, ['id' => $sid,
                'encoding' => 'base64', 'content' => '',
                'bytes' => GApi::jenc(0), 'eof' => GApi::jenc(true),
                'timed_out' => GApi::jenc(false)]);
        }
        if ($sess['recvPending']) {
            return GResponse::err(400,
                'Another recv is already pending on this session.');
        }
        if ($sess['buf'] !== '') {
            $chunk = substr($sess['buf'], 0, $max);
            $sess['buf'] = substr($sess['buf'], strlen($chunk));
            $sess['last'] = microtime(true);
            return GResponse::json(200, ['id' => $sid,
                'encoding' => 'base64',
                'content' => base64_encode($chunk),
                'bytes' => GApi::jenc(strlen($chunk)),
                'eof' => GApi::jenc(false),
                'timed_out' => GApi::jenc(false)]);
        }
        if ($sess['peerClosed']) {
            $sess['eof'] = true;
            return GResponse::json(200, ['id' => $sid,
                'encoding' => 'base64', 'content' => '',
                'bytes' => GApi::jenc(0), 'eof' => GApi::jenc(true),
                'timed_out' => GApi::jenc(false)]);
        }
        // Long-poll: wait for data, peer close, or the timeout.
        $deferred = new Deferred();
        $sess['recvPending'] = true;
        $timer = Loop::addTimer($timeout, function () use ($sid, $max) {
            if (!isset(self::$sessions[$sid])) return;
            $sess = &self::$sessions[$sid];
            if ($sess['waiter'] === null) return;
            $waiter = $sess['waiter'];
            $sess['waiter'] = null;
            $sess['recvPending'] = false;
            // Data may have arrived before the timer fired.
            if ($sess['buf'] !== '') {
                $chunk = substr($sess['buf'], 0, $max);
                $sess['buf'] = substr($sess['buf'], strlen($chunk));
                $sess['last'] = microtime(true);
                $waiter['deferred']->resolve(GResponse::json(200, [
                    'id' => $sid, 'encoding' => 'base64',
                    'content' => base64_encode($chunk),
                    'bytes' => GApi::jenc(strlen($chunk)),
                    'eof' => GApi::jenc(false),
                    'timed_out' => GApi::jenc(false)]));
                return;
            }
            if ($sess['peerClosed']) {
                $sess['eof'] = true;
                $waiter['deferred']->resolve(GResponse::json(200, [
                    'id' => $sid, 'encoding' => 'base64',
                    'content' => '', 'bytes' => GApi::jenc(0),
                    'eof' => GApi::jenc(true),
                    'timed_out' => GApi::jenc(false)]));
                return;
            }
            $sess['last'] = microtime(true);
            $waiter['deferred']->resolve(GResponse::json(200, [
                'id' => $sid, 'encoding' => 'base64',
                'content' => '', 'bytes' => GApi::jenc(0),
                'eof' => GApi::jenc(false),
                'timed_out' => GApi::jenc(true)]));
        });
        $sess['waiter'] = [
            'deferred' => $deferred, 'timer' => $timer, 'max' => $max,
        ];
        return $deferred->promise();
    }

    private static function fulfillWaiter(string $sid): void {
        $sess = &self::$sessions[$sid];
        $waiter = $sess['waiter'];
        if ($waiter === null) return;
        $sess['waiter'] = null;
        $sess['recvPending'] = false;
        try {
            $waiter['timer']->cancel();
        } catch (Throwable) {
        }
        $max = $waiter['max'];
        if ($sess['buf'] !== '') {
            $chunk = substr($sess['buf'], 0, $max);
            $sess['buf'] = substr($sess['buf'], strlen($chunk));
            $sess['last'] = microtime(true);
            $waiter['deferred']->resolve(GResponse::json(200, [
                'id' => $sid, 'encoding' => 'base64',
                'content' => base64_encode($chunk),
                'bytes' => GApi::jenc(strlen($chunk)),
                'eof' => GApi::jenc(false),
                'timed_out' => GApi::jenc(false)]));
            return;
        }
        if ($sess['peerClosed']) {
            $sess['eof'] = true;
            $waiter['deferred']->resolve(GResponse::json(200, [
                'id' => $sid, 'encoding' => 'base64',
                'content' => '', 'bytes' => GApi::jenc(0),
                'eof' => GApi::jenc(true),
                'timed_out' => GApi::jenc(false)]));
            return;
        }
        // Spurious wakeup (should not happen): resolve as timeout.
        $waiter['deferred']->resolve(GResponse::json(200, [
            'id' => $sid, 'encoding' => 'base64',
            'content' => '', 'bytes' => GApi::jenc(0),
            'eof' => GApi::jenc(false),
            'timed_out' => GApi::jenc(true)]));
    }

    // -- close -------------------------------------------------------

    public static function close(string $sid): GResponse {
        if (!isset(self::$sessions[$sid])) {
            return GResponse::err(404, 'Unknown socket session: ' . $sid);
        }
        self::endSession($sid);
        return GResponse::json(200,
            ['id' => $sid, 'closed' => GApi::jenc(true)]);
    }

    // -- STARTTLS ------------------------------------------------------
    // Upgrade a plaintext session to TLS in place. The handshake is a
    // blocking stream_socket_enable_crypto call (like the blocking
    // sqlite queries, short by design); the loop simply stalls for
    // its duration. A failed handshake leaves the plaintext session
    // untouched.

    public static function starttls(string $sid, string $contentType,
            string $raw): GResponse {
        if (!isset(self::$sessions[$sid])) {
            return GResponse::err(404, 'Unknown socket session: ' . $sid);
        }
        $sess = &self::$sessions[$sid];
        $sess['last'] = microtime(true);
        if ($sess['wsAttached']) {
            return GResponse::err(400, 'Socket session is attached ' .
                'to a websocket (STARTTLS refused while attached).');
        }
        if (($sess['kind'] ?? 'tcp') === 'tls') {
            return GResponse::err(400,
                'Socket session is already TLS.');
        }
        if ($sess['recvPending']) {
            return GResponse::err(400,
                'Another recv is already pending on this session.');
        }
        $obj = [];
        if (trim($raw) !== '') {
            if (stripos($contentType, 'application/json') === false) {
                return GResponse::err(400,
                    'Content-Type must be application/json with body ' .
                    '{ sni?, ca_pem? }.');
            }
            $obj = json_decode($raw, true);
            if (json_last_error() !== JSON_ERROR_NONE) {
                return GResponse::err(400, 'Invalid JSON body.');
            }
            if (!is_array($obj) ||
                (array_is_list($obj) && count($obj) > 0)) {
                return GResponse::err(400, 'JSON body must be an object.');
            }
        }
        [$sni, $caPem, $identErr] = g_sock_parse_tls_identity($obj);
        if ($identErr !== null) {
            return GResponse::err(400, $identErr);
        }
        if ($sni === '') $sni = $sess['host'];
        $port = $sess['port'];
        $host = $sess['host'];
        $caFile = null;
        if ($caPem !== '') {
            $caErr = g_sock_validate_ca_pem($caPem);
            if ($caErr !== null) {
                return GResponse::err(400, $caErr);
            }
            [$caFile, $caFileErr] = g_sock_write_cafile($caPem);
            if ($caFileErr !== null) {
                return GResponse::err(400, $caFileErr);
            }
        }
        $conn = $sess['conn'];
        if (!($conn instanceof React\Socket\Connection) ||
            !is_resource($conn->stream)) {
            if ($caFile !== null) @unlink($caFile);
            return GResponse::err(400,
                "TLS handshake with $host:$port failed: " .
                'session stream does not support STARTTLS.');
        }
        $stream = $conn->stream;
        $opts = ['verify_peer' => true,
                 'verify_peer_name' => true,
                 'SNI_enabled' => true,
                 'peer_name' => $sni,
                 'crypto_method' =>
                     STREAM_CRYPTO_METHOD_TLSv1_2_CLIENT |
                     STREAM_CRYPTO_METHOD_TLSv1_3_CLIENT];
        if ($caFile !== null) $opts['cafile'] = $caFile;
        stream_context_set_option($stream, ['ssl' => $opts]);
        stream_set_blocking($stream, true);
        stream_set_timeout($stream, (int) G_SOCK_MAX_CONNECT_TIMEOUT);
        $ok = @stream_socket_enable_crypto($stream, true,
            STREAM_CRYPTO_METHOD_TLSv1_2_CLIENT |
            STREAM_CRYPTO_METHOD_TLSv1_3_CLIENT);
        $cryptoErr = $ok === true ? '' : (error_get_last()['message'] ?? '');
        stream_set_blocking($stream, false);
        if ($caFile !== null) @unlink($caFile);
        if ($ok !== true) {
            return GResponse::err(400,
                "TLS handshake with $host:$port failed: $cryptoErr");
        }
        $sess['kind'] = 'tls';
        $sess['last'] = microtime(true);
        return GResponse::json(200,
            ['id' => $sid, 'kind' => 'tls']);
    }

    // -- WebSocket attach --------------------------------------------

    public static function setAttached(string $sid, bool $attached,
            $wsConn = null): void {
        if (!isset(self::$sessions[$sid])) return;
        self::$sessions[$sid]['wsAttached'] = $attached;
        self::$sessions[$sid]['wsConn'] = $attached ? $wsConn : null;
        self::$sessions[$sid]['last'] = microtime(true);
    }

    public static function isAttached(string $sid): bool {
        return isset(self::$sessions[$sid]) &&
            self::$sessions[$sid]['wsAttached'];
    }

    /** Forward buffered + future TCP bytes to the WS client. */
    private static function pumpToWs(string $sid): void {
        if (!isset(self::$sessions[$sid])) return;
        $sess = &self::$sessions[$sid];
        if ($sess['buf'] === '' || $sess['wsConn'] === null) return;
        $chunk = $sess['buf'];
        $sess['buf'] = '';
        try {
            $sess['wsConn']->write(g_ws_encode_frame(G_WS_BIN, $chunk));
        } catch (Throwable) {
        }
    }

    /** Drain bytes buffered before the WS attach (e.g. an IMAP
    greeting that arrived between open and upgrade). Must run only
    after the 101 handshake went out: frames before 101 violate
    RFC 6455 and strict clients abort. */
    public static function pumpBuffered(string $sid): void {
        self::pumpToWs($sid);
    }

    /** TCP side ended while attached: sticky EOF + server close. */
    private static function wsPeerEnded(string $sid, bool $error): void {
        if (!isset(self::$sessions[$sid])) return;
        $sess = self::$sessions[$sid];
        if ($sess['wsConn'] === null) return;
        // Flush anything still buffered before the close frame.
        self::pumpToWs($sid);
        try {
            $sess['wsConn']->write(g_ws_encode_close(
                $error ? 1011 : 1000));
        } catch (Throwable) {
        }
    }

    public static function wsTcpError(string $sid): void {
        self::wsPeerEnded($sid, true);
    }
}
