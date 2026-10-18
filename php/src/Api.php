<?php
// HTTP API implementation (used by router.php, one instance per request).

const G_VERSION = '0.10.0';
const G_SERVICE = 'GnumarusPythonNativeOperationsHttpService';
const G_OPEN_ROUTES = ['/', '/api/help'];

const G_SHELL_DEFAULT_TIMEOUT = 30;
const G_SHELL_MAX_TIMEOUT = 300;
const G_SHELL_DEFAULT_MAX_OUTPUT = 1048576;
const G_SHELL_MAX_OUTPUT = 104857600;
const G_SQLITE_DEFAULT_TIMEOUT = 5;
const G_SQLITE_MAX_TIMEOUT = 300;

class GApi {
    public bool $authRequired;
    public string $authToken;
    public string $cwd;

    public function __construct(bool $authRequired, string $authToken) {
        $this->authRequired = $authRequired;
        $this->authToken = $authToken;
        $this->cwd = getcwd() ?: '.';
    }

    /**
     * Encode a value scalar as a tagged string (i:/f:/b:/null).
     * Plain strings travel raw; only dynamically typed positions
     * (sqlite row cells, params values) take the "s:" prefix.
     */
    public static function jenc($v): string {
        if ($v === null) return 'null';
        if (is_bool($v)) return $v ? 'b:true' : 'b:false';
        if (is_int($v)) return 'i:' . $v;
        if (is_float($v)) {
            if (!is_finite($v)) throw new Exception('non-finite float');
            $s = json_encode($v);
            if ($s === false) throw new Exception('cannot encode float');
            return 'f:' . $s;
        }
        throw new Exception('jenc takes null/bool/int/float');
    }

    public static function jtagStr(string $s): string {
        return 's:' . $s;
    }

    /** Parse a tagged string back to a value. Throws on violation. */
    public static function jdec($text) {
        if (!is_string($text)) throw new Exception('expected a tagged string');
        if ($text === 'null') return null;
        if (str_starts_with($text, 'i:')) {
            $body = substr($text, 2);
            if (!preg_match('/^-?(0|[1-9][0-9]*)$/', $body)) {
                throw new Exception("bad int tag: $text");
            }
            return (int) $body;
        }
        if (str_starts_with($text, 'f:')) {
            $body = substr($text, 2);
            if (!is_numeric($body)) throw new Exception("bad float tag: $text");
            $v = (float) $body;
            if (!is_finite($v)) throw new Exception("bad float tag: $text");
            return $v;
        }
        if ($text === 'b:true') return true;
        if ($text === 'b:false') return false;
        if (str_starts_with($text, 's:')) return substr($text, 2);
        throw new Exception("untagged scalar: $text");
    }

    // -- plumbing ------------------------------------------------------
    // Responses are GResponse values (never echoed): the manual
    // server serializes them onto the connection.

    private static function err(int $status, string $message,
            ?string $path = null): GResponse {
        return GResponse::err($status, $message, $path);
    }

    /** Null when the request may proceed, else the error response. */
    public function checkAuth(GRequest $req, string $path): ?GResponse {
        if (!g_host_allowed($req->header('host'))) {
            return self::err(403, G_HOST_DENIED_MSG);
        }
        if (!$this->authRequired) return null;
        if ($req->method === 'GET' &&
                in_array($path, G_OPEN_ROUTES, true)) {
            return null;
        }
        $supplied = '';
        $auth = $req->header('authorization');
        if (str_starts_with($auth, 'Bearer ')) {
            $supplied = trim(substr($auth, 7));
        }
        if ($supplied === '' && isset($req->query['token'])) {
            $supplied = $req->query['token'];
        }
        if ($this->authToken !== '' &&
                hash_equals($this->authToken, $supplied)) {
            return null;
        }
        return GResponse::unauthorized();
    }

    /** @return array{0:array|null,1:GResponse|null} */
    private function reqJson(string $body, string $ct): array {
        if (stripos($ct, 'application/json') === false) {
            return [null, self::err(400,
                'Content-Type must be application/json.')];
        }
        if ($body === '') return [[], null];
        $obj = json_decode($body, true);
        if (json_last_error() !== JSON_ERROR_NONE) {
            return [null, self::err(400, 'Invalid JSON body.')];
        }
        // Note: JSON {} decodes to PHP [] — treat empty array as empty object.
        if (!is_array($obj) || (array_is_list($obj) && count($obj) > 0)) {
            return [null, self::err(400, 'JSON body must be an object.')];
        }
        return [$obj, null];
    }

    private function isAssoc(array $a): bool {
        return count($a) === 0 || array_keys($a) !== range(0, count($a) - 1);
    }

    private function isListOrEmpty(array $a): bool {
        return count($a) === 0 || array_is_list($a);
    }

    public static function resolvePath(string $rawPath, string $cwd): string {
        $p = $rawPath;
        if ($p === '~' || str_starts_with($p, '~/')) {
            $home = getenv('HOME') ?: (getenv('USERPROFILE') ?: '');
            if ($home !== '') $p = $home . substr($p, 1);
        }
        $abs = self::isAbsolute($p) ? $p : rtrim($cwd, '/\\') . '/' . $p;
        return self::normpath($abs);
    }

    private static function isAbsolute(string $p): bool {
        if (str_starts_with($p, '/')) return true;
        if (preg_match('/^[A-Za-z]:[\\\\\\/]/', $p)) return true;
        if (str_starts_with($p, '\\\\')) return true;
        return false;
    }

    private static function normpath(string $p): string {
        $win = (bool) preg_match('/^[A-Za-z]:[\\\\\\/]/', $p);
        $unc = str_starts_with($p, '\\\\');
        $root = '';
        $rest = $p;
        if ($win) {
            $root = substr($p, 0, 2) . '\\';
            $rest = substr($p, 2);
        } elseif ($unc) {
            $root = '\\\\';
            $rest = substr($p, 2);
        } elseif (str_starts_with($p, '/')) {
            $root = '/';
            $rest = substr($p, 1);
        }
        $sep = ($win || $unc) ? '\\' : '/';
        $rest = str_replace(['/', '\\'], $sep, $rest);
        $parts = [];
        foreach (explode($sep, $rest) as $seg) {
            if ($seg === '' || $seg === '.') continue;
            if ($seg === '..') {
                array_pop($parts);
                continue;
            }
            $parts[] = $seg;
        }
        return $root . implode($sep, $parts);
    }

    private static function lexists(string $p): bool {
        return is_link($p) || file_exists($p);
    }

    private static function entryType(string $p): string {
        if (is_link($p)) return 'symlink';
        if (is_dir($p)) return 'dir';
        if (is_file($p)) return 'file';
        return 'other';
    }

    private static function modeString(string $p): string {
        $st = @lstat($p);
        if ($st === false) return '0o644';
        return '0o' . decoct($st['mode'] & 0777);
    }

    /** @return array{0:string|null,1:GResponse|null} */
    private function reqPath(array $query): array {
        if (!isset($query['path']) || $query['path'] === '') {
            return [null, self::err(400,
                'Missing required query parameter: path')];
        }
        return [self::resolvePath($query['path'], $this->cwd), null];
    }

    // -- dispatch --------------------------------------------------------
    // Returns a GResponse, or a Promise of one for async operations
    // (shell, socket open/recv). The manual server awaits promises.

    /** @return GResponse|\React\Promise\PromiseInterface */
    public function handle(GRequest $req) {
        if ($req->method === 'OPTIONS') {
            if (!g_host_allowed($req->header('host'))) {
                return self::err(403, G_HOST_DENIED_MSG);
            }
            return GResponse::empty(204);
        }
        try {
            clearstatcache();
            if ($req->method === 'GET') return $this->doGet($req);
            if ($req->method === 'POST') return $this->doPost($req);
            if ($req->method === 'PUT') return $this->doPut($req);
            if ($req->method === 'DELETE') return $this->doDelete($req);
            $deny = $this->checkAuth($req, $req->path);
            if ($deny !== null) return $deny;
            return self::err(405, "Method {$req->method} not supported.");
        } catch (Throwable $e) {
            return self::err(500, 'Internal error: ' . $e->getMessage());
        }
    }

