# frozen_string_literal: true

require_relative "tags"
require_relative "util"
require_relative "http"
require_relative "routes"
require_relative "fsops"
require_relative "shell"
require_relative "sysinfo"
require_relative "sqlite"
require_relative "sock"
require_relative "sock_ws"
require_relative "oauth2"

module Gnumarus
  # Per-server state (auth + working directory), mirroring the Go Opts.
  Opts = Struct.new(:auth_required, :auth_token, :cwd)

  # Method + path dispatch, Host/auth/CORS gates and the small routes
  # (help, index). The route catalog lives in routes.rb (byte-identical
  # to the Go reference); the heavy lifting lives in the feature modules.
  module Api
    VERSION = "0.10.0"
    SERVICE_NAME = "GnumarusPythonNativeOperationsHttpService"

    OPEN_ROUTES = { "/" => true, "/api/help" => true, "/api/oauth2/callback" => true }.freeze

    GET_POST_ONLY = %w[
      /api/shell /api/sqlite/create /api/sqlite/query
      /api/socket/open /api/socket/send /api/socket/close
      /api/socket/starttls
    ].freeze

    ALWAYS_KNOWN = %w[
      / /api/help /api/dir /api/dir/shallow /api/shallow_dir /api/dir_shallow
      /api/shell /api/sqlite/create /api/sqlite/query
      /api/sysinfo /api/socket/open /api/socket/send /api/socket/recv
      /api/socket/close /api/socket/starttls /api/socket/ws
      /api/oauth2/start /api/oauth2/callback /api/oauth2/poll
      /api/oauth2/refresh /api/oauth2/close
    ].freeze

    module_function

    # One connection = one request (every response is Connection: close).
    def handle_conn(conn, opts)
      req = Request.parse(conn)
      return if req.nil?

      handle(conn, req, opts)
    rescue StandardError => e
      begin
        Http.send_err(conn, 500, "Internal error: #{e}")
      rescue StandardError
        nil
      end
    ensure
      conn.close
    end

    def handle(conn, req, opts)
      unless Util.host_allowed?(req.header("host"))
        Http.send_host_forbidden(conn)
        return
      end
      if req.method == "OPTIONS"
        Http.send_response(conn, 204, {}, "")
        return
      end
      unless auth_ok?(req, opts)
        Http.send_unauthorized(conn)
        return
      end

      case req.method
      when "GET" then do_get(conn, req, opts)
      when "POST" then do_post(conn, req, opts)
      when "PUT" then do_put(conn, req, opts)
      when "DELETE" then do_delete(conn, req, opts)
      else
        Http.send_err(conn, 405, "Method #{req.method} not supported.")
      end
    end

    def auth_ok?(req, opts)
      return true unless opts.auth_required
      return true if req.method == "GET" && OPEN_ROUTES[req.path]

      supplied = ""
      auth = req.header("authorization").to_s
      supplied = auth[7..].to_s.strip if auth.start_with?("Bearer ")
      supplied = Util.first_query(req.query, "token") if supplied.empty?

      token = opts.auth_token.to_s
      return false if token.empty?

      secure_compare(supplied, token)
    end

    def secure_compare(supplied, token)
      return false unless supplied.bytesize == token.bytesize

      diff = 0
      supplied.bytes.each_with_index { |byte, i| diff |= byte ^ token.getbyte(i) }
      diff.zero?
    end

    def do_get(conn, req, opts)
      case req.path
      when "/api/help" then help(conn, opts)
      when "/api/dir" then FsOps.read_dir(conn, req, opts)
      when "/api/file" then FsOps.read_file(conn, req, opts)
      when "/" then index(conn)
      when "/api/socket/recv" then Sock.recv(conn, req, opts)
      when "/api/socket/ws" then SockWs.ws(conn, req, opts)
      when "/api/sysinfo" then Sysinfo.sysinfo(conn, req, opts)
      when "/api/oauth2/callback" then OAuth2.callback(conn, req, opts)
      when "/api/oauth2/poll" then OAuth2.poll(conn, req, opts)
      when "/api/oauth2/start", "/api/oauth2/refresh", "/api/oauth2/close"
        Http.send_err(conn, 405, "Method GET not supported for this route.")
      else
        if GET_POST_ONLY.include?(req.path)
          Http.send_err(conn, 405,
                        "Method GET not supported for this route (use POST).")
        else
          Http.send_err(conn, 404, "Unknown route: #{req.path}")
        end
      end
    end

    def do_post(conn, req, opts)
      case req.path
      when "/api/file" then FsOps.create_file(conn, req, opts)
      when "/api/shell" then Shell.exec_shell(conn, req, opts)
      when "/api/sqlite/create" then Sqlite.create(conn, req, opts)
      when "/api/sqlite/query" then Sqlite.query(conn, req, opts)
      when "/api/socket/open" then Sock.open(conn, req, opts)
      when "/api/socket/send" then Sock.send(conn, req, opts)
      when "/api/socket/close" then Sock.close(conn, req, opts)
      when "/api/socket/starttls" then Sock.starttls(conn, req, opts)
      when "/api/oauth2/start" then OAuth2.start(conn, req, opts)
      when "/api/oauth2/refresh" then OAuth2.refresh(conn, req, opts)
      when "/api/oauth2/close" then OAuth2.close(conn, req, opts)
      when "/api/oauth2/callback", "/api/oauth2/poll"
        Http.send_err(conn, 405, "Method POST not supported for this route.")
      when "/", "/api/help", "/api/dir", "/api/sysinfo", "/api/socket/recv",
           "/api/socket/ws"
        Http.send_err(conn, 405, "Method POST not supported for this route.")
      else
        Http.send_err(conn, 404, "Unknown route: #{req.path}")
      end
    end

    def do_put(conn, req, opts)
      if req.path == "/api/file"
        FsOps.overwrite_file(conn, req, opts)
        return
      end
      if ALWAYS_KNOWN.include?(req.path)
        Http.send_err(conn, 405, "Method PUT not supported for this route.")
      else
        Http.send_err(conn, 404, "Unknown route: #{req.path}")
      end
    end

    def do_delete(conn, req, opts)
      case req.path
      when "/api/file" then FsOps.delete_file(conn, req, opts)
      when "/api/dir" then FsOps.delete_dir(conn, req, opts)
      when "/api/dir/shallow", "/api/shallow_dir", "/api/dir_shallow" then FsOps.delete_dir_shallow(conn, req, opts)
      else
        if ALWAYS_KNOWN.include?(req.path)
          Http.send_err(conn, 405, "Method DELETE not supported for this route.")
        else
          Http.send_err(conn, 404, "Unknown route: #{req.path}")
        end
      end
    end

    def help(conn, opts)
      catalog = Routes.all
      Http.send_json(conn, 200,
                     "version" => VERSION,
                     "count" => Tags.jenc(catalog.length),
                     "routes" => catalog,
                     "auth" => {
                       "required" => Tags.jenc(opts.auth_required),
                       "open_routes" => ["/", "/api/help", "/api/oauth2/callback", "OPTIONS *"],
                       "how" => "Authorization: Bearer <token> header (preferred) or ?token= query parameter."
                     })
    end

    def index(conn)
      Http.send_json(conn, 200,
                     "service" => SERVICE_NAME, "version" => VERSION,
                     "help" => "/api/help")
    end
  end
end
