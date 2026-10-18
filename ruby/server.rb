#!/usr/bin/env ruby
# frozen_string_literal: true

# Gnumarus native-operations HTTP service (Ruby port).
#
# Usage:
#   ruby ruby/server.rb [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]
#
# Settings resolve as: CLI flag > GNUMARUS_* env var > stdin key=value
# lines > config.cfg (cwd, then next to this script) > default.

require "socket"

# The only non-stdlib dependency (sqlite3) is vendored under ruby/vendor
# by ruby/build.sh; pointing Gem at it keeps every default gem working
# while making the driver optional (missing => 503, like the other ports).
VENDOR_GEMS = File.join(__dir__, "vendor", "gems")
if File.directory?(VENDOR_GEMS)
  Gem.paths = { "GEM_HOME" => VENDOR_GEMS, "GEM_PATH" => VENDOR_GEMS }
end

require_relative "lib/gnumarus/api"
require_relative "lib/gnumarus/config"

USAGE = <<~TEXT
  Usage: ruby ruby/server.rb [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]

  Every setting can also come from a GNUMARUS_* environment variable
  (GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from
  ruby/config.cfg. Precedence (highest first): CLI flag > env var >
  stdin > config.cfg > default.
TEXT

def fail_config(message)
  warn "config error: #{message}"
  exit 2
end

def bind_host(host)
  h = host.strip
  h = h[1..-2] if h.start_with?("[") && h.end_with?("]") && h.length > 2
  return "127.0.0.1" if h.casecmp?("localhost")

  h
end

cli =
  begin
    Gnumarus::Config.parse_argv(ARGV)
  rescue Gnumarus::Config::ConfigError => e
    warn e.message
    exit 2
  end
if ARGV.include?("-h") || ARGV.include?("--help")
  warn USAGE
  exit 0
end

$stdout.sync = true

cfg =
  begin
    Gnumarus::Config.resolve_config(cli, Gnumarus::Config.load_stdin_config,
                                    Gnumarus::Config.load_file_config)
  rescue Gnumarus::Config::ConfigError => e
    fail_config(e.message)
  end

sources = cfg.sources
puts "config sources: host=#{sources['host']} port=#{sources['port']} " \
     "token=#{sources['token']} no_auth=#{sources['no_auth']}"

opts = Gnumarus::Opts.new(!cfg.no_auth, cfg.token, Dir.pwd)
host = bind_host(cfg.host)

server =
  begin
    TCPServer.new(host, cfg.port)
  rescue SystemCallError => e
    warn "cannot listen: #{e.message}"
    exit 1
  end
server.setsockopt(:SOCKET, :REUSEADDR, true) rescue nil

puts "GnumarusNativeOps v#{Gnumarus::Api::VERSION} serving on " \
     "http://#{cfg.host}:#{cfg.port} (Ctrl+C to stop)"
if cfg.no_auth
  puts "WARNING: authentication DISABLED (no_auth from #{sources['no_auth']}). " \
       "Anyone able to reach this server has full access, including shell execution."
else
  puts "API token: #{cfg.token}"
  puts "Send it as 'Authorization: Bearer <token>' (or ?token=). /api/help stays open."
end

trap("INT") { exit 0 }
trap("TERM") { exit 0 }

loop do
  socket =
    begin
      server.accept
    rescue IOError, Errno::EBADF
      break
    rescue SystemCallError
      next
    end
  Thread.new(socket) do |sock|
    sock.setsockopt(:IPPROTO_TCP, :TCP_NODELAY, 1) rescue nil
    Gnumarus::Api.handle_conn(Gnumarus::Conn.new(sock), opts)
  rescue StandardError
    begin
      sock.close
    rescue StandardError
      nil
    end
  end
end