    /** @return GResponse|\React\Promise\PromiseInterface */
    private function doGet(GRequest $req) {
        $path = $req->path;
        $query = $req->query;
        $deny = $this->checkAuth($req, $path);
        if ($deny !== null) return $deny;
        switch ($path) {
            case '/api/help':
                return GResponse::json(200, ['version' => G_VERSION,
                    'count' => self::jenc(count($this->routes())),
                    'routes' => $this->routes(),
                    'auth' => ['required' => self::jenc($this->authRequired),
                        'open_routes' => array_merge(G_OPEN_ROUTES, ['OPTIONS *']),
                        'how' => 'Authorization: Bearer <token> header (preferred) or ?token= query parameter.']]);
            case '/api/dir':
                return $this->handleReadDir($req);
            case '/api/file':
                return $this->handleReadFile($req);
            case '/':
                return GResponse::json(200, ['service' => G_SERVICE,
                    'version' => G_VERSION, 'help' => '/api/help']);
            case '/api/shell':
            case '/api/sqlite/create':
            case '/api/sqlite/query':
            case '/api/socket/open':
            case '/api/socket/send':
            case '/api/socket/close':
            case '/api/socket/starttls':
                return self::err(405,
                    'Method GET not supported for this route (use POST).');
            case '/api/socket/recv':
                return $this->handleSocketRecv($req);
            case '/api/socket/ws':
                // The manual server intercepts upgrades before
                // dispatch; a plain GET here simply lacks one.
                return self::err(400,
                    'This route requires a WebSocket upgrade ' .
                    '(Upgrade: websocket).');
            case '/api/sysinfo':
                return $this->handleSysinfo();
            default:
                return self::err(404, "Unknown route: $path");
        }
    }

    /** @return GResponse|\React\Promise\PromiseInterface */
    private function doPost(GRequest $req) {
        $path = $req->path;
        $deny = $this->checkAuth($req, $path);
        if ($deny !== null) return $deny;
        switch ($path) {
            case '/api/file':
                return $this->handleCreateFile($req);
            case '/api/shell':
                return $this->handleExecShell($req);
            case '/api/sqlite/create':
                return $this->handleSqliteCreate($req);
            case '/api/sqlite/query':
                return $this->handleSqliteQuery($req);
            case '/api/socket/open':
                return $this->handleSocketOpen($req);
            case '/api/socket/send':
                return $this->handleSocketSend($req);
            case '/api/socket/close':
                return $this->handleSocketClose($req);
            case '/api/socket/starttls':
                return $this->handleSocketStarttls($req);
            case '/':
            case '/api/help':
            case '/api/dir':
            case '/api/sysinfo':
            case '/api/socket/recv':
            case '/api/socket/ws':
                return self::err(405,
                    'Method POST not supported for this route.');
            default:
                return self::err(404, "Unknown route: $path");
        }
    }

    private function doPut(GRequest $req): GResponse {
        $path = $req->path;
        $deny = $this->checkAuth($req, $path);
        if ($deny !== null) return $deny;
        if ($path === '/api/file') {
            return $this->handleOverwriteFile($req);
        }
        if (in_array($path, ['/', '/api/help', '/api/dir', '/api/shell',
                '/api/sqlite/create', '/api/sqlite/query', '/api/sysinfo',
                '/api/socket/open', '/api/socket/send', '/api/socket/recv',
                '/api/socket/close', '/api/socket/starttls',
                '/api/socket/ws'], true)) {
            return self::err(405, 'Method PUT not supported for this route.');
        }
        return self::err(404, "Unknown route: $path");
    }

    private function doDelete(GRequest $req): GResponse {
        $path = $req->path;
        $deny = $this->checkAuth($req, $path);
        if ($deny !== null) return $deny;
        if ($path === '/api/file') {
            return $this->handleDeleteFile($req);
        }
        if ($path === '/api/dir') {
            return $this->handleDeleteDir($req);
        }
        if (in_array($path, ['/api/dir/shallow', '/api/shallow_dir',
                '/api/dir_shallow'], true)) {
            return $this->handleDeleteDirShallow($req);
        }
        if (in_array($path, ['/', '/api/help', '/api/shell',
                '/api/sqlite/create', '/api/sqlite/query', '/api/sysinfo',
                '/api/socket/open', '/api/socket/send', '/api/socket/recv',
                '/api/socket/close', '/api/socket/starttls',
                '/api/socket/ws'], true)) {
            return self::err(405,
                'Method DELETE not supported for this route.');
        }
        return self::err(404, "Unknown route: $path");
    }

    // -- read endpoints ----------------------------------------------------

    private function handleReadDir(GRequest $req): GResponse {
        $query = $req->query;
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (!self::lexists($abs)) {
            return self::err(404, 'Path does not exist', $abs);
        }
        if (!is_dir($abs)) {
            return self::err(400, 'Path is not a directory', $abs);
        }
        $names = @scandir($abs);
        if ($names === false) {
            return self::err(403, 'Cannot list directory', $abs);
        }
        $names = array_values(array_filter($names, fn($n) => $n !== '.' && $n !== '..'));
        sort($names, SORT_STRING);
        $entries = [];
        foreach ($names as $name) {
            $full = rtrim($abs, '/\\') . DIRECTORY_SEPARATOR . $name;
            $st = @lstat($full);
            if ($st === false) continue;
            $entries[] = ['name' => $name, 'path' => $full,
                'type' => self::entryType($full), 'size' => self::jenc($st['size']),
                'atime' => self::jenc((float) $st['atime']),
                'mtime' => self::jenc((float) $st['mtime']),
                'ctime' => self::jenc((float) $st['ctime']), 'crtime' => 'null',
                'mode' => self::modeString($full)];
        }
        return GResponse::json(200, ['path' => $query['path'], 'absolute_path' => $abs,
            'count' => self::jenc(count($entries)), 'entries' => $entries]);
    }

    private static function staticMime(string $path): string {
        static $map = ['txt' => 'text/plain', 'html' => 'text/html', 'htm' => 'text/html',
            'json' => 'application/json', 'xml' => 'text/xml', 'css' => 'text/css',
            'js' => 'text/javascript', 'png' => 'image/png', 'jpg' => 'image/jpeg',
            'jpeg' => 'image/jpeg', 'gif' => 'image/gif', 'pdf' => 'application/pdf',
            'zip' => 'application/zip', 'csv' => 'text/csv', 'md' => 'text/markdown'];
        $pos = strrpos($path, '.');
        if ($pos === false) return 'application/octet-stream';
        $ext = strtolower(substr($path, $pos + 1));
        return $map[$ext] ?? 'application/octet-stream';
    }

    private function handleReadFile(GRequest $req): GResponse {
        $query = $req->query;
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (!self::lexists($abs)) {
            return self::err(404, 'Path does not exist', $abs);
        }
        if (is_dir($abs) && !is_link($abs)) {
            return self::err(400, 'Path is a directory, not a file', $abs);
        }
        $encoding = strtolower($query['encoding'] ?? 'auto');
        if (!in_array($encoding, ['auto', 'text', 'base64'], true)) {
            return self::err(400, "Invalid encoding param: '$encoding' (use auto|text|base64)");
        }
        $rawMode = ($query['raw'] ?? '0') === '1';
        $data = @file_get_contents($abs);
        if ($data === false) {
            if (is_dir($abs)) {
                return self::err(400, 'Path is a directory, not a file', $abs);
            }
            if (!self::lexists($abs)) {
                return self::err(404, 'Path does not exist', $abs);
            }
            return self::err(403, 'Cannot read file', $abs);
        }
        $st = @lstat($abs);
        $mtime = $st === false ? null : (float) $st['mtime'];
        if ($rawMode) {
            return GResponse::raw(200, $data, self::staticMime($abs));
        }
        $mime = self::staticMime($abs);
        $meta = fn(bool $binary, string $enc, string $content) => [
            'path' => $query['path'], 'absolute_path' => $abs,
            'size' => self::jenc(strlen($data)),
            'mtime' => $mtime === null ? 'null' : self::jenc($mtime),
            'mime_type' => $mime, 'is_binary' => self::jenc($binary),
            'encoding' => $enc, 'content' => $content];
        if ($encoding === 'text') {
            return GResponse::json(200, $meta(false, 'utf-8', self::sanitizeUtf8($data)));
        }
        if ($encoding === 'base64') {
            return GResponse::json(200, $meta(true, 'base64', base64_encode($data)));
        }
        if (mb_check_encoding($data, 'UTF-8') && !self::hasBinaryControls($data)) {
            return GResponse::json(200, $meta(false, 'utf-8', $data));
        }
        return GResponse::json(200, $meta(true, 'base64', base64_encode($data)));
    }

