<?php
// Multi-source configuration: CLI flag > env var > stdin > config.cfg > default.

const G_CONFIG_KEYS = ['host', 'port', 'token', 'no_auth'];
const G_CONFIG_FILE_NAME = 'config.cfg';
const G_CONFIG_MAX_BYTES = 1048576;
const G_CONFIG_ENV_PREFIX = 'GNUMARUS_';

class GConfigError extends Exception {}

function g_parse_kv(string $data): array {
    if (strpos($data, "\x00") !== false) return [];
    if (!mb_check_encoding($data, 'UTF-8')) return [];
    $cfg = [];
    foreach (preg_split("/\r\n|\n|\r/", $data) as $rawLine) {
        $line = trim($rawLine);
        if ($line === '' || $line[0] === '#' || $line[0] === ';') continue;
        if ($line[0] === '[' && substr($line, -1) === ']') continue;
        $eq = strpos($line, '=');
        if ($eq === false) continue;
        $key = strtolower(trim(substr($line, 0, $eq)));
        $value = trim(substr($line, $eq + 1));
        $len = strlen($value);
        if ($len >= 2 && (($value[0] === "'" && $value[$len - 1] === "'") ||
                ($value[0] === '"' && $value[$len - 1] === '"'))) {
            $value = substr($value, 1, $len - 2);
        }
        if (in_array($key, G_CONFIG_KEYS, true)) $cfg[$key] = $value;
    }
    return $cfg;
}

function g_load_file_config(?string $dir = null): array {
    if ($dir === null) $dir = dirname(__DIR__); // php/ (next to server.php)
    $path = rtrim($dir, '/\\') . DIRECTORY_SEPARATOR . G_CONFIG_FILE_NAME;
    if (!is_file($path)) return [];
    $fh = @fopen($path, 'rb');
    if ($fh === false) return [];
    $data = stream_get_contents($fh, G_CONFIG_MAX_BYTES);
    fclose($fh);
    if ($data === false || $data === '') return [];
    return g_parse_kv($data);
}

function g_load_stdin_config(): array {
    // Never block: read only when input is already available (TTY with
    // nothing typed and empty-but-open pipes report not-readable -> skip).
    $stdin = fopen('php://stdin', 'rb');
    $read = [$stdin];
    $w = $e = null;
    $ready = @stream_select($read, $w, $e, 0);
    if ($ready !== 1) {
        fclose($stdin);
        return [];
    }
    $data = stream_get_contents($stdin, G_CONFIG_MAX_BYTES);
    fclose($stdin);
    if ($data === false || $data === '') return [];
    return g_parse_kv($data);
}

const G_HOST_DENIED_MSG =
    'Host not allowed (use 127.0.0.1, localhost or ::1).';

function g_is_loopback_ip(string $name): bool {
    if ($name === '::1') return true;
    $parts = explode('.', $name);
    if (count($parts) !== 4) return false;
    foreach ($parts as $p) {
        if ($p === '' || (strlen($p) > 1 && $p[0] === '0')) return false;
        if (!preg_match('/^[0-9]+$/', $p)) return false;
        if ((int) $p > 255) return false;
    }
    return $parts[0] === '127';
}

function g_is_loopback_name(string $name): bool {
    if ($name === '') return false;
    if (strtolower($name) === 'localhost') return true;
    return g_is_loopback_ip($name);
}

/** Whether an HTTP Host header targets this service. */
function g_host_allowed($header_value): bool {
    $h = trim((string) ($header_value ?? ''));
    if ($h === '') return false;
    if ($h[0] === '[') {
        $end = strpos($h, ']');
        if ($end === false) return false;
        $rest = substr($h, $end + 1);
        if ($rest !== '') {
            if ($rest[0] !== ':' || !preg_match('/^[0-9]+$/', substr($rest, 1))) {
                return false;
            }
        }
        $name = substr($h, 1, $end - 1);
    } else {
        if (substr_count($h, ':') > 1) return false;
        $i = strrpos($h, ':');
        if ($i !== false) {
            if (!preg_match('/^[0-9]+$/', substr($h, $i + 1))) {
                return false;
            }
            $name = substr($h, 0, $i);
        } else {
            $name = $h;
        }
    }
    return g_is_loopback_name($name);
}

/** Whether an address may be bound: loopback only. */
function g_bind_allowed(string $host): bool {
    $h = trim($host);
    if (strlen($h) > 2 && $h[0] === '[' && $h[-1] === ']') {
        $h = substr($h, 1, -1);
    }
    return g_is_loopback_name($h);
}

function g_parse_bool($value, string $source): bool {
    if (is_bool($value)) return $value;
    $t = strtolower(trim((string) $value));
    if (in_array($t, ['1', 'true', 'yes', 'on'], true)) return true;
    if (in_array($t, ['0', 'false', 'no', 'off'], true)) return false;
    throw new GConfigError("invalid boolean for no_auth from $source: '$value' (use true/false)");
}

function g_parse_port($value, string $source): int {
    if (is_bool($value)) {
        throw new GConfigError("invalid port from $source: '$value' (1-65535)");
    }
    if (is_int($value)) {
        $port = $value;
    } elseif (is_float($value) && $value == (int) $value) {
        $port = (int) $value;
    } elseif (is_string($value) && preg_match('/^[+-]?\d+$/', trim($value))) {
        $port = (int) trim($value);
    } else {
        throw new GConfigError("invalid port from $source: '$value' (1-65535)");
    }
    if ($port < 1 || $port > 65535) {
        throw new GConfigError("port from $source out of range: $port (1-65535)");
    }
    return $port;
}

function g_resolve_config(array $cli, array $stdinCfg, array $fileCfg): array {
    $envCfg = [];
    foreach (G_CONFIG_KEYS as $key) {
        $v = getenv(G_CONFIG_ENV_PREFIX . strtoupper($key));
        if ($v !== false && $v !== '') $envCfg[$key] = $v;
    }
    $layers = ['cli' => $cli, 'env' => $envCfg, 'stdin' => $stdinCfg, 'file' => $fileCfg];
    $raw = [];
    $sources = [];
    foreach (G_CONFIG_KEYS as $key) {
        $sources[$key] = 'default';
        foreach ($layers as $source => $layer) {
            if (array_key_exists($key, $layer) && $layer[$key] !== null) {
                $raw[$key] = $layer[$key];
                $sources[$key] = $source;
                break;
            }
        }
    }
    $host = isset($raw['host']) ? trim((string) $raw['host']) : '127.0.0.1';
    if ($host === '') {
        throw new GConfigError("host from {$sources['host']} must not be empty");
    }
    if (!g_bind_allowed($host)) {
        throw new GConfigError("host from {$sources['host']} must be a loopback address (127.0.0.1, ::1 or localhost), got '$host'");
    }
    $port = isset($raw['port']) ? g_parse_port($raw['port'], $sources['port']) : 1423;
    $noAuth = isset($raw['no_auth']) ? g_parse_bool($raw['no_auth'], $sources['no_auth']) : false;
    $token = null;
    if ($noAuth) {
        $sources['token'] = 'disabled';
    } elseif (array_key_exists('token', $raw)) {
        $token = (string) $raw['token'];
        if ($token === '') {
            throw new GConfigError("token from {$sources['token']} must not be empty");
        }
    } else {
        $token = rtrim(strtr(base64_encode(random_bytes(32)), '+/', '-_'), '=');
        $sources['token'] = 'generated';
    }
    return ['host' => $host, 'port' => $port, 'token' => $token,
        'no_auth' => $noAuth, 'sources' => $sources];
}
