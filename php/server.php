#!/usr/bin/env php
<?php
// Gnumarus native-operations HTTP service (PHP port on ReactPHP).
//
// Usage:
//     php php/server.php [--host H] [--port P] [--token TEXT] [--no-auth | --auth]
//
// Settings resolve as: CLI flag > GNUMARUS_* env var > stdin key=value
// lines > php/config.cfg > default. This entry point resolves the
// config, prints it, then serves directly on a ReactPHP event loop
// (manual HTTP/1.1 server in src/HttpServer.php): a single process
// owns every connection, so socket sessions live in memory and
// WebSocket upgrades hijack the raw connection on the same URL.

$autoload = __DIR__ . '/vendor/autoload.php';
if (!file_exists($autoload)) {
    fwrite(STDERR, "config error: php/vendor/autoload.php is missing " .
        "(run ./php/build.sh first)\n");
    exit(1);
}
require $autoload;
require_once __DIR__ . '/src/Config.php';
require_once __DIR__ . '/src/Http.php';
require_once __DIR__ . '/src/Api.php';
require_once __DIR__ . '/src/WsProto.php';
require_once __DIR__ . '/src/SocketProto.php';
require_once __DIR__ . '/src/SocketStore.php';
require_once __DIR__ . '/src/HttpServer.php';

use React\EventLoop\Loop;

const G_PHP_VERSION = '0.10.0';

function g_php_usage(): void {
    fwrite(STDERR,
        "Usage: php php/server.php [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]\n" .
        "\nEvery setting can also come from a GNUMARUS_* environment variable\n" .
        "(GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from\n" .
        "php/config.cfg. Precedence (highest first): CLI flag > env var >\n" .
        "stdin > config.cfg > default.\n");
}

function g_php_fail(string $msg): void {
    fwrite(STDERR, "config error: $msg\n");
    exit(2);
}

$cli = [];
$args = array_slice($argv, 1);
for ($i = 0; $i < count($args); $i++) {
    $a = $args[$i];
    $takeValue = function (string $flag) use ($args, &$i): string {
        if ($i + 1 >= count($args)) g_php_fail("$flag needs a value");
        $i++;
        return $args[$i];
    };
    if ($a === '--host') {
        $cli['host'] = $takeValue($a);
    } elseif ($a === '--port') {
        $cli['port'] = $takeValue($a);
    } elseif ($a === '--token') {
        $cli['token'] = $takeValue($a);
    } elseif ($a === '--no-auth') {
        if (array_key_exists('no_auth', $cli) && $cli['no_auth'] === false) {
            g_php_fail('--auth and --no-auth are mutually exclusive');
        }
        $cli['no_auth'] = true;
    } elseif ($a === '--auth') {
        if (array_key_exists('no_auth', $cli) && $cli['no_auth'] === true) {
            g_php_fail('--auth and --no-auth are mutually exclusive');
        }
        $cli['no_auth'] = false;
    } elseif ($a === '-h' || $a === '--help') {
        g_php_usage();
        exit(0);
    } elseif (str_starts_with($a, '--port=')) {
        $cli['port'] = substr($a, 7);
    } elseif (str_starts_with($a, '--host=')) {
        $cli['host'] = substr($a, 7);
    } elseif (str_starts_with($a, '--token=')) {
        $cli['token'] = substr($a, 8);
    } else {
        g_php_fail("unknown argument: $a");
    }
}

try {
    $cfg = g_resolve_config($cli, g_load_stdin_config(), g_load_file_config());
} catch (GConfigError $e) {
    g_php_fail($e->getMessage());
}
$src = $cfg['sources'];
echo "config sources: host={$src['host']} port={$src['port']} " .
    "token={$src['token']} no_auth={$src['no_auth']}\n";

$api = new GApi(!$cfg['no_auth'], $cfg['token'] ?? '');
$http = new GHttpServer($api);
try {
    $server = $http->listen($cfg['host'], (int) $cfg['port']);
} catch (Throwable $e) {
    fwrite(STDERR, 'config error: cannot listen: ' . $e->getMessage() . "\n");
    exit(1);
}

echo "GnumarusNativeOps v" . G_PHP_VERSION .
    " serving on http://{$cfg['host']}:{$cfg['port']} (Ctrl+C to stop)\n";
if (!$cfg['no_auth']) {
    echo "API token: {$cfg['token']}\n";
    echo "Send it as 'Authorization: Bearer <token>' (or ?token=). /api/help stays open.\n";
} else {
    echo "WARNING: authentication DISABLED (no_auth from {$src['no_auth']}). Anyone " .
        "able to reach this server has full access, including shell execution.\n";
}

if (function_exists('pcntl_async_signals')) {
    pcntl_async_signals(true);
    $stop = function () {
        exit(130);
    };
    pcntl_signal(SIGINT, $stop);
    pcntl_signal(SIGTERM, $stop);
}
Loop::run();
