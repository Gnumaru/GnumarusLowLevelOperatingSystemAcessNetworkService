# frozen_string_literal: true

require "digest/sha1"
require "base64"
require_relative "util"
require_relative "sock"

module Gnumarus
  # WebSocket (RFC 6455) transport for socket sessions
  # (GET /api/socket/ws?id=). Hand-rolled like the Go/Python/PHP ports:
  # after the 101 handshake, WS payloads flow client->TCP (text frames as
  # UTF-8 bytes) and TCP bytes come back as binary frames; ping gets an
  # auto-pong. A close from either side ends the whole session.
  module SockWs
    WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    WS_MAX_MESSAGE = 4 * 1024 * 1024
    WS_WRITE_TIMEOUT = 30.0

    OP_CONT = 0x0
    OP_TEXT = 0x1
    OP_BIN = 0x2
    OP_CLOSE = 0x8
    OP_PING = 0x9
    OP_PONG = 0xA

    class WsIoError < StandardError; end
    class WsProtoError < StandardError; end

    module_function

    def accept_key(key)
      Base64.strict_encode64(Digest::SHA1.digest(key + WS_GUID))
    end

    def has_token?(header_value, token)
      header_value.to_s.split(",").any? { |part| part.strip.casecmp?(token) }
    end

    def utf8_valid?(bytes)
      bytes.dup.force_encoding(Encoding::UTF_8).valid_encoding?
    end

    # Exact-length reader that first drains bytes already buffered by the
    # HTTP layer (a client may pipeline the first frame with the upgrade).
    class Reader
      def initialize(io, initial)
        @io = io
        @buf = (initial || "").b
      end

      def read_exact(count)
        while @buf.bytesize < count
          chunk = @io.readpartial(65_536)
          @buf << chunk.b
        end
        @buf.slice!(0, count)
      rescue EOFError, IOError, SystemCallError
        nil
      end
    end

    class Bridge
      def initialize(io, initial)
        @io = io
        @reader = Reader.new(io, initial)
        @wmu = Mutex.new
        @closed = false
        @state_mu = Mutex.new
        @done = false
      end

      def done?
        @state_mu.synchronize { @done }
      end

      def force_close
        already = @state_mu.synchronize do
          was = @done
          @done = true
          was
        end
        return if already

        begin
          @io.close
        rescue StandardError
          nil
        end
      end

      def frame_bytes(opcode, payload)
        length = payload.bytesize
        header =
          if length < 126
            [0x80 | opcode, length].pack("C*")
          elsif length < 65_536
            [0x80 | opcode, 126].pack("C*") + [length].pack("n")
          else
            [0x80 | opcode, 127].pack("C*") + [length].pack("Q>")
          end
        header + payload.b
      end

      def send_frame(opcode, payload)
        @wmu.synchronize do
          raise WsIoError, "websocket already closed" if @closed

          SockIO.write_all(@io, frame_bytes(opcode, payload), WS_WRITE_TIMEOUT)
        end
        true
      rescue StandardError
        false
      end

      def send_close(code, reason)
        @wmu.synchronize do
          return if @closed

          @closed = true
          payload = [code].pack("n") + reason.to_s.b
          begin
            SockIO.write_all(@io, frame_bytes(OP_CLOSE, payload), 5)
          rescue StandardError
            nil
          end
        end
      end

      # One client frame; raises WsProtoError (after an inline close) or
      # WsIoError. Client frames MUST be masked.
      def read_frame
        hdr = @reader.read_exact(2)
        raise WsIoError, "read error" if hdr.nil?

        b0 = hdr.getbyte(0)
        b1 = hdr.getbyte(1)
        fin = (b0 & 0x80) != 0
        opcode = b0 & 0x0F
        rsv = (b0 & 0x70) != 0
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F
        case length
        when 126
          ext = @reader.read_exact(2)
          raise WsIoError, "read error" if ext.nil?

          length = ext.unpack1("n")
        when 127
          ext = @reader.read_exact(8)
          raise WsIoError, "read error" if ext.nil?

          length = ext.unpack1("Q>")
          return fail_proto(1009) if length > WS_MAX_MESSAGE
        end
        is_ctl = opcode >= 0x08
        return fail_proto(1009) if length > WS_MAX_MESSAGE

        payload =
          if masked
            key = @reader.read_exact(4)
            raise WsIoError, "read error" if key.nil?

            body = @reader.read_exact(length)
            raise WsIoError, "read error" if body.nil?

            unmask(body, key)
          else
            body = @reader.read_exact(length)
            raise WsIoError, "read error" if body.nil?

            body
          end

        return fail_proto(1002) if rsv
        return fail_proto(1002) unless masked
        return fail_proto(1002) if is_ctl && (!fin || length > 125)

        [fin, opcode, payload, is_ctl]
      end

      def unmask(body, key)
        return body if body.empty?

        mask = (key * ((body.bytesize / 4) + 1)).byteslice(0, body.bytesize)
        body.bytes.zip(mask.bytes).map { |a, b| a ^ b }.pack("C*")
      end

      def fail_proto(code)
        send_close(code, "")
        raise WsProtoError, "websocket protocol error"
      end

      # Reassembles one data message across fragments, answering ping
      # inline. Returns {closed:, code:, reason:} on a peer close, else
      # {op:, payload:}.
      def read_message
        msg_op = 0
        msg = +"".b
        started = false
        loop do
          fin, opcode, payload, is_ctl = read_frame
          if is_ctl
            case opcode
            when OP_CLOSE
              if payload.bytesize == 1
                send_close(1002, "")
                raise WsProtoError, "websocket protocol error"
              end
              code = 1000
              reason = ""
              if payload.bytesize >= 2
                code = payload.byteslice(0, 2).unpack1("n")
                reason = payload.byteslice(2, payload.bytesize - 2).to_s
              end
              return { closed: true, code: code, reason: reason }
            when OP_PING
              send_frame(OP_PONG, payload)
              next
            when OP_PONG
              next
            else
              send_close(1002, "")
              raise WsProtoError, "websocket protocol error"
            end
          end

          if opcode == OP_CONT
            if !started
              send_close(1002, "")
              raise WsProtoError, "websocket protocol error"
            end
          elsif opcode == OP_TEXT || opcode == OP_BIN
            if started
              send_close(1002, "")
              raise WsProtoError, "websocket protocol error"
            end
            started = true
            msg_op = opcode
          else
            send_close(1003, "")
            raise WsProtoError, "websocket protocol error"
          end

          if msg.bytesize + payload.bytesize > WS_MAX_MESSAGE
            send_close(1009, "")
            raise WsProtoError, "websocket protocol error"
          end

          msg << payload
          next unless fin

          if msg_op == OP_TEXT && !SockWs.utf8_valid?(msg)
            send_close(1007, "")
            raise WsProtoError, "websocket protocol error"
          end
          return { op: msg_op, payload: msg }
        end
      end

      # Peer bytes -> client binary frames; any TCP end closes the WS.
      def tcp_to_ws(sid, sess)
        loop do
          result = SockIO.read_some(sess.conn, 32_768, 0.25)
          case result[0]
          when :data
            return unless send_frame(OP_BIN, result[1])

            Sock.touch_session(sid, sess)
          when :timeout
            # Nothing arrived: a concurrent HTTP close may have dropped
            # the session without us being woken (IO.select is not
            # interruptible in Ruby), so notice it here.
            unless Sock.live?(sid, sess)
              send_close(1011, "")
              return
            end
          when :eof
            Sock.mark_eof(sid, sess)
            send_close(1000, "")
            return
          else
            send_close(1011, "")
            return
          end
        end
      end

      # Client messages -> peer; ends on close, protocol error, write
      # failure or a dead bridge.
      def ws_to_tcp(sid, sess)
        loop do
          message =
            begin
              read_message
            rescue WsIoError, WsProtoError
              nil
            rescue StandardError
              # Unexpected failure: end the session instead of leaking.
              nil
            end
          if message.nil? || message[:closed]
            Sock.close_session(sid, sess)
            send_close(message[:code], message[:reason]) if message && message[:closed]
            return
          end
          begin
            SockIO.write_all(sess.conn, message[:payload], 30)
          rescue StandardError
            return
          end
          Sock.touch_session(sid, sess)
        end
      end
    end

    # GET /api/socket/ws?id=<sid>. Returns :hijacked when it owns the
    # connection (the dispatcher must not write a response then).
    def ws(conn, req, _opts)
      unless has_token?(req.header("connection"), "upgrade") &&
             req.header("upgrade").to_s.strip.casecmp?("websocket")
        Http.send_err(conn, 400,
                      "This route requires a WebSocket upgrade (Upgrade: websocket).")
        return nil
      end
      unless req.header("sec-websocket-version").to_s.strip == "13"
        Http.send_err(conn, 400, "Unsupported WebSocket version (need 13).")
        return nil
      end
      key = req.header("sec-websocket-key").to_s.strip
      if key.empty?
        Http.send_err(conn, 400, "Missing Sec-WebSocket-Key.")
        return nil
      end

      sid, sess, found = Sock.lookup(conn, req)
      return nil unless found

      case Sock.attach_ws(sess)
      when :attached
        Http.send_err(conn, 400, "Socket session is already attached to a websocket.")
        return nil
      when :recv_pending
        Http.send_err(conn, 400, "Another recv is already pending on this session.")
        return nil
      end

      io = conn.io
      leftover = conn.take_buffer
      response = "HTTP/1.1 101 Switching Protocols\r\n" \
                 "Upgrade: websocket\r\n" \
                 "Connection: Upgrade\r\n" \
                 "Sec-WebSocket-Accept: #{accept_key(key)}\r\n\r\n"
      begin
        io.write(response)
      rescue StandardError
        begin
          io.close
        rescue StandardError
          nil
        end
        Sock.unmark_ws(sid, sess)
        return nil
      end

      bridge = Bridge.new(io, leftover)
      tcp_thread = Thread.new do
        begin
          bridge.tcp_to_ws(sid, sess)
        rescue StandardError
          nil
        ensure
          deadline = SockIO.now + 2
          sleep 0.02 until bridge.done? || SockIO.now >= deadline
          bridge.force_close
        end
      end

      begin
        bridge.ws_to_tcp(sid, sess)
      rescue StandardError
        nil
      ensure
        bridge.force_close
        Sock.close_session(sid, sess)
      end
      tcp_thread.join(3)

      :hijacked
    end
  end
end