    private static function hasBinaryControls(string $data): bool {
        $len = strlen($data);
        for ($i = 0; $i < $len; $i++) {
            $o = ord($data[$i]);
            if ($o < 32 && $o !== 9 && $o !== 10 && $o !== 13) return true;
        }
        return false;
    }

    private static function sanitizeUtf8(string $data): string {
        if (function_exists('mb_scrub')) return mb_scrub($data, 'UTF-8');
        return (string) iconv('UTF-8', 'UTF-8//IGNORE', $data);
    }

    // -- write/delete endpoints ----------------------------------------------

    /** @return array{0:string|null,1:GResponse|null} */
    private function parseWriteBody(string $body,
            string $ct): array {
        if (stripos($ct, 'application/json') === false) return [$body, null];
        if ($body === '') return ['', null];
        $obj = json_decode($body, true);
        if (json_last_error() !== JSON_ERROR_NONE) {
            return [null, self::err(400, 'Invalid JSON body.')];
        }
        if (!is_array($obj) || !$this->isAssoc($obj)) {
            return [null, self::err(400, 'JSON body must be an object.')];
        }
        $content = $obj['content'] ?? null;
        $encoding = 'text';
        if (array_key_exists('encoding', $obj)) {
            if (!is_string($obj['encoding'])) {
                return [null, self::err(400,
                    'Invalid encoding: (use text|base64).')];
            }
            $encoding = strtolower($obj['encoding']);
        }
        if ($encoding === 'text') {
            if ($content === null) return ['', null];
            if (!is_string($content)) {
                return [null, self::err(400,
                    "'content' must be a string for text encoding.")];
            }
            return [$content, null];
        }
        if ($encoding === 'base64') {
            if ($content === null) return ['', null];
            if (!is_string($content)) {
                return [null, self::err(400,
                    "'content' must be a base64 string.")];
            }
            $data = base64_decode($content, true);
            if ($data === false) {
                return [null, self::err(400, "Invalid base64 'content'.")];
            }
            return [$data, null];
        }
        return [null, self::err(400,
            "Invalid encoding: '$encoding' (use text|base64).")];
    }

    private function handleCreateFile(GRequest $req): GResponse {
        $query = $req->query;
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (self::lexists($abs)) {
            return self::err(409, 'Path already exists (use PUT to overwrite).', $abs);
        }
        $parent = dirname($abs);
        if (!is_dir($parent)) {
            return self::err(404, 'Parent directory does not exist.', $abs);
        }
        if (!is_writable($parent)) {
            return self::err(403, 'Permission denied', $abs);
        }
        [$data, $bodyErr] = $this->parseWriteBody($req->body, $req->header('Content-Type'));
        if ($bodyErr !== null) return $bodyErr;
        $fh = @fopen($abs, 'xb');
        if ($fh === false) {
            if (self::lexists($abs)) {
                return self::err(409, 'Path already exists (use PUT to overwrite).', $abs);
            }
            return self::err(400, 'Cannot create file', $abs);
        }
        fwrite($fh, $data);
        fclose($fh);
        return GResponse::json(201, ['path' => $query['path'], 'absolute_path' => $abs,
            'size' => self::jenc(strlen($data)), 'mime_type' => self::staticMime($abs)]);
    }

    private function handleOverwriteFile(GRequest $req): GResponse {
        $query = $req->query;
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (!self::lexists($abs)) {
            return self::err(404, 'Path does not exist', $abs);
        }
        if (is_dir($abs) && !is_link($abs)) {
            return self::err(400, 'Path is a directory, not a file', $abs);
        }
        if (!is_writable($abs)) {
            return self::err(403, 'Permission denied', $abs);
        }
        [$data, $bodyErr] = $this->parseWriteBody($req->body, $req->header('Content-Type'));
        if ($bodyErr !== null) return $bodyErr;
        $r = @file_put_contents($abs, $data);
        if ($r === false) {
            if (!self::lexists($abs)) {
                return self::err(404, 'Path does not exist', $abs);
            }
            if (is_dir($abs)) {
                return self::err(400, 'Path is a directory, not a file', $abs);
            }
            return self::err(400, 'Cannot write file', $abs);
        }
        return GResponse::json(200, ['path' => $query['path'], 'absolute_path' => $abs,
            'size' => self::jenc(strlen($data)), 'mime_type' => self::staticMime($abs)]);
    }

    private function handleDeleteFile(GRequest $req): GResponse {
        $query = $req->query;
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (!self::lexists($abs)) {
            return self::err(404, 'Path does not exist', $abs);
        }
        if (is_dir($abs) && !is_link($abs)) {
            return self::err(400, 'Path is a directory, not a file (use DELETE /api/dir).', $abs);
        }
        if (!is_writable($abs) || !is_writable(dirname($abs))) {
            return self::err(403, 'Permission denied', $abs);
        }
        if (!@unlink($abs)) {
            if (!self::lexists($abs)) {
                return self::err(404, 'Path does not exist', $abs);
            }
            return self::err(400, 'Cannot delete file', $abs);
        }
        return GResponse::json(200, ['path' => $query['path'],
            'absolute_path' => $abs, 'deleted' => self::jenc(true)]);
    }

    private function handleDeleteDir(GRequest $req): GResponse {
        $query = $req->query;
        $shallow = isset($query['shallow']) ? trim(strtolower((string)$query['shallow'])) : '';
        if (in_array($shallow, ['1', 'true', 'yes', 'on'], true)) {
            return $this->handleDeleteDirShallow($req);
        }
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (!self::lexists($abs)) {
            return self::err(404, 'Path does not exist', $abs);
        }
        if (is_link($abs) || !is_dir($abs)) {
            return self::err(400, 'Path is not a directory', $abs);
        }
        if (!is_writable($abs) || !is_writable(dirname($abs))) {
            return self::err(403, 'Permission denied', $abs);
        }
        if (!@rmdir($abs)) {
            if (!self::lexists($abs)) {
                return self::err(404, 'Path does not exist', $abs);
            }
            $names = @scandir($abs);
            if (is_array($names) && count(array_filter($names,
                    fn($n) => $n !== '.' && $n !== '..')) > 0) {
                return self::err(400, "Cannot delete directory: Directory not empty: '$abs'", $abs);
            }
            return self::err(400, 'Cannot delete directory', $abs);
        }
        return GResponse::json(200, ['path' => $query['path'],
            'absolute_path' => $abs, 'deleted' => self::jenc(true)]);
    }

