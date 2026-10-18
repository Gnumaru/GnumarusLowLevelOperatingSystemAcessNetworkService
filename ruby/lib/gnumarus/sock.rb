# frozen_string_literal: true

require "socket"
require "openssl"
require "securerandom"
require "ipaddr"
require "base64"
require_relative "tags"
require_relative "util"
require_relative "shell"

module Gnumarus
  # Nonblocking-with-deadline I/O over a plain Socket or an
  # OpenSSL::SSL::SSLSocket (both share read_nonblock/write_nonblock).
  module SockIO
    module_function

    def now
      Process.clock_gettime(Process::CLOCK_MONOTONIC)
    end

    def wait_io(io, mode, deadline)
      if deadline.nil?
        mode == :read ? IO.select([io]) : IO.select(nil, [io])
      else
        remaining = deadline - now
        return false if remaining <= 0

        mode == :read ? IO.select([io], nil, nil, remaining) : IO.select(nil, [io], nil, remaining)
      end
    end

    # Returns [:data, str] | [:eof] | [:timeout] | [:error, msg].
    def read_some(io, max, timeout)
      deadline = timeout.nil? ? nil : now + timeout
      loop do
        result = io.read_nonblock(max, exception: false)
        case result
        when :wait_readable
          return [:timeout] unless wait_io(io, :read, deadline)
        when :wait_writable
          return [:timeout] unless wait_io(io, :write, deadline)
        when nil
          return [:eof]
        else
          return [:data, result]
        end
      end
    rescue EOFError
      [:eof]
    rescue OpenSSL::SSL::SSLError => e
      return [:eof] if e.message.include?("unexpected eof")

      [:error, e.message]
    rescue SystemCallError, IOError => e
      [:error, e.message]
    end

    # Writes every byte or raises IOError on error/timeout.
    def write_all(io, data, timeout)
      total = data.bytesize
      return 0 if total.zero?

      deadline = timeout.nil? ? nil : now + timeout
      offset = 0
      while offset < total
        chunk = data.byteslice(offset, total - offset)
        result = io.write_nonblock(chunk, exception: false)
        case result
        when :wait_writable
          raise IOError, "write timeout" unless wait_io(io, :write, deadline)
        when :wait_readable
          raise IOError, "write timeout" unless wait_io(io, :read, deadline)
        when nil
          raise IOError, "connection closed during write"
        else
          offset += result
        end
      end
      offset
    end
  end

  # One held TCP/TLS session.
  class SockSession
    attr_accessor :conn, :host, :port, :kind, :server_name, :last, :eof, :ws_attached
    attr_reader :mu

    def initialize(conn:, host:, port:, kind:, server_name:, last:)
      @conn = conn
      @host = host
      @port = port
      @kind = kind
      @server_name = server_name
      @last = last
      @eof = false
      @ws_attached = false
      @mu = Mutex.new
    end
  end

  # The transparent TCP socket proxy (/api/socket/*): open/send/recv/close
  # plus in-place TLS (open with tls:b:true, or /api/socket/starttls).
  module Sock
    DEFAULT_CONNECT_TIMEOUT = 5.0
    MAX_CONNECT_TIMEOUT = 30.0
    DEFAULT_RECV_TIMEOUT = 5.0
    MAX_RECV_TIMEOUT = 60.0
    DEFAULT_MAX_BYTES = 65_536
    MAX_BYTES = 1024 * 1024
    MAX_SESSIONS = 128
    IDLE_TIMEOUT = 300.0
    WRITE_TIMEOUT = 30.0

    B64_STRICT = %r{\A[A-Za-z0-9+/]*={0,2}\z}

    @sessions = {}
    @mutex = Mutex.new
    @sweeper = false

    module_function

    # -- global bookkeeping ----------------------------------------------

    def ensure_sweeper
      return if @sweeper

      @sweeper = true
      Thread.new do
        loop do
          sleep 60
          sweep_expired
        end
      end
    end

    def sweep_expired
      now = Time.now
      @mutex.synchronize do
        @sessions.each do |sid, sess|
          next if sess.ws_attached
          next unless now - sess.last > IDLE_TIMEOUT

          @sessions.delete(sid)
          begin
            sess.conn.close
          rescue StandardError
            nil
          end
        end
      end
    end

    def session_count
      @mutex.synchronize { @sessions.length }
    end

    def mint_id
      SecureRandom.urlsafe_base64(16)
    end

    def lookup(conn, req)
      sid = Util.first_query(req.query, "id")
      if sid.empty?
        Http.send_err(conn, 400, "Missing required query parameter: id")
        return [nil, nil, false]
      end
      sess = nil
      @mutex.synchronize do
        sess = @sessions[sid]
        sess.last = Time.now if sess
      end
      if sess.nil?
        Http.send_err(conn, 404, "Unknown socket session: #{sid}")
        return [nil, nil, false]
      end
      [sid, sess, true]
    end

    def attached?(sess)
      @mutex.synchronize { sess.ws_attached }
    end

    # Whether the session is still in the map and its transport open.
    # (Ruby's IO.select is not woken by another thread closing an IO,
    # so the WS tcp->client pump polls this between short reads.)
    def live?(sid, sess)
      return false unless @mutex.synchronize { @sessions[sid].equal?(sess) }

      io = sess.conn
      return true unless io.respond_to?(:closed?)

      !io.closed?
    rescue StandardError
      false
    end

    def touch(sid)
      @mutex.synchronize do
        @sessions[sid].last = Time.now if @sessions.key?(sid)
      end
    end

    # -- WebSocket ownership (used by sock_ws) ---------------------------

    # Returns :ok | :attached | :recv_pending.
    def attach_ws(sess)
      @mutex.synchronize do
        return :attached if sess.ws_attached
        return :recv_pending unless sess.mu.try_lock

        sess.mu.unlock
        sess.ws_attached = true
        sess.last = Time.now
        :ok
      end
    end

    def unmark_ws(sid, sess)
      @mutex.synchronize do
        if @sessions[sid].equal?(sess)
          sess.ws_attached = false
          sess.last = Time.now
        end
      end
    end

    def touch_session(sid, sess)
      @mutex.synchronize do
        sess.last = Time.now if @sessions[sid].equal?(sess)
      end
    end

    def mark_eof(sid, sess)
      @mutex.synchronize do
        if @sessions[sid].equal?(sess)
          sess.eof = true
          sess.last = Time.now
        end
      end
    end

    # A WebSocket close is a session close: end it from whichever side
    # hung up first. Idempotent (HTTP close may win the race).
    def close_session(sid, sess)
      @mutex.synchronize do
        if @sessions[sid].equal?(sess)
          sess.ws_attached = false
          @sessions.delete(sid)
        end
      end
      begin
        sess.conn.close
      rescue StandardError
        nil
      end
    end

    # -- TLS -------------------------------------------------------------

    def system_store
      store = OpenSSL::X509::Store.new
      begin
        store.set_default_paths
      rescue StandardError
        nil
      end
      store
    end

    # System roots + an extra PEM bundle; nil when the PEM has no certs.
    def cert_store(ca_pem)
      store = system_store
      count = 0
      ca_pem.scan(/-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----/m).each do |pem|
        begin
          store.add_cert(OpenSSL::X509::Certificate.new(pem))
          count += 1
        rescue StandardError
          nil
        end
      end
      return nil if count.zero?

      store
    end

    def ip_literal?(name)
      IPAddr.new(name)
      true
    rescue IPAddr::InvalidAddressError, StandardError
      false
    end

    def normalize_ip(name)
      IPAddr.new(name).to_s
    rescue StandardError
      name
    end

    # RFC 6125 hostname check that also matches IP SANs (Ruby's built-in
    # verify_hostname only matches DNS names).
    def verify_hostname!(cert, name)
      dns, ips = san_entries(cert)
      if ip_literal?(name)
        target = normalize_ip(name)
        return if ips.any? { |ip| normalize_ip(ip) == target }

        raise OpenSSL::SSL::SSLError, "certificate is not valid for #{name}"
      end
      if dns.empty?
        cn = common_name(cert)
        return if cn && dns_match?(cn, name)

        raise OpenSSL::SSL::SSLError, "certificate is not valid for #{name}"
      end
      return if dns.any? { |pattern| dns_match?(pattern, name) }

      raise OpenSSL::SSL::SSLError, "certificate is not valid for #{name}"
    end

    def san_entries(cert)
      dns = []
      ips = []
      ext = cert.extensions.find { |e| e.oid == "subjectAltName" }
      return [dns, ips] if ext.nil?

      ext.value.split(",").each do |raw|
        entry = raw.strip
        if entry.start_with?("DNS:")
          dns << entry[4..].to_s
        elsif entry.start_with?("IP Address:")
          ips << entry[11..].to_s
        end
      end
      [dns, ips]
    end

    def common_name(cert)
      n = cert.subject.to_a.find { |part| part[0] == "CN" }
      n && n[1]
    end

    def dns_match?(pattern, host)
      pat = pattern.downcase.sub(/\.\z/, "")
      h = host.downcase.sub(/\.\z/, "")
      return false if pat.empty? || h.empty?
      return true if pat == h

      pparts = pat.split(".", -1)
      hparts = h.split(".", -1)
      return false unless pparts.length == hparts.length

      pparts.each_with_index do |part, index|
        next if index.zero? && part == "*"
        return false unless part == hparts[index]
      end
      !pparts.include?("*")
    end

    # Runs a client handshake over an established TCP socket. Raises on
    # failure and leaves the raw socket untouched (sync_close stays off).
    def tls_wrap(raw, server_name, store, timeout)
      ctx = OpenSSL::SSL::SSLContext.new
      ctx.verify_mode = OpenSSL::SSL::VERIFY_PEER
      ctx.verify_hostname = false if ctx.respond_to?(:verify_hostname=)
      ctx.cert_store = store if store
      ctx.min_version = OpenSSL::SSL::TLS1_2_VERSION if ctx.respond_to?(:min_version=)

      ssl = OpenSSL::SSL::SSLSocket.new(raw, ctx)
      ssl.sync_close = false
      ssl.hostname = server_name unless ip_literal?(server_name)

      deadline = SockIO.now + timeout
      loop do
        case ssl.connect_nonblock(exception: false)
        when :wait_readable
          raise OpenSSL::SSL::SSLError, "handshake timed out" unless SockIO.wait_io(raw, :read, deadline)
        when :wait_writable
          raise OpenSSL::SSL::SSLError, "handshake timed out" unless SockIO.wait_io(raw, :write, deadline)
        else
          break
        end
      end

      verify_hostname!(ssl.peer_cert, server_name)
      ssl.sync_close = true
      ssl
    end

    # -- body helpers ----------------------------------------------------

    def parse_json_body(conn, req)
      obj, errmsg = Util.json_parse_object(req.body)
      if errmsg
        Http.send_err(conn, 400, errmsg)
        return [nil, false]
      end
      [obj, true]
    end

    def parse_tls_identity(obj)
      sni = ""
      if obj.key?("sni") && !obj["sni"].nil?
        value = obj["sni"]
        unless value.is_a?(String) && !value.strip.empty?
          return ["", "", "Field 'sni' must be a non-empty string."]
        end

        sni = value.strip
      end
      ca_pem = ""
      if obj.key?("ca_pem") && !obj["ca_pem"].nil?
        value = obj["ca_pem"]
        unless value.is_a?(String) && !value.strip.empty?
          return ["", "", "Field 'ca_pem' must be a PEM CA bundle string."]
        end

        ca_pem = value
      end
      [sni, ca_pem, ""]
    end

    # -- POST /api/socket/open -------------------------------------------

    def open(conn, req, _opts)
      ensure_sweeper
      unless req.header("content-type").to_s.include?("application/json")
        Http.send_err(conn, 400,
                      "Content-Type must be application/json with body { host, port, timeout? }.")
        return
      end
      obj, ok = parse_json_body(conn, req)
      return unless ok

      host = obj["host"].is_a?(String) ? obj["host"] : ""
      if host.strip.empty?
        Http.send_err(conn, 400, "Missing or empty required field: host.")
        return
      end

      if !obj.key?("port") || obj["port"].nil?
        Http.send_err(conn, 400, "Missing required field: port (i:<1-65535>).")
        return
      end
      pv = obj["port"]
      bad_port = "Field 'port' must be an integer 1-65535 (i:<int>)."
      unless pv.is_a?(String) && pv.start_with?("i:")
        Http.send_err(conn, 400, bad_port)
        return
      end
      begin
        decoded = Tags.jdec(pv)
      rescue Tags::BadTag
        Http.send_err(conn, 400, bad_port)
        return
      end
      port, ok = Shell.to_int(decoded)
      if !ok || port < 1 || port > 65_535
        Http.send_err(conn, 400, bad_port)
        return
      end

      timeout = DEFAULT_CONNECT_TIMEOUT
      if obj.key?("timeout") && !obj["timeout"].nil?
        value = obj["timeout"]
        bad = "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."
        unless value.is_a?(String)
          Http.send_err(conn, 400, bad)
          return
        end
        begin
          decoded = Tags.jdec(value)
        rescue Tags::BadTag
          Http.send_err(conn, 400, bad)
          return
        end
        seconds, ok = Shell.to_float(decoded)
        unless ok
          Http.send_err(conn, 400, bad)
          return
        end
        timeout = seconds
      end
      unless timeout > 0 && timeout <= MAX_CONNECT_TIMEOUT
        Http.send_err(conn, 400,
                      "Field 'timeout' must be within (0, #{MAX_CONNECT_TIMEOUT.to_i}].")
        return
      end

      tls_mode = false
      if obj.key?("tls") && !obj["tls"].nil?
        value = obj["tls"]
        bad = "Field 'tls' must be b:true or b:false."
        unless value.is_a?(String)
          Http.send_err(conn, 400, bad)
          return
        end
        begin
          decoded = Tags.jdec(value)
        rescue Tags::BadTag
          Http.send_err(conn, 400, bad)
          return
        end
        unless decoded == true || decoded == false
          Http.send_err(conn, 400, bad)
          return
        end
        tls_mode = decoded
      end

      sni, ca_pem, id_err = parse_tls_identity(obj)
      unless id_err.empty?
        Http.send_err(conn, 400, id_err)
        return
      end
      sni = host if sni.empty?

      store = nil
      unless ca_pem.empty?
        store = cert_store(ca_pem)
        if store.nil?
          Http.send_err(conn, 400, "Field 'ca_pem' must be a valid PEM CA bundle.")
          return
        end
      end

      sweep_expired
      if session_count >= MAX_SESSIONS
        Http.send_err(conn, 400, "Too many open socket sessions (#{MAX_SESSIONS}).")
        return
      end

      raw =
        begin
          Socket.tcp(host, port, connect_timeout: timeout)
        rescue StandardError => e
          Http.send_err(conn, 400, "Cannot connect to #{host}:#{port}: #{e}")
          return
        end

      kind = "tcp"
      conn_io = raw
      if tls_mode
        begin
          conn_io = tls_wrap(raw, sni, store, timeout)
          kind = "tls"
        rescue StandardError => e
          begin
            raw.close
          rescue StandardError
            nil
          end
          Http.send_err(conn, 400, "TLS handshake with #{host}:#{port} failed: #{e.message}")
          return
        end
      end

      sid = mint_id
      @mutex.synchronize do
        @sessions[sid] = SockSession.new(conn: conn_io, host: host, port: port,
                                         kind: kind, server_name: sni, last: Time.now)
      end
      Http.send_json(conn, 201,
                     "id" => sid, "kind" => kind, "host" => host,
                     "port" => Tags.jenc(port))
    end

    # -- POST /api/socket/send -------------------------------------------

    def send(conn, req, _opts)
      sid, sess, ok = lookup(conn, req)
      return unless ok

      if attached?(sess)
        Http.send_err(conn, 400,
                      "Socket session is attached to a websocket (HTTP send refused while attached).")
        return
      end

      data, ok = send_bytes(conn, req)
      return unless ok

      begin
        SockIO.write_all(sess.conn, data, WRITE_TIMEOUT)
      rescue StandardError => e
        Http.send_err(conn, 400, "Socket error: #{e.message}")
        return
      end
      touch(sid)
      Http.send_json(conn, 200, "id" => sid, "bytes" => Tags.jenc(data.bytesize))
    end

    def send_bytes(conn, req)
      ct = req.header("content-type").to_s
      return [req.body, true] unless ct.include?("application/json")

      obj, errmsg = Util.json_parse_object(req.body)
      if errmsg
        Http.send_err(conn, 400, errmsg)
        return [nil, false]
      end

      content = ""
      if obj.key?("content") && !obj["content"].nil?
        value = obj["content"]
        unless value.is_a?(String)
          Http.send_err(conn, 400, "'content' must be a string.")
          return [nil, false]
        end

        content = value
      end

      encoding = "text"
      if obj.key?("encoding") && !obj["encoding"].nil?
        value = obj["encoding"]
        unless value.is_a?(String)
          Http.send_err(conn, 400, "Invalid encoding: (use text|base64).")
          return [nil, false]
        end

        encoding = value.downcase
      end

      case encoding
      when "text"
        [content.b, true]
      when "base64"
        unless (content.length % 4).zero? && B64_STRICT.match?(content)
          Http.send_err(conn, 400, "Invalid base64 'content'.")
          return [nil, false]
        end
        begin
          [Base64.strict_decode64(content), true]
        rescue ArgumentError
          Http.send_err(conn, 400, "Invalid base64 'content'.")
          [nil, false]
        end
      else
        Http.send_err(conn, 400, "Invalid encoding: '#{encoding}' (use text|base64).")
        [nil, false]
      end
    end

    # -- GET /api/socket/recv --------------------------------------------

    def recv(conn, req, _opts)
      timeout = DEFAULT_RECV_TIMEOUT
      raw_timeout = Util.first_query(req.query, "timeout")
      unless raw_timeout.empty?
        value = begin
          Float(raw_timeout.strip)
        rescue ArgumentError, TypeError
          nil
        end
        if value.nil? || !(value > 0 && value <= MAX_RECV_TIMEOUT)
          Http.send_err(conn, 400,
                        "Invalid timeout param (seconds within (0, #{MAX_RECV_TIMEOUT.to_i}]).")
          return
        end
        timeout = value
      end

      max_bytes = DEFAULT_MAX_BYTES
      raw_max = Util.first_query(req.query, "max_bytes")
      unless raw_max.empty?
        value = raw_max.strip.match?(/\A[+-]?\d+\z/) ? Integer(raw_max.strip, 10) : nil
        if value.nil? || value < 1 || value > MAX_BYTES
          Http.send_err(conn, 400, "Invalid max_bytes param (1-#{MAX_BYTES}).")
          return
        end
        max_bytes = value
      end

      sid, sess, ok = lookup(conn, req)
      return unless ok

      if attached?(sess)
        Http.send_err(conn, 400,
                      "Socket session is attached to a websocket (HTTP recv refused while attached).")
        return
      end

      unless sess.mu.try_lock
        Http.send_err(conn, 400, "Another recv is already pending on this session.")
        return
      end
      begin
        if sess.eof
          empty_recv(conn, sid, eof: true, timed_out: false)
          return
        end

        result = SockIO.read_some(sess.conn, max_bytes, timeout)
        touch(sid)
        case result[0]
        when :timeout
          empty_recv(conn, sid, eof: false, timed_out: true)
        when :eof
          @mutex.synchronize { @sessions[sid].eof = true if @sessions.key?(sid) }
          empty_recv(conn, sid, eof: true, timed_out: false)
        when :error
          Http.send_err(conn, 400, "Socket error: #{result[1]}")
        else
          bytes = result[1]
          Http.send_json(conn, 200,
                         "id" => sid, "encoding" => "base64",
                         "content" => Base64.strict_encode64(bytes),
                         "bytes" => Tags.jenc(bytes.bytesize),
                         "eof" => "b:false", "timed_out" => "b:false")
        end
      ensure
        sess.mu.unlock
      end
    end

    def empty_recv(conn, sid, eof:, timed_out:)
      Http.send_json(conn, 200,
                     "id" => sid, "encoding" => "base64", "content" => "",
                     "bytes" => "i:0",
                     "eof" => Tags.jenc(eof), "timed_out" => Tags.jenc(timed_out))
    end

    # -- POST /api/socket/close ------------------------------------------

    def close(conn, req, _opts)
      sid, sess, ok = lookup(conn, req)
      return unless ok

      @mutex.synchronize { @sessions.delete(sid) }
      begin
        sess.conn.close
      rescue StandardError
        nil
      end
      Http.send_json(conn, 200, "id" => sid, "closed" => "b:true")
    end

    # -- POST /api/socket/starttls ---------------------------------------

    def starttls(conn, req, _opts)
      sid, sess, ok = lookup(conn, req)
      return unless ok

      attached = attached?(sess)
      already_tls = sess.kind == "tls"
      host = sess.host
      port = sess.port

      if attached
        Http.send_err(conn, 400,
                      "Socket session is attached to a websocket (STARTTLS refused while attached).")
        return
      end
      if already_tls
        Http.send_err(conn, 400, "Socket session is already TLS.")
        return
      end

      unless sess.mu.try_lock
        Http.send_err(conn, 400, "Another recv is already pending on this session.")
        return
      end

      begin
        server_name = host
        store = nil
        body = req.body.to_s
        unless body.strip.empty?
          unless req.header("content-type").to_s.include?("application/json")
            Http.send_err(conn, 400,
                          "Content-Type must be application/json with body { sni?, ca_pem? }.")
            return
          end
          obj, errmsg = Util.json_parse_object(body)
          if errmsg
            Http.send_err(conn, 400, errmsg)
            return
          end
          sni, ca_pem, id_err = parse_tls_identity(obj)
          unless id_err.empty?
            Http.send_err(conn, 400, id_err)
            return
          end
          server_name = sni unless sni.empty?
          unless ca_pem.empty?
            store = cert_store(ca_pem)
            if store.nil?
              Http.send_err(conn, 400, "Field 'ca_pem' must be a valid PEM CA bundle.")
              return
            end
          end
        end

        begin
          ssl = tls_wrap(sess.conn, server_name, store, MAX_CONNECT_TIMEOUT)
        rescue StandardError => e
          Http.send_err(conn, 400, "TLS handshake with #{host}:#{port} failed: #{e.message}")
          return
        end

        @mutex.synchronize do
          current = @sessions[sid]
          if current.equal?(sess)
            sess.conn = ssl
            sess.kind = "tls"
            sess.server_name = server_name
            sess.last = Time.now
          end
        end
        Http.send_json(conn, 200, "id" => sid, "kind" => "tls")
      ensure
        sess.mu.unlock
      end
    end
  end
end
