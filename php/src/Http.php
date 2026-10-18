<?php
// Request/response value objects for the ReactPHP port.
//
// The service speaks plain HTTP/1.1 on a manually driven server
// (see HttpServer.php): handlers take a GRequest and return a
// GResponse (or a Promise of one for async operations like shell
// and socket I/O), instead of touching SAPI globals.

class GRequest {
    public string $method;
    public string $path;
    /** Query params: name => raw string (first wins). */
    public array $query;
    /** Headers: lowercase name => value (first wins). */
    public array $headers;
    public string $body;

    public function __construct(string $method, string $path,
            array $query, array $headers, string $body) {
        $this->method = $method;
        $this->path = $path;
        $this->query = $query;
        $this->headers = $headers;
        $this->body = $body;
    }

    public function header(string $name): string {
        $k = strtolower($name);
        return $this->headers[$k] ?? '';
    }
}

class GResponse {
    public int $status;
    /** List of [name, value] pairs (duplicates allowed). */
    public array $headers;
    public string $body;

    public function __construct(int $status, array $headers, string $body) {
        $this->status = $status;
        $this->headers = $headers;
        $this->body = $body;
    }

    /** Standard reason phrases (enough for the codes we emit). */
    public static function reason(int $status): string {
        return match ($status) {
            200 => 'OK',
            201 => 'Created',
            204 => 'No Content',
            400 => 'Bad Request',
            401 => 'Unauthorized',
            403 => 'Forbidden',
            404 => 'Not Found',
            405 => 'Method Not Allowed',
            409 => 'Conflict',
            500 => 'Internal Server Error',
            503 => 'Service Unavailable',
            default => 'Unknown',
        };
    }

    /** @return list<array{0:string,1:string}> */
    public static function corsHeaders(): array {
        return [
            ['Access-Control-Allow-Origin', '*'],
            ['Access-Control-Allow-Methods',
                'GET, POST, PUT, DELETE, PATCH, OPTIONS'],
            ['Access-Control-Allow-Headers',
                'Content-Type, Authorization, X-Requested-With'],
            ['Access-Control-Max-Age', '86400'],
            // One request per connection everywhere (except an
            // upgraded WebSocket, whose 101 is built by hand).
            ['Connection', 'close'],
        ];
    }

    public static function json(int $status, $data): self {
        $body = json_encode($data,
            JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        if ($body === false) {
            $body = '{"error":"error","message":"JSON encoding failed"}';
        }
        $headers = self::corsHeaders();
        $headers[] = ['Content-Type', 'application/json; charset=utf-8'];
        $headers[] = ['Content-Length', (string) strlen($body)];
        return new self($status, $headers, $body);
    }

    public static function err(int $status, string $message,
            ?string $path = null): self {
        $obj = ['error' => self::label($status), 'message' => $message];
        if ($path !== null) $obj['path'] = $path;
        return self::json($status, $obj);
    }

    public static function raw(int $status, string $body,
            string $contentType): self {
        $headers = self::corsHeaders();
        $headers[] = ['Content-Type', $contentType];
        $headers[] = ['Content-Length', (string) strlen($body)];
        return new self($status, $headers, $body);
    }

    /** Empty response with CORS headers (204 preflight, 101 n/a). */
    public static function empty(int $status): self {
        return new self($status, self::corsHeaders(), '');
    }

    public static function unauthorized(): self {
        $r = self::json(401, ['error' => 'unauthorized',
            'message' => 'Valid API token required: send ' .
                'Authorization: Bearer <token> or ?token=.']);
        $r->headers[] = ['WWW-Authenticate', 'Bearer'];
        return $r;
    }

    public static function label(int $status): string {
        return match ($status) {
            400 => 'bad_request',
            401 => 'unauthorized',
            403 => 'forbidden',
            404 => 'not_found',
            405 => 'method_not_allowed',
            409 => 'conflict',
            default => 'error',
        };
    }

    /** Serialize to a full HTTP/1.1 response head + body. */
    public function toBytes(): string {
        $out = 'HTTP/1.1 ' . $this->status . ' ' .
            self::reason($this->status) . "\r\n";
        foreach ($this->headers as [$name, $value]) {
            $out .= $name . ': ' . $value . "\r\n";
        }
        return $out . "\r\n" . $this->body;
    }
}