    private function handleDeleteDirShallow(GRequest $req): GResponse {
        $query = $req->query;
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (!self::lexists($abs)) {
            return self::err(404, 'Path does not exist', $abs);
        }
        if (is_link($abs) || !is_dir($abs)) {
            return self::err(400, 'Path is not a directory', $abs);
        }
        $names = @scandir($abs);
        if (!is_array($names)) {
            return self::err(400, 'Cannot delete directory', $abs);
        }
        $entries = array_values(array_filter($names,
            fn($n) => $n !== '.' && $n !== '..'));
        foreach ($entries as $name) {
            $sub = $abs . '/' . $name;
            if (is_dir($sub) && !is_link($sub)) {
                $subNames = @scandir($sub);
                $subEntries = is_array($subNames) ? array_values(array_filter(
                    $subNames, fn($n) => $n !== '.' && $n !== '..')) : [];
                if (count($subEntries) > 0) {
                    return self::err(400,
                        'Directory is not shallow (contains a non-empty subdirectory)',
                        $abs);
                }
            }
        }
        foreach ($entries as $name) {
            $p = $abs . '/' . $name;
            if (is_dir($p) && !is_link($p)) {
                if (!@rmdir($p)) {
                    return self::err(400, 'Cannot delete directory', $abs);
                }
            } else {
                if (!@unlink($p)) {
                    return self::err(400, 'Cannot delete directory', $abs);
                }
            }
        }
        if (!@rmdir($abs)) {
            if (!self::lexists($abs)) {
                return self::err(404, 'Path does not exist', $abs);
            }
            return self::err(400, 'Cannot delete directory', $abs);
        }
        return GResponse::json(200, ['path' => $query['path'],
            'absolute_path' => $abs, 'deleted' => self::jenc(true)]);
    }

    // -- shell ------------------------------------------------------------------

    // -- socket proxy (sessions live in GSocketStore, in-process) -------

    /** @return GResponse|\React\Promise\PromiseInterface */
    private function handleSocketOpen(GRequest $req) {
        return GSocketStore::open(
            $req->header('Content-Type'), $req->body);
    }

    private function handleSocketSend(GRequest $req): GResponse {
        [$sid, $sess, $lookupErr] =
            GSocketStore::lookup($req->query);
        if ($lookupErr !== null) return $lookupErr;
        if ($sess['wsAttached']) {
            return self::err(400, 'Socket session is attached to a ' .
                'websocket (HTTP send refused while attached).');
        }
        return GSocketStore::send($sid,
            $req->header('Content-Type'), $req->body);
    }

    /** @return GResponse|\React\Promise\PromiseInterface */
    private function handleSocketRecv(GRequest $req) {
        return GSocketStore::recv($req->query);
    }

    private function handleSocketClose(GRequest $req): GResponse {
        [$sid, $sess, $lookupErr] =
            GSocketStore::lookup($req->query);
        if ($lookupErr !== null) return $lookupErr;
        return GSocketStore::close($sid);
    }

    private function handleSocketStarttls(GRequest $req): GResponse {
        $sid = $req->query['id'] ?? '';
        if ($sid === '') {
            return self::err(400, 'Missing required query parameter: id');
        }
        return GSocketStore::starttls((string) $sid,
            $req->header('Content-Type'), $req->body);
    }

    /** @return GResponse|\React\Promise\PromiseInterface */
    private function handleExecShell(GRequest $req) {
        [$obj, $reqErr] = $this->reqJson($req->body,
            $req->header('Content-Type'));
        if ($reqErr !== null) return $reqErr;
        if (!isset($obj['command']) || !is_string($obj['command']) ||
                trim($obj['command']) === '') {
            return self::err(400,
                'Missing or empty required field: command.');
        }
        $command = $obj['command'];
        $timeout = G_SHELL_DEFAULT_TIMEOUT;
        if (array_key_exists('timeout', $obj) && $obj['timeout'] !== null) {
            try {
                $t = self::jdec($obj['timeout']);
            } catch (Exception) {
                return self::err(400, "Field 'timeout' must be a tagged " .
                    'number (i:<int> or f:<float>).');
            }
            if (!is_int($t) && !is_float($t)) {
                return self::err(400, "Field 'timeout' must be a tagged " .
                    'number (i:<int> or f:<float>).');
            }
            $timeout = max(1, min((float) $t, G_SHELL_MAX_TIMEOUT));
        }
        $cwd = $this->cwd;
        if (array_key_exists('cwd', $obj) && $obj['cwd'] !== null) {
            if (!is_string($obj['cwd'])) {
                return self::err(400, "Field 'cwd' must be a string.");
            }
            $cwd = self::resolvePath($obj['cwd'], $this->cwd);
            if (!is_dir($cwd)) {
                return self::err(400,
                    'Working directory does not exist.', $cwd);
            }
        }
        [$maxOut, $maxOutErr] = $this->shellLimit($obj, 'max_stdout');
        if ($maxOutErr !== null) return $maxOutErr;
        [$maxErr, $maxErrErr] = $this->shellLimit($obj, 'max_stderr');
        if ($maxErrErr !== null) return $maxErrErr;

        $stdinRaw = $obj['stdin'] ?? '';
        if ($stdinRaw === null) $stdinRaw = '';
        if (!is_string($stdinRaw)) {
            return self::err(400, "Field 'stdin' must be a string.");
        }
        $stdinEnc = 'text';
        if (array_key_exists('stdin_encoding', $obj) &&
                $obj['stdin_encoding'] !== null) {
            if (!is_string($obj['stdin_encoding'])) {
                return self::err(400,
                    "Field 'stdin_encoding' must be text|base64.");
            }
            $stdinEnc = strtolower($obj['stdin_encoding']);
        }
        if ($stdinEnc === 'text') {
            $stdinData = $stdinRaw;
        } elseif ($stdinEnc === 'base64') {
            $stdinData = base64_decode($stdinRaw, true);
            if ($stdinData === false) {
                return self::err(400, "Invalid base64 'stdin'.");
            }
        } else {
            return self::err(400,
                "Field 'stdin_encoding' must be text|base64.");
        }

        // Async child (the loop must stay responsive while the
        // command runs): exec+setsid makes the child its own
        // process-group leader, so a timeout kills the whole group
        // (killpg parity with the other ports).
        $deferred = new React\Promise\Deferred();
        $proc = new React\ChildProcess\Process(
            'exec setsid bash -c ' . escapeshellarg($command), $cwd);
        try {
            $proc->start();
        } catch (Throwable $e) {
            return self::err(400,
                'Cannot start shell: ' . $e->getMessage());
        }
        $pid = $proc->getPid() ?? 0;
        $out = '';
        $errOut = '';
        $outCut = false;
        $errCut = false;
        $inLen = strlen($stdinData);
        $settled = false;
        $finish = function (int $returncode, bool $timedOut) use (
                &$settled, $deferred, &$out, &$errOut, &$outCut,
                &$errCut, $command, $cwd, $maxOut, $maxErr, $inLen,
                &$timer) {
            if ($settled) return;
            $settled = true;
            if (isset($timer)) {
                try {
                    React\EventLoop\Loop::cancelTimer($timer);
                } catch (Throwable) {
                }
            }
            $deferred->resolve(GResponse::json(200, [
                'command' => $command, 'cwd' => $cwd,
                'returncode' => GApi::jenc($returncode),
                'stdout' => self::sanitizeUtf8($out),
                'stderr' => self::sanitizeUtf8($errOut),
                'timed_out' => GApi::jenc($timedOut),
                'stdout_truncated' => GApi::jenc($outCut),
                'stderr_truncated' => GApi::jenc($errCut),
                'max_stdout' => GApi::jenc($maxOut),
                'max_stderr' => GApi::jenc($maxErr),
                'stdin_bytes' => GApi::jenc($inLen)]));
        };
        $proc->stdout->on('data', function ($chunk) use (
                &$out, &$outCut, $maxOut) {
            $chunk = (string) $chunk;
            $room = $maxOut - strlen($out);
            if ($room > 0) $out .= substr($chunk, 0, $room);
            if (strlen($chunk) > max(0, $room)) $outCut = true;
        });
        $proc->stderr->on('data', function ($chunk) use (
                &$errOut, &$errCut, $maxErr) {
            $chunk = (string) $chunk;
            $room = $maxErr - strlen($errOut);
            if ($room > 0) $errOut .= substr($chunk, 0, $room);
            if (strlen($chunk) > max(0, $room)) $errCut = true;
        });
        $exited = false;
        $exitCode = 0;
        $outClosed = false;
        $errClosed = false;
        $maybeDone = function () use (&$exited, &$outClosed,
                &$errClosed, &$exitCode, $finish) {
            if ($exited && $outClosed && $errClosed) {
                $finish($exitCode, false);
            }
        };
        $proc->stdout->on('close', function () use (
                &$outClosed, $maybeDone) {
            $outClosed = true;
            $maybeDone();
        });
        $proc->stderr->on('close', function () use (
                &$errClosed, $maybeDone) {
            $errClosed = true;
            $maybeDone();
        });
        $proc->on('exit', function ($code) use (&$exited,
                &$exitCode, $maybeDone) {
            $exited = true;
            $exitCode = $code === null ? 0 : (int) $code;
            $maybeDone();
        });
        // Feed stdin, then EOF (empty stdin: EOF at once).
        if ($inLen > 0) {
            try {
                $proc->stdin->write($stdinData);
            } catch (Throwable) {
            }
        }
        try {
            $proc->stdin->end();
        } catch (Throwable) {
        }
        $timer = React\EventLoop\Loop::addTimer($timeout,
            function () use ($proc, $pid, $finish) {
                if (function_exists('posix_kill') && $pid > 1) {
                    @posix_kill(-$pid, 9);
                }
                try {
                    $proc->terminate(9);
                } catch (Throwable) {
                }
                $finish(124, true);
            });
        return $deferred->promise();
    }

