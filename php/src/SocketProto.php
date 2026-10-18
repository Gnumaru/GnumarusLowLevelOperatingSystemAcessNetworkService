<?php
// Shared socket-proxy protocol: constants and validation.
// Used by src/SocketStore.php (in-memory sessions) and src/Api.php.
//
// Wire rule reminder: HTTP responses carry tagged scalars (i:/b:);
// the daemon IPC payloads below already use those tags so the router
// can forward bodies verbatim.

const G_SOCK_DEFAULT_CONNECT_TIMEOUT = 5.0;
const G_SOCK_MAX_CONNECT_TIMEOUT = 30;
const G_SOCK_DEFAULT_RECV_TIMEOUT = 5.0;
const G_SOCK_MAX_RECV_TIMEOUT = 60;
const G_SOCK_DEFAULT_MAX_BYTES = 65536;
const G_SOCK_MAX_BYTES = 1048576;
const G_SOCK_MAX_SESSIONS = 128;
const G_SOCK_IDLE_SECS = 300;

/** Tagged int port (i:<1-65535>). Returns [int|null, ?error]. */
function g_sock_parse_port($raw): array {
    if ($raw === null) return [null, 'Missing required field: port (i:<1-65535>).'];
    if (!is_string($raw) || !str_starts_with($raw, 'i:')) {
        return [null, "Field 'port' must be an integer 1-65535 (i:<int>)."];
    }
    try {
        $v = GApi::jdec($raw);
    } catch (Exception) {
        return [null, "Field 'port' must be an integer 1-65535 (i:<int>)."];
    }
    if (!is_int($v) || $v < 1 || $v > 65535) {
        return [null, "Field 'port' must be an integer 1-65535 (i:<int>)."];
    }
    return [$v, null];
}

/** Tagged connect timeout in seconds. Returns [float|null, ?error]. */
function g_sock_parse_body_timeout($raw): array {
    if ($raw === null) return [G_SOCK_DEFAULT_CONNECT_TIMEOUT, null];
    try {
        $v = GApi::jdec($raw);
    } catch (Exception) {
        return [null, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."];
    }
    if (!is_int($v) && !is_float($v)) {
        return [null, "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."];
    }
    $t = (float) $v;
    if (!($t > 0 && $t <= G_SOCK_MAX_CONNECT_TIMEOUT)) {
        return [null, 'Field \'timeout\' must be within (0, ' .
            G_SOCK_MAX_CONNECT_TIMEOUT . '].'];
    }
    return [$t, null];
}

/** Plain query timeout in seconds. Returns [float|null, ?error]. */
function g_sock_parse_query_timeout($raw): array {
    if ($raw === null || $raw === '') {
        return [G_SOCK_DEFAULT_RECV_TIMEOUT, null];
    }
    if (!is_numeric($raw)) {
        return [null, 'Invalid timeout param (seconds within (0, ' .
            G_SOCK_MAX_RECV_TIMEOUT . ']).'];
    }
    $t = (float) $raw;
    if (!($t > 0 && $t <= G_SOCK_MAX_RECV_TIMEOUT)) {
        return [null, 'Invalid timeout param (seconds within (0, ' .
            G_SOCK_MAX_RECV_TIMEOUT . ']).'];
    }
    return [$t, null];
}

/** Plain max_bytes. Returns [int|null, ?error]. */
function g_sock_parse_max_bytes($raw): array {
    if ($raw === null || $raw === '') {
        return [G_SOCK_DEFAULT_MAX_BYTES, null];
    }
    $s = trim((string) $raw);
    if (!preg_match('/^[+-]?\d+$/', $s)) {
        return [null, 'Invalid max_bytes param (1-' . G_SOCK_MAX_BYTES . ').'];
    }
    $n = (int) $s;
    if ($n < 1 || $n > G_SOCK_MAX_BYTES) {
        return [null, 'Invalid max_bytes param (1-' . G_SOCK_MAX_BYTES . ').'];
    }
    return [$n, null];
}

/** Tagged TLS flag (b:true/b:false, absent/null means false).
Returns [bool|null, ?error]. */
function g_sock_parse_tls($raw): array {
    if ($raw === null) return [false, null];
    if (!is_string($raw)) {
        return [null, "Field 'tls' must be b:true or b:false."];
    }
    try {
        $v = GApi::jdec($raw);
    } catch (Exception) {
        return [null, "Field 'tls' must be b:true or b:false."];
    }
    if (!is_bool($v)) {
        return [null, "Field 'tls' must be b:true or b:false."];
    }
    return [$v, null];
}

/** Optional sni/ca_pem fields shared by open and starttls.
Returns [sni, caPem, ?error]. */
function g_sock_parse_tls_identity($obj): array {
    $sni = '';
    if (array_key_exists('sni', $obj) && $obj['sni'] !== null) {
        if (!is_string($obj['sni']) || trim($obj['sni']) === '') {
            return ['', '', "Field 'sni' must be a non-empty string."];
        }
        $sni = trim($obj['sni']);
    }
    $caPem = '';
    if (array_key_exists('ca_pem', $obj) && $obj['ca_pem'] !== null) {
        if (!is_string($obj['ca_pem']) || trim($obj['ca_pem']) === '') {
            return ['', '',
                "Field 'ca_pem' must be a PEM CA bundle string."];
        }
        $caPem = $obj['ca_pem'];
    }
    return [$sni, $caPem, null];
}

/** Validate a PEM CA bundle string (every CERTIFICATE block must
parse). Returns ?error. */
function g_sock_validate_ca_pem(string $caPem): ?string {
    if (!preg_match_all(
            '/-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----/s',
            $caPem, $m) || count($m[0]) === 0) {
        return "Field 'ca_pem' must be a valid PEM CA bundle.";
    }
    foreach ($m[0] as $block) {
        $cert = @openssl_x509_read($block);
        if ($cert === false) {
            return "Field 'ca_pem' must be a valid PEM CA bundle.";
        }
    }
    return null;
}

/** Stage a PEM bundle as a temp cafile. Returns [path|null, ?error];
caller unlinks the path. */
function g_sock_write_cafile(string $caPem): array {
    $path = tempnam(sys_get_temp_dir(), 'gnumarus-ca-');
    if ($path === false || file_put_contents($path, $caPem) === false) {
        if (is_string($path)) @unlink($path);
        return [null, "TLS setup failed: cannot stage CA bundle."];
    }
    return [$path, null];
}

/** Strict base64 (like Python validate=True for the tested shapes). */
function g_sock_b64decode(string $s) {
    if (strlen($s) % 4 !== 0) return false;
    if (!preg_match('/^[A-Za-z0-9+\/]*={0,2}$/', $s)) return false;
    $v = base64_decode($s, true);
    return $v === false ? false : $v;
}

/** Read one line (up to $cap bytes) with an absolute deadline. */
function g_sock_read_line($sock, float $deadline) {
    $buf = '';
    while (true) {
        $left = $deadline - microtime(true);
        if ($left <= 0) return null;
        $r = [$sock];
        $w = $e = null;
        $sec = (int) $left;
        $usec = (int) (($left - $sec) * 1000000);
        if (@stream_select($r, $w, $e, $sec, $usec) !== 1) return null;
        $chunk = @fgets($sock, 8 * 1024 * 1024);
        if (!is_string($chunk) || $chunk === '') return null;
        $buf .= $chunk;
        if (str_ends_with($buf, "\n")) return $buf;
        if (strlen($buf) > 8 * 1024 * 1024) return null;
    }
}
