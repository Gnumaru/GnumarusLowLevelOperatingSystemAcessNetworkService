# frozen_string_literal: true

require "base64"
require_relative "tags"
require_relative "util"

module Gnumarus
  # POST /api/shell: run a bash command string with separated stdout /
  # stderr, output caps, stdin, a working directory and a timeout that
  # kills the whole process group.
  module Shell
    DEFAULT_TIMEOUT = 30.0
    MAX_TIMEOUT = 300.0
    DEFAULT_MAX_OUT = 1024 * 1024
    MAX_OUTPUT = 100 * 1024 * 1024

    module_function

    def to_float(value)
      return [value.to_f, true] if value.is_a?(Integer)
      return [value, true] if value.is_a?(Float)

      [0.0, false]
    end

    def to_int(value)
      return [value, true] if value.is_a?(Integer)

      [0, false]
    end

    def clamp(value, lo, hi)
      return lo if value < lo
      return hi if value > hi

      value
    end

    def tag_number?(value)
      value.is_a?(String)
    end

    def exec_shell(conn, req, opts)
      unless req.header("content-type").to_s.include?("application/json")
        Http.send_err(conn, 400,
                      "Content-Type must be application/json with body { command, timeout?, cwd?, max_stdout?, max_stderr?, stdin?, stdin_encoding? }.")
        return
      end

      obj, errmsg = Util.json_parse_object(req.body)
      if errmsg
        Http.send_err(conn, 400, errmsg)
        return
      end

      command = obj["command"].is_a?(String) ? obj["command"] : ""
      if command.strip.empty?
        Http.send_err(conn, 400, "Missing or empty required field: command.")
        return
      end

      timeout = DEFAULT_TIMEOUT
      if obj.key?("timeout") && !obj["timeout"].nil?
        value = obj["timeout"]
        bad = "Field 'timeout' must be a tagged number (i:<int> or f:<float>)."
        unless tag_number?(value)
          Http.send_err(conn, 400, bad)
          return
        end
        begin
          decoded = Tags.jdec(value)
        rescue Tags::BadTag
          Http.send_err(conn, 400, bad)
          return
        end
        seconds, ok = to_float(decoded)
        unless ok
          Http.send_err(conn, 400, bad)
          return
        end
        timeout = clamp(seconds, 1, MAX_TIMEOUT)
      end

      cwd = opts.cwd
      if obj.key?("cwd") && !obj["cwd"].nil?
        value = obj["cwd"]
        unless value.is_a?(String)
          Http.send_err(conn, 400, "Field 'cwd' must be a string.")
          return
        end
        cwd = Util.resolve_path(value, opts.cwd)
        unless FsOps.dir?(cwd)
          Http.send_err(conn, 400, "Working directory does not exist.", cwd)
          return
        end
      end

      max_out, ok = limit_of(conn, obj, "max_stdout")
      return unless ok

      max_err, ok = limit_of(conn, obj, "max_stderr")
      return unless ok

      stdin_raw = ""
      if obj.key?("stdin") && !obj["stdin"].nil?
        value = obj["stdin"]
        unless value.is_a?(String)
          Http.send_err(conn, 400, "Field 'stdin' must be a string.")
          return
        end
        stdin_raw = value
      end

      stdin_encoding = "text"
      if obj.key?("stdin_encoding") && !obj["stdin_encoding"].nil?
        value = obj["stdin_encoding"]
        unless value.is_a?(String)
          Http.send_err(conn, 400, "Field 'stdin_encoding' must be text|base64.")
          return
        end
        stdin_encoding = value.downcase
      end

      case stdin_encoding
      when "text"
        stdin_data = stdin_raw.b
      when "base64"
        begin
          stdin_data = Base64.strict_decode64(stdin_raw)
        rescue ArgumentError
          Http.send_err(conn, 400, "Invalid base64 'stdin'.")
          return
        end
      else
        Http.send_err(conn, 400, "Field 'stdin_encoding' must be text|base64.")
        return
      end

      run(conn, command, cwd, timeout, max_out, max_err, stdin_data)
    end

    def limit_of(conn, obj, key)
      unless obj.key?(key) && !obj[key].nil?
        return [DEFAULT_MAX_OUT, true]
      end

      value = obj[key]
      bad = "Field '#{key}' must be an integer >= 0 (i:<int>)."
      unless value.is_a?(String)
        Http.send_err(conn, 400, bad)
        return [0, false]
      end
      begin
        decoded = Tags.jdec(value)
      rescue Tags::BadTag
        Http.send_err(conn, 400, bad)
        return [0, false]
      end
      limit, ok = to_int(decoded)
      if !ok || limit.negative?
        Http.send_err(conn, 400, bad)
        return [0, false]
      end
      if limit > MAX_OUTPUT
        Http.send_err(conn, 400, "Field '#{key}' exceeds the #{MAX_OUTPUT} byte cap.")
        return [0, false]
      end
      [limit, true]
    end

    def run(conn, command, cwd, timeout, max_out, max_err, stdin_data)
      in_r, in_w = IO.pipe
      out_r, out_w = IO.pipe
      err_r, err_w = IO.pipe

      begin
        pid = Process.spawn("bash", "-c", command,
                            chdir: cwd, in: in_r, out: out_w, err: err_w,
                            pgroup: true)
      rescue SystemCallError => e
        [in_r, in_w, out_r, out_w, err_r, err_w].each { |io| io.close rescue nil }
        Http.send_err(conn, 400, "Cannot start shell: #{e}")
        return
      end

      in_r.close
      out_w.close
      err_w.close

      writer = Thread.new do
        begin
          in_w.write(stdin_data) unless stdin_data.empty?
        rescue SystemCallError, IOError
          nil
        ensure
          in_w.close rescue nil
        end
      end

      out_thread = Thread.new { pump(out_r, max_out) }
      err_thread = Thread.new { pump(err_r, max_err) }

      timed_out = false
      status = nil
      waiter = Thread.new do
        begin
          _, st = Process.waitpid2(pid)
          st
        rescue Errno::ECHILD
          nil
        end
      end

      if waiter.join(timeout)
        status = waiter.value
      else
        timed_out = true
        kill_group(pid)
        unless waiter.join(5)
          begin
            Process.kill("KILL", pid)
          rescue StandardError
            nil
          end
          waiter.join(5)
        end
        status = waiter.value
      end

      out_buf, out_truncated = out_thread.value
      err_buf, err_truncated = err_thread.value
      writer.join(5)
      out_r.close rescue nil
      err_r.close rescue nil

      returncode = 124
      if timed_out
        returncode = 124
      elsif status.nil?
        returncode = 0
      elsif status.exited?
        returncode = status.exitstatus
      else
        returncode = -1
      end

      Http.send_json(conn, 200,
                     "command" => command, "cwd" => cwd,
                     "returncode" => Tags.jenc(returncode),
                     "stdout" => out_buf,
                     "stderr" => err_buf,
                     "timed_out" => Tags.jenc(timed_out),
                     "stdout_truncated" => Tags.jenc(out_truncated),
                     "stderr_truncated" => Tags.jenc(err_truncated),
                     "max_stdout" => Tags.jenc(max_out),
                     "max_stderr" => Tags.jenc(max_err),
                     "stdin_bytes" => Tags.jenc(stdin_data.bytesize))
    end

    def kill_group(pid)
      Process.kill("KILL", -pid)
    rescue SystemCallError
      begin
        Process.kill("KILL", pid)
      rescue SystemCallError
        nil
      end
    end

    # Reads until EOF, keeping at most cap bytes (the rest is discarded
    # but always drained so the child never blocks on a full pipe).
    def pump(io, cap)
      buffer = +"".b
      truncated = false
      loop do
        chunk = io.readpartial(65_536)
        remaining = cap - buffer.bytesize
        if remaining <= 0
          truncated = true
        elsif chunk.bytesize > remaining
          buffer << chunk.byteslice(0, remaining)
          truncated = true
        else
          buffer << chunk
        end
      end
      [buffer, truncated]
    rescue EOFError, IOError, SystemCallError
      [buffer, truncated]
    end
  end
end