    /** @return array{0:int|null,1:GResponse|null} */
    private function shellLimit(array $obj, string $key): array {
        if (!array_key_exists($key, $obj) || $obj[$key] === null) {
            return [G_SHELL_DEFAULT_MAX_OUTPUT, null];
        }
        try {
            $v = self::jdec($obj[$key]);
        } catch (Exception) {
            return [null, self::err(400,
                "Field '$key' must be an integer >= 0 (i:<int>).")];
        }
        if (!is_int($v) || $v < 0) {
            return [null, self::err(400,
                "Field '$key' must be an integer >= 0 (i:<int>).")];
        }
        if ($v > G_SHELL_MAX_OUTPUT) {
            return [null, self::err(400, "Field '$key' exceeds the " .
                G_SHELL_MAX_OUTPUT . ' byte cap.')];
        }
        return [$v, null];
    }

    // -- sqlite -------------------------------------------------------------------

    private static function sqliteAvailable(): bool {
        static $ok = null;
        if ($ok === null) {
            $ok = in_array('sqlite', PDO::getAvailableDrivers(), true);
        }
        return $ok;
    }

    private function handleSqliteCreate(GRequest $req): GResponse {
        $query = $req->query;
        if (!self::sqliteAvailable()) {
            return self::err(503, 'SQLite support unavailable: pdo_sqlite driver not installed.');
        }
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (self::lexists($abs)) {
            return self::err(409, 'Database file already exists.', $abs);
        }
        $parent = dirname($abs);
        if (!is_dir($parent)) {
            return self::err(404, 'Parent directory does not exist.', $abs);
        }
        try {
            $pdo = new PDO('sqlite:' . $abs);
            $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
            $version = $pdo->query('SELECT sqlite_version()')->fetchColumn();
            $pdo = null;
        } catch (PDOException $e) {
            @unlink($abs);
            return self::err(400, 'Cannot create database: ' . $e->getMessage(), $abs);
        }
        $size = is_file($abs) ? filesize($abs) : 0;
        return GResponse::json(201, ['path' => $query['path'], 'absolute_path' => $abs,
            'size' => self::jenc($size === false ? 0 : $size),
            'sqlite_version' => $version]);
    }

