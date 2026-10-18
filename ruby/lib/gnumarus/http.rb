# frozen_string_literal: true

require_relative "util"

module Gnumarus
  # Buffered reader/writer over one accepted client TCP connection.
  class Conn
    CHUNK = 65_536
    HEAD_LIMIT = 1 << 20

    def initialize(io)
      @io = io
      @buf = String.new(encoding: Encoding::BINARY)
    end

    attr_reader :io

    def buffered?
      !@buf.empty?
    end

    # Pulls one more chunk into the buffer; false on EOF.
    def fill
      chunk = @io.readpartial(CHUNK)
      @buf << chunk.b
      true
    rescue EOFError, IOError, SystemCallError
      false
    end

    # Reads through the next blank line ("\r\n\r\n") and returns the
    # head block (nil on EOF before any data).
    def read_head
      loop do
        idx = @buf.index("\r\n\r\n".b)
        return @buf.slice!(0, idx + 4) if idx
        return nil if @buf.bytesize > HEAD_LIMIT

        return nil unless fill
      end
    end

    # Reads exactly n bytes (drains any buffered bytes first). Returns
    # nil when the peer closes early.
    def read_exact(n)
      while @buf.bytesize < n
        return nil unless fill
      end
      @buf.slice!(0, n)
    end

    # Returns up to max buffered/available bytes, or nil on EOF.
    def read_some(max)
      return nil if @buf.empty? && !fill

      n = [max, @buf.bytesize].min
      @buf.slice!(0, n)
    end

    def write(data)
      @io.write(data)
      true
    rescue SystemCallError, IOError
      false
    end

    # Hands over any bytes already read past the request (WebSocket
    # hijack: a client may pipeline its first frame with the upgrade).
    def take_buffer
      out = @buf
      @buf = String.new(encoding: Encoding::BINARY)
      out
    end

    def close
      @io.close
    rescue StandardError
      nil
    end
  end

  # Parsed HTTP request.
  class Request
    attr_reader :method, :target, :path, :query, :headers, :body, :version

    def initialize(method:, target:, version:, headers:, body:)
      @method = method
      @target = target
      @version = version
      @headers = headers
      @body = body
      path, _sep, query_string = target.partition("?")
      @path = path
      @query = Util.parse_query(query_string)
    end

    def header(name)
      @headers[name.downcase]
    end

    def self.parse(conn)
      head = conn.read_head
      return nil if head.nil?

      lines = head.split("\r\n", -1)
      request_line = lines.shift.to_s
      return nil if request_line.strip.empty?

      parts = request_line.split(" ")
      method = parts[0].to_s
      target = parts[1].to_s
      version = parts[2].to_s
      version = "HTTP/1.1" if version.empty?

      headers = {}
      lines.each do |line|
        next if line.empty?

        key, _sep, value = line.partition(":")
        key = key.strip.downcase
        value = value.strip
        headers[key] = headers.key?(key) ? "#{headers[key]}, #{value}" : value
      end

      body = read_body(conn, headers)
      new(method: method, target: target, version: version,
          headers: headers, body: body)
    end

    def self.read_body(conn, headers)
      length = headers["content-length"]
      if length && length.match?(/\A\d+\z/)
        n = length.to_i
        return String.new(encoding: Encoding::BINARY) if n.zero?

        body = conn.read_exact(n)
        return body || String.new(encoding: Encoding::BINARY)
      end
      String.new(encoding: Encoding::BINARY)
    end
  end

  # HTTP response writer.
  module Http
    CORS = {
      "Access-Control-Allow-Origin" => "*",
      "Access-Control-Allow-Methods" => "GET, POST, PUT, DELETE, PATCH, OPTIONS",
      "Access-Control-Allow-Headers" => "Content-Type, Authorization, X-Requested-With",
      "Access-Control-Max-Age" => "86400"
    }.freeze

    REASONS = {
      101 => "Switching Protocols", 200 => "OK", 201 => "Created",
      204 => "No Content", 400 => "Bad Request", 401 => "Unauthorized",
      403 => "Forbidden", 404 => "Not Found", 405 => "Method Not Allowed",
      409 => "Conflict", 500 => "Internal Server Error",
      503 => "Service Unavailable"
    }.freeze

    module_function

    def reason(status)
      REASONS[status] || "Error"
    end

    def label(status)
      case status
      when 400 then "bad_request"
      when 401 then "unauthorized"
      when 403 then "forbidden"
      when 404 then "not_found"
      when 405 then "method_not_allowed"
      when 409 then "conflict"
      else "error"
      end
    end

    def send_response(conn, status, headers, body)
      body = body.to_s.b
      hdrs = CORS.merge("Connection" => "close").merge(headers)
      hdrs["Content-Length"] = body.bytesize.to_s
      head = +"HTTP/1.1 #{status} #{reason(status)}\r\n".b
      hdrs.each { |k, v| head << "#{k}: #{v}\r\n" }
      head << "\r\n"
      conn.write(head + body)
    end

    def send_json(conn, status, obj, extra = {})
      body = Util.json_generate(obj)
      send_response(conn, status, extra.merge("Content-Type" => "application/json; charset=utf-8"), body)
    end

    def send_raw(conn, status, body, content_type)
      send_response(conn, status, { "Content-Type" => content_type }, body)
    end

    def send_err(conn, status, message, path = "")
      obj = { "error" => label(status), "message" => message }
      obj["path"] = path unless path.nil? || path.empty?
      send_json(conn, status, obj)
    end

    def send_unauthorized(conn)
      send_response(conn, 401,
                    { "Content-Type" => "application/json; charset=utf-8",
                      "WWW-Authenticate" => "Bearer" },
                    '{"error":"unauthorized","message":"Valid API token required: send Authorization: Bearer <token> or ?token=."}')
    end

    def send_host_forbidden(conn)
      send_response(conn, 403,
                    { "Content-Type" => "application/json; charset=utf-8" },
                    '{"error":"forbidden","message":"Host not allowed (use 127.0.0.1, localhost or ::1)."}')
    end
  end
end
