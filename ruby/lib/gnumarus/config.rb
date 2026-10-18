# frozen_string_literal: true

require "securerandom"
require_relative "util"

module Gnumarus
  # Multi-source configuration: CLI flag > GNUMARUS_* env var > stdin
  # key=value lines > config.cfg (cwd or exe dir) > default. Mirrors
  # go/config.go exactly, including the "config sources:" summary.
  module Config
    KEYS = %w[host port token no_auth].freeze
    ENV_PREFIX = "GNUMARUS_"
    FILE_NAME = "config.cfg"
    MAX_BYTES = 1024 * 1024

    class ConfigError < StandardError; end

    Resolved = Struct.new(:host, :port, :token, :no_auth, :sources)

    module_function

    # -- key=value parsing -----------------------------------------------

    # Binary or invalid-UTF-8 input is ignored wholesale (Go behaviour).
    def parse_kv(data)
      out = {}
      return out if data.nil?

      bytes = data.dup.force_encoding(Encoding::BINARY)
      return out if bytes.include?("\0")

      text = bytes.force_encoding(Encoding::UTF_8)
      return out unless text.valid_encoding?

      text.split("\n", -1).each do |raw_line|
        line = raw_line.strip
        next if line.empty? || line.start_with?("#") || line.start_with?(";")
        next if line.start_with?("[") && line.end_with?("]")

        eq = line.index("=")
        next if eq.nil?

        key = line[0...eq].strip.downcase
        value = line[(eq + 1)..].strip
        if value.length >= 2 &&
           ((value.start_with?("'") && value.end_with?("'")) ||
            (value.start_with?('"') && value.end_with?('"')))
          value = value[1..-2]
        end
        out[key] = value if KEYS.include?(key)
      end
      out
    end

    def read_limited(path)
      File.open(path, "rb") { |fh| fh.read(MAX_BYTES) }
    rescue SystemCallError, IOError
      nil
    end

    # First existing candidate wins (working dir, then exe dir).
    def load_file_config
      candidates.each do |candidate|
        data = read_limited(candidate)
        next if data.nil?

        return parse_kv(data)
      end
      {}
    end

    def candidates
      out = [File.join(Dir.pwd, FILE_NAME)]
      exe = program_path
      out << File.join(File.dirname(exe), FILE_NAME) if exe
      out
    end

    def program_path
      File.expand_path($PROGRAM_NAME)
    rescue StandardError
      nil
    end

    # Never blocks: a terminal (or nothing piped) means absent.
    def load_stdin_config
      return {} if $stdin.tty?

      data = $stdin.read(MAX_BYTES)
      return {} if data.nil? || data.empty?

      parse_kv(data)
    rescue SystemCallError, IOError
      {}
    end

    # -- value coercion --------------------------------------------------

    def parse_bool(value, source)
      return value if value == true || value == false

      text = value.to_s.strip.downcase
      return true if %w[1 true yes on].include?(text)
      return false if %w[0 false no off].include?(text)

      raise ConfigError,
            "invalid boolean for no_auth from #{source}: '#{value}' (use true/false)"
    end

    def parse_port(value, source)
      return check_port(value, source, value) if value.is_a?(Integer)

      unless value.is_a?(String)
        raise ConfigError, "invalid port from #{source}: '#{value}' (1-65535)"
      end

      text = value.strip
      negative = false
      if text.start_with?("+")
        text = text[1..]
      elsif text.start_with?("-")
        negative = true
        text = text[1..]
      end
      unless !text.empty? && text.match?(/\A[0-9]+\z/)
        raise ConfigError, "invalid port from #{source}: '#{value}' (1-65535)"
      end

      number = text.to_i
      number = -number if negative
      check_port(number, source, value)
    end

    def check_port(number, source, value)
      if number < 1 || number > 65_535
        raise ConfigError, "port from #{source} out of range: #{number} (1-65535)"
      end

      number
    end

    # -- resolution ------------------------------------------------------

    def resolve_config(cli, stdin_cfg, file_cfg)
      env_cfg = {}
      KEYS.each do |key|
        value = ENV[ENV_PREFIX + key.upcase]
        env_cfg[key] = value if value && !value.empty?
      end

      layers = [["cli", cli], ["env", env_cfg], ["stdin", stdin_cfg],
                ["file", file_cfg]]
      raw = {}
      sources = {}
      KEYS.each do |key|
        sources[key] = "default"
        layers.each do |name, layer|
          next unless layer.key?(key) && !layer[key].nil?

          raw[key] = layer[key]
          sources[key] = name
          break
        end
      end

      host = "127.0.0.1"
      host = raw["host"].to_s.strip if raw.key?("host")
      raise ConfigError, "host from #{sources['host']} must not be empty" if host.empty?
      unless Util.bind_allowed?(host)
        raise ConfigError,
              "host from #{sources['host']} must be a loopback address " \
              "(127.0.0.1, ::1 or localhost), got '#{host}'"
      end

      port = 1423
      port = parse_port(raw["port"], sources["port"]) if raw.key?("port")

      no_auth = false
      no_auth = parse_bool(raw["no_auth"], sources["no_auth"]) if raw.key?("no_auth")

      token = ""
      if no_auth
        sources["token"] = "disabled"
      elsif raw.key?("token")
        token = raw["token"].to_s
        if token.empty?
          raise ConfigError, "token from #{sources['token']} must not be empty"
        end
      else
        token = SecureRandom.urlsafe_base64(32)
        sources["token"] = "generated"
      end

      Resolved.new(host, port, token, no_auth, sources)
    end

    # -- CLI -------------------------------------------------------------

    def parse_argv(argv)
      cli = {}
      i = 0
      while i < argv.length
        arg = argv[i]
        if arg == "--host" || arg == "--port" || arg == "--token"
          i += 1
          raise ConfigError, "#{arg} needs a value" if i >= argv.length

          cli[arg[2..]] = argv[i]
        elsif arg == "--no-auth"
          if cli.key?("no_auth") && cli["no_auth"] == false
            raise ConfigError, "--auth and --no-auth are mutually exclusive"
          end

          cli["no_auth"] = true
        elsif arg == "--auth"
          if cli.key?("no_auth") && cli["no_auth"] == true
            raise ConfigError, "--auth and --no-auth are mutually exclusive"
          end

          cli["no_auth"] = false
        elsif arg.start_with?("--port=")
          cli["port"] = arg[7..]
        elsif arg.start_with?("--host=")
          cli["host"] = arg[7..]
        elsif arg.start_with?("--token=")
          cli["token"] = arg[8..]
        else
          raise ConfigError, "unknown argument: #{arg}"
        end
        i += 1
      end
      cli
    end
  end
end