    private function handleSqliteQuery(GRequest $req): GResponse {
        $query = $req->query;
        if (!self::sqliteAvailable()) {
            return self::err(503, 'SQLite support unavailable: pdo_sqlite driver not installed.');
        }
        [$abs, $absErr] = $this->reqPath($query);
        if ($absErr !== null) return $absErr;
        if (!self::lexists($abs)) {
            return self::err(404, 'Database file does not exist', $abs);
        }
        [$obj, $reqErr] = $this->reqJson($req->body, $req->header('Content-Type'));
        if ($reqErr !== null) return $reqErr;
        if (!isset($obj['sql']) || !is_string($obj['sql']) || trim($obj['sql']) === '') {
            return self::err(400, 'Missing or empty required field: sql.');
        }
        $sql = $obj['sql'];
        $params = $obj['params'] ?? [];
        if ($params === null) $params = [];
        if ($params !== [] && !is_array($params)) {
            return self::err(400, "Field 'params' must be an array or an object.");
        }
        if (is_array($params) && !array_is_list($params) && !$this->isAssoc($params)) {
            return self::err(400, "Field 'params' must be an array or an object.");
        }
        if (is_array($params)) {
            $named = !$this->isListOrEmpty($params);
            foreach ($params as $k => $v) {
                if (!is_string($v)) {
                    return self::err(400, $named
                        ? 'Object params must be tagged scalars (i:/f:/s:/b:/null).'
                        : 'Array params must be tagged scalars (i:/f:/s:/b:/null).');
                }
                try {
                    $params[$k] = self::jdec($v);
                } catch (Exception) {
                    return self::err(400, $named
                        ? 'Object params must be tagged scalars (i:/f:/s:/b:/null).'
                        : 'Array params must be tagged scalars (i:/f:/s:/b:/null).');
                }
            }
        }
        $timeout = G_SQLITE_DEFAULT_TIMEOUT;
        if (array_key_exists('timeout', $obj) && $obj['timeout'] !== null) {
            if (!is_string($obj['timeout'])) {
                return self::err(400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
            }
            try {
                $t = self::jdec($obj['timeout']);
            } catch (Exception) {
                return self::err(400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
            }
            if (!is_int($t) && !is_float($t)) {
                return self::err(400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
            }
            if (!($t > 0 && $t <= G_SQLITE_MAX_TIMEOUT)) {
                return self::err(400, "Field 'timeout' must be within (0, 300].");
            }
            $timeout = (float) $t;
        }
        $readonly = false;
        if (array_key_exists('readonly', $obj) && $obj['readonly'] !== null) {
            if (!is_string($obj['readonly'])) {
                return self::err(400, "Field 'readonly' must be b:true or b:false.");
            }
            try {
                $r = self::jdec($obj['readonly']);
            } catch (Exception) {
                return self::err(400, "Field 'readonly' must be b:true or b:false.");
            }
            if (!is_bool($r)) {
                return self::err(400, "Field 'readonly' must be b:true or b:false.");
            }
            $readonly = $r;
        }
        try {
            $scan = self::scanSql($sql);
        } catch (Exception $e) {
            return self::err(400, $e->getMessage(), $abs);
        }
        if (count($scan['statements']) !== 1) {
            return self::err(400, 'Send exactly one SQL statement per request.', $abs);
        }
        $single = $scan['statements'][0];
        $rowsMode = in_array($scan['first'], self::rowFirstWords(), true) || $scan['returning'];
        // Native binding: PDO takes ? and :named as-is. Values decoded
        // from tags above; bools become 1/0 for Python parity.
        $bound = self::toPdoParams($params);
        try {
            $pdo = new PDO('sqlite:' . $abs);
            $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
            $pdo->setAttribute(PDO::ATTR_TIMEOUT, (int) $timeout);
            if ($readonly) $pdo->exec('PRAGMA query_only=ON');
            if ($rowsMode) {
                $stmt = $pdo->prepare($single);
                try {
                    $stmt->execute($bound);
                } catch (PDOException $e) {
                    return self::err(400, 'SQLite error: ' . $e->getMessage(), $abs);
                }
                $columns = [];
                for ($i = 0; $i < $stmt->columnCount(); $i++) {
                    $meta = $stmt->getColumnMeta($i);
                    $columns[] = $meta['name'];
                }
                $rows = [];
                foreach ($stmt->fetchAll(PDO::FETCH_NUM) as $row) {
                    $out = [];
                    foreach ($row as $v) {
                        if ($v === null) {
                            $out[] = 'null';
                        } elseif (is_bool($v)) {
                            $out[] = self::jenc($v);
                        } elseif (is_int($v)) {
                            $out[] = self::jenc($v);
                        } elseif (is_float($v)) {
                            $out[] = self::jenc($v);
                        } elseif (is_string($v) && !mb_check_encoding($v, 'UTF-8')) {
                            $out[] = self::jtagStr(base64_encode($v));
                        } elseif (is_string($v)) {
                            $out[] = self::jtagStr($v);
                        } else {
                            $out[] = self::jtagStr((string) $v);
                        }
                    }
                    $rows[] = $out;
                }
                $payload = ['columns' => $columns, 'rows' => $rows,
                    'rowcount' => self::jenc(count($rows))];
            } else {
                $stmt = $pdo->prepare($single);
                try {
                    $stmt->execute($bound);
                } catch (PDOException $e) {
                    return self::err(400, 'SQLite error: ' . $e->getMessage(), $abs);
                }
                $affected = $stmt->rowCount();
                $lastId = (int) $pdo->lastInsertId();
                $rc = in_array($scan['first'], self::ddlFirstWords(), true) ? -1 : $affected;
                $payload = ['rowcount' => self::jenc($rc),
                    'lastrowid' => self::jenc($lastId)];
            }
            $pdo = null;
        } catch (PDOException $e) {
            return self::err(400, 'SQLite error: ' . $e->getMessage(), $abs);
        }
        $payload['path'] = $query['path'];
        $payload['absolute_path'] = $abs;
        return GResponse::json(200, $payload);
    }

    private static function rowFirstWords(): array {
        return ['SELECT', 'VALUES', 'WITH', 'EXPLAIN', 'PRAGMA', 'TABLE'];
    }

    private static function ddlFirstWords(): array {
        return ['CREATE', 'DROP', 'ALTER', 'VACUUM', 'ANALYZE', 'REINDEX',
            'ATTACH', 'DETACH', 'BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'END'];
    }

    /**
     * Map decoded tag values to PDO bind values (bool -> 1/0 to match
     * the other ports; named keys pass through untouched).
     */
    private static function toPdoParams($params): array {
        if (!is_array($params)) return [];
        $out = [];
        foreach ($params as $k => $v) {
            $out[$k] = is_bool($v) ? (int) $v : $v;
        }
        return $out;
    }

    /**
     * Split SQL into statements; find first keyword and RETURNING.
     * String/comment aware so separators inside literals don't split.
     */
    private static function scanSql(string $sql): array {
        $stmts = [];
        $cur = '';
        $first = null;
        $returning = false;
        $word = '';
        $flush = function () use (&$word, &$first, &$returning) {
            if ($word !== '') {
                $w = strtoupper($word);
                if ($first === null) $first = $w;
                if ($w === 'RETURNING') $returning = true;
                $word = '';
            }
        };
        $state = 0;
        $i = 0;
        $n = strlen($sql);
        $isWordChar = fn($c) => ctype_alpha($c) || $c === '_';
        while ($i < $n) {
            $c = $sql[$i];
            if ($state === 0) {
                if ($c === "'") {
                    $state = 1;
                    $cur .= $c;
                    $i++;
                } elseif ($c === '"') {
                    $state = 2;
                    $cur .= $c;
                    $i++;
                } elseif ($c === '`') {
                    $state = 3;
                    $cur .= $c;
                    $i++;
                } elseif ($c === '-' && $i + 1 < $n && $sql[$i + 1] === '-') {
                    $state = 4;
                    $i += 2;
                } elseif ($c === '/' && $i + 1 < $n && $sql[$i + 1] === '*') {
                    $state = 5;
                    $i += 2;
                } elseif ($c === ';') {
                    $flush();
                    if (trim($cur) !== '') $stmts[] = $cur;
                    $cur = '';
                    $i++;
                } elseif ($isWordChar($c)) {
                    $word .= $c;
                    $cur .= $c;
                    $i++;
                } else {
                    if (!ctype_alnum($c) && $c !== '_' && $c !== '$') $flush();
                    $cur .= $c;
                    $i++;
                }
            } elseif ($state === 1) {
                if ($c === "'" && $i + 1 < $n && $sql[$i + 1] === "'") {
                    $cur .= "''";
                    $i += 2;
                } else {
                    $cur .= $c;
                    $i++;
                    if ($c === "'") $state = 0;
                }
            } elseif ($state === 2) {
                if ($c === '"' && $i + 1 < $n && $sql[$i + 1] === '"') {
                    $cur .= '""';
                    $i += 2;
                } else {
                    $cur .= $c;
                    $i++;
                    if ($c === '"') $state = 0;
                }
            } elseif ($state === 3) {
                $cur .= $c;
                $i++;
                if ($c === '`') $state = 0;
            } elseif ($state === 4) {
                if ($c === "\n") $state = 0;
                $i++;
            } else {
                if ($c === '*' && $i + 1 < $n && $sql[$i + 1] === '/') {
                    $state = 0;
                    $i += 2;
                } else {
                    $i++;
                }
            }
        }
        $flush();
        if (trim($cur) !== '') $stmts[] = $cur;
        return ['statements' => $stmts, 'first' => $first ?? '', 'returning' => $returning];
    }

    // -- sysinfo --------------------------------------------------------------------

    private static function readOsRelease(): array {
        $info = [];
        try {
            $lines = @file('/etc/os-release', FILE_IGNORE_NEW_LINES);
            if (!is_array($lines)) return [];
            foreach ($lines as $line) {
                $t = trim($line);
                if ($t === '' || $t[0] === '#' || !str_contains($t, '=')) continue;
                $eq = strpos($t, '=');
                $info[substr($t, 0, $eq)] = trim(trim(substr($t, $eq + 1)), '"\'');
            }
        } catch (Throwable) {
        }
        return $info;
    }

    private function handleSysinfo(): GResponse {
        $os = php_uname('s');
        if (str_starts_with($os, 'Windows')) {
            $system = 'Windows';
        } elseif (str_starts_with($os, 'Darwin') || str_starts_with($os, 'Mac')) {
            $system = 'Darwin';
        } else {
            $system = 'Linux';
        }
        $distro = null;
        $windows = null;
        $mac = null;
        if ($system === 'Linux') {
            $rel = self::readOsRelease();
            if ($rel !== []) {
                $distro = ['id' => $rel['ID'] ?? '', 'name' => $rel['NAME'] ?? '',
                    'version' => $rel['VERSION'] ?? '',
                    'version_id' => $rel['VERSION_ID'] ?? '',
                    'pretty_name' => $rel['PRETTY_NAME'] ?? ''];
            }
        } elseif ($system === 'Windows') {
            $windows = ['release' => $os, 'version' => php_uname('r'),
                'service_pack' => '', 'edition' => ''];
        } elseif ($system === 'Darwin') {
            $mac = ['version' => php_uname('r')];
        }
        $hostname = gethostname();
        if ($hostname === false) $hostname = '';
        $cpu = 1;
        if ($system === 'Linux') {
            $info = @file_get_contents('/proc/cpuinfo');
            if (is_string($info)) {
                $cpu = max(1, substr_count($info, 'processor'));
            }
        }
        return GResponse::json(200, ['system' => $system,
            'system_name' => $system === 'Darwin' ? 'macOS' : $system,
            'release' => php_uname('r'), 'version' => php_uname('v'),
            'machine' => php_uname('m'), 'processor' => '',
            'hostname' => $hostname, 'python_version' => 'null',
            'php_version' => PHP_VERSION,
            'cpu_count' => self::jenc($cpu),
            'distro' => $distro === null ? 'null' : $distro,
            'windows' => $windows === null ? 'null' : $windows,
            'mac' => $mac === null ? 'null' : $mac]);
    }

    // -- route catalog ----------------------------------------------------------

    private static function paramP(string $name, string $location, bool $required,
            string $type, string $desc, ?string $default = null,
            ?array $allowed = null): array {
        $p = ['name' => $name, 'location' => $location,
            'required' => self::jenc($required),
            'type' => $type, 'description' => $desc];
        if ($default !== null) $p['default'] = $default;
        if ($allowed !== null) $p['allowed'] = $allowed;
        return $p;
    }

    private static function routeE(string $method, string $url, string $name,
            string $desc, array $params, string $response,
            ?string $errors = null): array {
        $r = ['method' => $method, 'url' => $url, 'name' => $name,
            'description' => $desc, 'params' => $params, 'response' => $response];
        if ($errors !== null) $r['errors'] = $errors;
        return $r;
    }

    private function routes(): array {
        return [
            self::routeE('GET', '/api/help', 'help',
                'Return the full list of implemented routes with URL, name, description and parameter formats. Wire convention: JSON carries only string/array/object - numbers, booleans and nulls travel as tagged strings (i:<int>, f:<float>, b:true|false, null). Fixed vocabulary and free text stay raw; sqlite row cells and params values are fully tagged (strings take an s: prefix there).',
                [], 'JSON object { version, count, routes: [...] }.'),
            self::routeE('GET', '/api/dir?path=<path>', 'read_directory',
                'List the contents of a directory.',
                [self::paramP('path', 'query', true, 'string',
                    'Absolute or relative path of the directory. Relative paths resolve against the server working directory. Paths starting with ~/ expand to the server user\'s home directory.')],
                'JSON { path, absolute_path, count, entries: [{ name, path, type, size, atime, mtime, ctime, crtime, mode }] }. type is one of file|dir|symlink|other. Times are epoch seconds: atime (last access), mtime (last modification), ctime (last status change; creation on Windows), crtime (creation/birth, null where unavailable).',
                '400 missing path / not-a-directory, 404 not found, 403 forbidden.'),
            self::routeE('GET', '/api/file?path=<path>[&encoding=text|base64|auto][&raw=1]', 'read_file',
                'Read a file. Supports text and binary files. Default JSON mode auto-detects: UTF-8-decodable content is returned as text, otherwise as base64. With raw=1 the raw bytes are returned with a guessed Content-Type.',
                [self::paramP('path', 'query', true, 'string',
                    'Absolute or relative path of the file. Relative paths resolve against the server working directory; ~/ expands to the user\'s home directory.'),
                    self::paramP('encoding', 'query', false, 'string',
                        'auto: try UTF-8 text, fall back to base64. text: force UTF-8 decode with replacement of invalid sequences. base64: force base64.',
                        'auto', ['auto', 'text', 'base64']),
                    self::paramP('raw', 'query', false, 'string',
                        "If '1', respond with raw file bytes and a guessed Content-Type instead of JSON.",
                        '0', ['0', '1'])],
                'JSON mode: { path, absolute_path, size, mtime, mime_type, is_binary, encoding, content }. Raw mode: file bytes with Content-Type + Content-Length headers.',
                '400 missing path / is-a-directory, 404 not found, 403 forbidden.'),
            self::routeE('POST', '/api/file?path=<path>', 'create_file',
                'Create a file, empty when no body is sent or with initial data (text or binary). Fails if the path already exists (use PUT to overwrite).',
                [self::paramP('path', 'query', true, 'string',
                    'Absolute or relative path of the file to create. ~/ expands to the server user\'s home directory.'),
                    self::paramP('content', 'body (JSON)', false, 'string',
                        'Initial content. Plain string for encoding=text, base64 string for encoding=base64. Omitted means empty file. Alternatively send raw bytes with a non-JSON Content-Type.'),
                    self::paramP('encoding', 'body (JSON)', false, 'string',
                        'How to interpret the JSON body content.', 'text', ['text', 'base64'])],
                '201 JSON { path, absolute_path, size, mime_type }.',
                '400 missing path / bad body, 404 parent directory missing, 409 already exists, 403 forbidden.'),
            self::routeE('PUT', '/api/file?path=<path>', 'overwrite_file',
                'Overwrite the content of an existing file (text or binary). Same body rules as create_file.',
                [self::paramP('path', 'query', true, 'string',
                    'Absolute or relative path of the file to overwrite. ~/ expands to the server user\'s home directory.'),
                    self::paramP('content', 'body (JSON)', false, 'string',
                        'New content (see create_file). Omitted truncates the file to zero bytes.'),
                    self::paramP('encoding', 'body (JSON)', false, 'string',
                        'How to interpret the JSON body content.', 'text', ['text', 'base64'])],
                '200 JSON { path, absolute_path, size, mime_type }.',
                '400 missing path / is-a-directory / bad body, 404 not found, 403 forbidden.'),
            self::routeE('DELETE', '/api/file?path=<path>', 'delete_file',
                'Delete a file.',
                [self::paramP('path', 'query', true, 'string',
                    'Absolute or relative path of the file to delete. ~/ expands to the server user\'s home directory.')],
                '200 JSON { path, absolute_path, deleted: true }.',
                '400 missing path / is-a-directory, 404 not found, 403 forbidden.'),
            self::routeE('DELETE', '/api/dir?path=<path>', 'delete_directory',
                'Delete an empty directory (rmdir equivalent). Fails when the directory is not empty.',
                [self::paramP('path', 'query', true, 'string',
                    'Absolute or relative path of the empty directory to delete. ~/ expands to the server user\'s home directory.')],
                '200 JSON { path, absolute_path, deleted: true }.',
                '400 missing path / not-a-directory / not-empty, 404 not found, 403 forbidden.'),
            self::routeE('DELETE', '/api/dir/shallow?path=<path>', 'delete_directory_shallow',
                'Delete a shallow directory: a directory containing only files (including symlinks) and empty subdirectories. Fails when it contains a non-empty real subdirectory. Does not delete recursively.',
                [self::paramP('path', 'query', true, 'string',
                    'Absolute or relative path of the shallow directory to delete. ~/ expands to the server user\'s home directory.')],
                '200 JSON { path, absolute_path, deleted: true }.',
                '400 missing path / not-a-directory / not-shallow, 404 not found, 403 forbidden.'),
            self::routeE('POST', '/api/shell', 'exec_shell',
                'Execute a bash command string (pipes, redirections and all bash syntax allowed) and return the exit code plus separately captured stdout and stderr. WARNING: this is arbitrary remote code execution on the host; the server binds localhost by default and relies on that for protection.',
                [self::paramP('command', 'body (JSON)', true, 'string',
                    'Exact string as typed in bash, e.g. "cat < a.txt | sort > b.txt".'),
                    self::paramP('timeout', 'body (JSON)', false, 'number',
                        'Seconds before the process group is killed (max 300). Timed-out calls return 124 with timed_out=true plus partial output.',
                        'i:30'),
                    self::paramP('cwd', 'body (JSON)', false, 'string',
                        'Working directory for the command (default: server working directory).'),
                    self::paramP('max_stdout', 'body (JSON)', false, 'integer',
                        'Max captured stdout bytes sent back (1 MB default, 100 MB hard cap). The command runs fully; only the response is truncated.',
                        'i:1048576'),
                    self::paramP('max_stderr', 'body (JSON)', false, 'integer',
                        'Max captured stderr bytes sent back (same rules as max_stdout).',
                        'i:1048576'),
                    self::paramP('stdin', 'body (JSON)', false, 'string',
                        'Input piped to the command\'s stdin (as if from a previous pipe stage). Plain string for stdin_encoding=text, base64 for stdin_encoding=base64. Omitted means empty stdin (EOF at once).'),
                    self::paramP('stdin_encoding', 'body (JSON)', false, 'string',
                        'How to interpret the stdin field.',
                        'text', ['text', 'base64'])],
                "200 JSON { command, cwd, returncode, stdout, stderr, timed_out, stdout_truncated, stderr_truncated, max_stdout, max_stderr, stdin_bytes }. returncode is the exit status of the last pipeline element (plain bash semantics). stdout/stderr are what the shell process emitted on its own fds, so in-command redirections apply: '> f' leaves captured stdout empty, '2>&1' merges into captured stdout. stdin_bytes is how many input bytes were fed.",
                '400 missing/empty command / bad JSON / bad timeout / bad cwd / bad or over-cap output limits, 405 wrong method.'),
            self::routeE('POST', '/api/sqlite/create?path=<path>', 'sqlite_create',
                'Create a new SQLite database file. The file is permanent; only the per-request connection is closed afterwards.',
                [self::paramP('path', 'query', true, 'string', 'Full path of the database file to create.')],
                '201 JSON { path, absolute_path, size, sqlite_version }.',
                '400 missing path, 404 parent directory missing, 409 file already exists, 403 forbidden.'),
            self::routeE('POST', '/api/sqlite/query?path=<db-path>', 'sqlite_query',
                'Execute one arbitrary SQL statement on a SQLite database. Stateless: a fresh connection is opened per request and closed right after. SELECT-style statements return rows; anything else is committed and returns the change counters.',
                [self::paramP('path', 'query', true, 'string', 'Path of the database file.'),
                    self::paramP('sql', 'body (JSON)', true, 'string',
                        'Exactly one SQL statement. Multi-statement strings are rejected (send one per request).'),
                    self::paramP('params', 'body (JSON)', false, 'array | object',
                        'Bound parameters: array for ? placeholders, object for :named placeholders. Values must be JSON scalars (BLOB input is not supported).',
                        '[]'),
                    self::paramP('timeout', 'body (JSON)', false, 'number',
                        'Busy timeout in seconds while waiting on a locked database (max 300).', 'i:5'),
                    self::paramP('readonly', 'body (JSON)', false, 'boolean',
                        'Open the database read-only.', 'b:false')],
                '200 JSON, SELECT-style: { path, absolute_path, columns, rows, rowcount } with BLOB values as base64 strings. Writes: { path, absolute_path, rowcount, lastrowid }.',
                '400 missing path/sql, bad JSON/params/timeout, multi-statement, or the SQLite error text itself (e.g. no such table). 404 database file not found, 403 forbidden, 405 wrong method.'),
            self::routeE('GET', '/api/sysinfo', 'system_info',
                'Return operating-system information about the host running the server. Takes no parameters.',
                [],
                '200 JSON { system, system_name, release, version, machine, processor, hostname, python_version, cpu_count, distro, windows, mac }. system is the raw platform name (Linux/Windows/Darwin); system_name is the friendly one (Darwin -> macOS). distro is parsed from /etc/os-release on Linux and null elsewhere; windows/mac objects appear only on those systems.',
                '401 missing/invalid token, 405 wrong method.'),
            self::routeE('POST', '/api/socket/open', 'socket_open',
                'Open a TCP connection to a network peer and hold it as a session, so browser pages (which cannot open raw sockets) can speak arbitrary TCP protocols through the local service (e.g. SMTP/IMAP for a browser email client). With tls:b:true the handshake runs immediately (implicit TLS, e.g. IMAPS on 993), verified against the system roots (+ optional ca_pem) with hostname check and SNI. Returns an opaque session id for send/recv/close/starttls. WARNING: this is an open network proxy on the host; the server binds localhost by default and relies on that for protection.',
                [self::paramP('host', 'body (JSON)', true, 'string',
                    'Peer hostname or IP address to connect to.'),
                    self::paramP('port', 'body (JSON)', true, 'integer',
                        'Peer TCP port, 1-65535 (i:<int>).'),
                    self::paramP('timeout', 'body (JSON)', false, 'number',
                        'Connect timeout in seconds (max 30).', 'i:5'),
                    self::paramP('tls', 'body (JSON)', false, 'boolean',
                        'Handshake TLS immediately and verify the peer (implicit TLS).', 'b:false'),
                    self::paramP('sni', 'body (JSON)', false, 'string',
                        'Server name for SNI and certificate verification (default: host).'),
                    self::paramP('ca_pem', 'body (JSON)', false, 'string',
                        'Extra PEM CA bundle appended to the system roots (corporate intercepts, test PKIs).')],
                '201 JSON { id, kind (tcp|tls), host, port }. The id is an opaque session token for the other socket routes.',
                '400 missing/invalid host/port/timeout/tls/sni/ca_pem, bad JSON, connect or TLS-handshake failure (engine error text), or too many open sessions. 405 wrong method.'),
            self::routeE('POST', '/api/socket/send?id=<id>', 'socket_send',
                'Write bytes to an open socket session (transparent: the peer receives exactly these bytes).',
                [self::paramP('id', 'query', true, 'string',
                    'Session id from socket_open.'),
                    self::paramP('content', 'body (JSON)', false, 'string',
                        'Bytes to write. Plain string for encoding=text, base64 string for encoding=base64. Alternatively send raw bytes with a non-JSON Content-Type.'),
                    self::paramP('encoding', 'body (JSON)', false, 'string',
                        'How to interpret the JSON body content.', 'text', ['text', 'base64'])],
                '200 JSON { id, bytes } with the written byte count.',
                '400 missing id / bad body / socket error, 404 unknown session, 405 wrong method.'),
            self::routeE('GET', '/api/socket/recv?id=<id>[&timeout=][&max_bytes=]', 'socket_recv',
                'Read bytes from an open socket session (transparent: whatever the peer sent). Long-polls up to timeout seconds; returns whatever arrived, an EOF flag when the peer closed, or a timeout flag when nothing arrived in time.',
                [self::paramP('id', 'query', true, 'string',
                    'Session id from socket_open.'),
                    self::paramP('timeout', 'query', false, 'string',
                        'Seconds to wait for data (max 60).', '5'),
                    self::paramP('max_bytes', 'query', false, 'string',
                        'Max bytes per call (1-1048576).', '65536')],
                '200 JSON { id, encoding (base64), content, bytes, eof, timed_out }. content carries base64 bytes (may be empty); eof is true only with empty content after the peer closed; timed_out is true only with empty content when nothing arrived in time.',
                '400 missing id / bad timeout/max_bytes / socket error, 404 unknown session, 405 wrong method.'),
            self::routeE('POST', '/api/socket/close?id=<id>', 'socket_close',
                'Close an open socket session and release it. Idle sessions also expire automatically.',
                [self::paramP('id', 'query', true, 'string',
                    'Session id from socket_open.')],
                '200 JSON { id, closed: true }.',
                '400 missing id, 404 unknown session, 405 wrong method.'),
            self::routeE('POST', '/api/socket/starttls?id=<id>', 'socket_starttls',
                'Upgrade a plaintext socket session to TLS in place (STARTTLS-style protocols: drive the cleartext prelude over the session first, then call this before sending any more application bytes). Verified like implicit TLS (system roots + optional ca_pem, hostname check, SNI). A failed handshake leaves the plaintext session untouched; refused while a WebSocket owns the session, while a recv is pending, or when already TLS.',
                [self::paramP('id', 'query', true, 'string',
                    'Session id from socket_open.'),
                    self::paramP('sni', 'body (JSON)', false, 'string',
                        'Server name for SNI and certificate verification (default: the session host).'),
                    self::paramP('ca_pem', 'body (JSON)', false, 'string',
                        'Extra PEM CA bundle appended to the system roots.')],
                '200 JSON { id, kind (tls) }.',
                '400 missing id / bad body / already TLS / recv pending / attached / handshake failure (engine error text), 404 unknown session, 405 wrong method.'),
            self::routeE('GET', '/api/socket/ws?id=<id>', 'socket_ws',
                'Attach a real WebSocket to an open socket session (Upgrade: websocket; token rides ?token= since browsers cannot set WS headers). After the 101 handshake, WS message payloads flow transparently client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back as WS binary frames; ping gets an auto-pong. A close from either side ends the whole session (a WS close is a session close, like HTTP close); HTTP send/recv on an attached session are refused.',
                [self::paramP('id', 'query', true, 'string',
                    'Session id from socket_open.')],
                '101 Switching Protocols, then WS frames. TCP bytes arrive as binary frames.',
                '400 missing id / not an upgrade / bad version or key / already attached / recv pending, 404 unknown session, 405 wrong method.'),
            self::routeE('GET', '/', 'index',
                'Service index with version and pointer to /api/help.',
                [], 'JSON { service, version, help }.'),
        ];
    }
}
