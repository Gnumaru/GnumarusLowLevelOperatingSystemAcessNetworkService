# frozen_string_literal: true

require "base64"
require_relative "tags"
require_relative "util"

module Gnumarus
  # POST /api/sqlite/create and /api/sqlite/query: stateless SQLite access
  # (fresh connection per request) with tagged cells/params and a
  # single-statement scan. The driver is the sqlite3 gem under
  # ruby/vendor/gems; a 503 is returned when it is unavailable.
  module Sqlite
    DEFAULT_TIMEOUT = 5.0
    MAX_TIMEOUT = 300.0

    ROW_FIRST = %w[SELECT VALUES WITH EXPLAIN PRAGMA TABLE].freeze
    DDL_FIRST = %w[CREATE DROP ALTER VACUUM ANALYZE REINDEX ATTACH DETACH
                   BEGIN COMMIT ROLLBACK SAVEPOINT END].freeze

    @driver_ok = nil

    module_function

    def driver_ok?
      return @driver_ok unless @driver_ok.nil?

      @driver_ok = begin
        require "sqlite3"
        true
      rescue LoadError
        false
      end
    end

    def unavailable(conn)
      Http.send_err(conn, 503,
                    "SQLite support unavailable: sqlite3 gem failed to load.")
    end

    # -- SQL scanning ----------------------------------------------------

    def word_char?(c)
      c == "_" || (c >= "A" && c <= "Z") || (c >= "a" && c <= "z")
    end

    def word_char_or_digit?(c)
      word_char?(c) || (c >= "0" && c <= "9")
    end

    # Splits SQL into statements and finds the first keyword.
    def scan_sql(sql)
      statements = []
      cur = +""
      first = ""
      returning = false
      word = +""

      flush = lambda do
        unless word.empty?
          w = word.upcase
          first = w if first.empty?
          returning = true if w == "RETURNING"
          word = +""
        end
      end

      state = 0
      i = 0
      n = sql.length
      while i < n
        c = sql[i]
        case state
        when 0
          if c == "'"
            state = 1
            cur << c
            i += 1
          elsif c == '"'
            state = 2
            cur << c
            i += 1
          elsif c == "`"
            state = 3
            cur << c
            i += 1
          elsif c == "-" && i + 1 < n && sql[i + 1] == "-"
            state = 4
            i += 2
          elsif c == "/" && i + 1 < n && sql[i + 1] == "*"
            state = 5
            i += 2
          elsif c == ";"
            flush.call
            statements << cur unless cur.strip.empty?
            cur = +""
            i += 1
          elsif word_char?(c)
            word << c
            cur << c
            i += 1
          else
            flush.call unless (c >= "0" && c <= "9") || word_char?(c) || c == "$"
            cur << c
            i += 1
          end
        when 1
          if c == "'" && i + 1 < n && sql[i + 1] == "'"
            cur << "''"
            i += 2
          else
            cur << c
            i += 1
            state = 0 if c == "'"
          end
        when 2
          if c == '"' && i + 1 < n && sql[i + 1] == '"'
            cur << '""'
            i += 2
          else
            cur << c
            i += 1
            state = 0 if c == '"'
          end
        when 3
          cur << c
          i += 1
          state = 0 if c == "`"
        when 4
          state = 0 if c == "\n"
          i += 1
        else
          if c == "*" && i + 1 < n && sql[i + 1] == "/"
            state = 0
            i += 2
          else
            i += 1
          end
        end
      end
      flush.call
      statements << cur unless cur.strip.empty?
      { statements: statements, first: first, returning: returning }
    end

    # Rewrites :/@/$ named placeholders to `?`; returns [sql, order, err].
    def rewrite_named(sql, named)
      out = +""
      order = []
      state = 0
      i = 0
      n = sql.length
      while i < n
        c = sql[i]
        case state
        when 0
          if c == "'"
            state = 1
            out << c
            i += 1
          elsif c == '"'
            state = 2
            out << c
            i += 1
          elsif c == "`"
            state = 3
            out << c
            i += 1
          elsif c == "-" && i + 1 < n && sql[i + 1] == "-"
            state = 4
            i += 2
          elsif c == "/" && i + 1 < n && sql[i + 1] == "*"
            state = 5
            i += 2
          elsif c == "?"
            return ["", nil, "Unnamed placeholder with object params."]
          elsif (c == ":" || c == "@" || c == "$") && i + 1 < n && word_char?(sql[i + 1])
            j = i + 1
            j += 1 while j < n && word_char_or_digit?(sql[j])
            name = sql[(i + 1)...j]
            return ["", nil, "Missing named param: #{name}."] unless named.key?(name)

            out << "?"
            order << name
            i = j
          else
            out << c
            i += 1
          end
        when 1
          if c == "'" && i + 1 < n && sql[i + 1] == "'"
            out << "''"
            i += 2
          else
            out << c
            i += 1
            state = 0 if c == "'"
          end
        when 2
          if c == '"' && i + 1 < n && sql[i + 1] == '"'
            out << '""'
            i += 2
          else
            out << c
            i += 1
            state = 0 if c == '"'
          end
        when 3
          out << c
          i += 1
          state = 0 if c == "`"
        when 4
          state = 0 if c == "\n"
          i += 1
        else
          if c == "*" && i + 1 < n && sql[i + 1] == "/"
            state = 0
            i += 2
          else
            i += 1
          end
        end
      end
      [out, order, ""]
    end

    def has_named?(sql)
      state = 0
      i = 0
      n = sql.length
      while i < n
        c = sql[i]
        case state
        when 0
          if c == "'"
            state = 1
            i += 1
          elsif c == '"'
            state = 2
            i += 1
          elsif c == "`"
            state = 3
            i += 1
          elsif c == "-" && i + 1 < n && sql[i + 1] == "-"
            state = 4
            i += 2
          elsif c == "/" && i + 1 < n && sql[i + 1] == "*"
            state = 5
            i += 2
          elsif (c == ":" || c == "@" || c == "$") && i + 1 < n && word_char?(sql[i + 1])
            return true
          else
            i += 1
          end
        when 1
          if c == "'" && i + 1 < n && sql[i + 1] == "'"
            i += 2
          else
            state = 0 if c == "'"
            i += 1
          end
        when 2
          if c == '"' && i + 1 < n && sql[i + 1] == '"'
            i += 2
          else
            state = 0 if c == '"'
            i += 1
          end
        when 3
          state = 0 if c == "`"
          i += 1
        when 4
          state = 0 if c == "\n"
          i += 1
        else
          if c == "*" && i + 1 < n && sql[i + 1] == "/"
            state = 0
            i += 2
          else
            i += 1
          end
        end
      end
      false
    end

    def bind_native(sql, params)
      case params
      when nil
        return [sql, [], ""]
      when Array
        return [sql, [], ""] if params.empty?

        return ["", [], "Named placeholder with array params."] if has_named?(sql)

        [sql, params, ""]
      when Hash
        return [sql, [], ""] if params.empty?

        rewritten, order, errmsg = rewrite_named(sql, params)
        return ["", [], errmsg] unless errmsg.empty?

        [rewritten, order.map { |name| params[name] }, ""]
      else
        ["", [], "Field 'params' must be an array or an object."]
      end
    end

    def parse_params(value)
      case value
      when nil
        [nil, ""]
      when Array
        out = []
        value.each do |entry|
          return [nil, "Array params must be tagged scalars (i:/f:/s:/b:/null)."] unless entry.is_a?(String)

          begin
            out << bind_value(Tags.jdec(entry))
          rescue Tags::BadTag
            return [nil, "Array params must be tagged scalars (i:/f:/s:/b:/null)."]
          end
        end
        [out, ""]
      when Hash
        out = {}
        value.each do |key, entry|
          return [nil, "Object params must be tagged scalars (i:/f:/s:/b:/null)."] unless entry.is_a?(String)

          begin
            out[key] = bind_value(Tags.jdec(entry))
          rescue Tags::BadTag
            return [nil, "Object params must be tagged scalars (i:/f:/s:/b:/null)."]
          end
        end
        [out, ""]
      else
        [nil, "Field 'params' must be an array or an object."]
      end
    end

    def bind_value(value)
      return 1 if value == true
      return 0 if value == false

      value
    end

    def cell_to_tag(value)
      case value
      when nil then "null"
      when true, false then Tags.jenc(value)
      when Integer then Tags.jenc(value)
      when Float then Tags.jenc(value)
      when String
        if value.encoding == Encoding::BINARY
          Tags.jtagstr(Base64.strict_encode64(value))
        else
          Tags.jtagstr(value)
        end
      else
        Tags.jtagstr(value.to_s)
      end
    end

    # -- POST /api/sqlite/create -----------------------------------------

    def create(conn, req, opts)
      return unavailable(conn) unless driver_ok?

      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      if Util.lexists?(abs)
        Http.send_err(conn, 409, "Database file already exists.", abs)
        return
      end
      unless FsOps.dir?(File.dirname(abs))
        Http.send_err(conn, 404, "Parent directory does not exist.", abs)
        return
      end

      version = ""
      begin
        db = SQLite3::Database.new(abs)
        begin
          value = db.get_first_value("SELECT sqlite_version()")
          version = value.to_s
        ensure
          db.close
        end
      rescue StandardError
        version = ""
      end

      if version.empty?
        begin
          File.delete(abs)
        rescue StandardError
          nil
        end
        Http.send_err(conn, 400, "Cannot create database", abs)
        return
      end

      size = begin
        File.size(abs)
      rescue StandardError
        0
      end
      Http.send_json(conn, 201,
                     "path" => raw, "absolute_path" => abs,
                     "size" => Tags.jenc(size), "sqlite_version" => version)
    end

    # -- POST /api/sqlite/query ------------------------------------------

    def query(conn, req, opts)
      return unavailable(conn) unless driver_ok?

      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      unless Util.lexists?(abs)
        Http.send_err(conn, 404, "Database file does not exist", abs)
        return
      end
      unless req.header("content-type").to_s.include?("application/json")
        Http.send_err(conn, 400,
                      "Content-Type must be application/json with body { sql, params?, timeout?, readonly? }.")
        return
      end

      obj, errmsg = Util.json_parse_object(req.body)
      if errmsg
        Http.send_err(conn, 400, errmsg)
        return
      end

      sql_text = obj["sql"].is_a?(String) ? obj["sql"] : ""
      if sql_text.strip.empty?
        Http.send_err(conn, 400, "Missing or empty required field: sql.")
        return
      end

      params, errmsg = parse_params(obj["params"])
      unless errmsg.empty?
        Http.send_err(conn, 400, errmsg)
        return
      end

      timeout = DEFAULT_TIMEOUT
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
        unless seconds > 0 && seconds <= MAX_TIMEOUT
          Http.send_err(conn, 400, "Field 'timeout' must be within (0, 300].")
          return
        end
        timeout = seconds
      end

      readonly = false
      if obj.key?("readonly") && !obj["readonly"].nil?
        value = obj["readonly"]
        bad = "Field 'readonly' must be b:true or b:false."
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
        readonly = decoded
      end

      scan = scan_sql(sql_text)
      if scan[:statements].length != 1
        Http.send_err(conn, 400, "Send exactly one SQL statement per request.", abs)
        return
      end
      final_sql, args, errmsg = bind_native(scan[:statements][0], params)
      unless errmsg.empty?
        Http.send_err(conn, 400, errmsg, abs)
        return
      end
      rows_mode = ROW_FIRST.include?(scan[:first]) || scan[:returning]

      db =
        begin
          if readonly
            SQLite3::Database.new(abs, readonly: true)
          else
            SQLite3::Database.new(abs)
          end
        rescue StandardError => e
          Http.send_err(conn, 400, "Cannot open database: #{e}", abs)
          return
        end

      begin
        db.busy_timeout = (timeout * 1000).to_i
      rescue StandardError => e
        begin
          db.close
        rescue StandardError
          nil
        end
        Http.send_err(conn, 400, "Cannot open database: #{e}", abs)
        return
      end

      payload = { "path" => raw, "absolute_path" => abs }
      begin
        if rows_mode
          results = db.execute2(final_sql, args)
          cols = results.shift || []
          rows = results.map { |row| row.map { |cell| cell_to_tag(cell) } }
          payload["columns"] = cols.map { |c| c.nil? ? "" : c.to_s }
          payload["rows"] = rows
          payload["rowcount"] = Tags.jenc(rows.length)
        else
          db.execute(final_sql, args)
          affected = db.changes
          last_id = db.last_insert_row_id
          rc = DDL_FIRST.include?(scan[:first]) ? -1 : affected
          payload["rowcount"] = Tags.jenc(rc)
          payload["lastrowid"] = Tags.jenc(last_id)
        end
      rescue StandardError => e
        Http.send_err(conn, 400, "SQLite error: #{e}", abs)
        return
      ensure
        begin
          db.close
        rescue StandardError
          nil
        end
      end

      Http.send_json(conn, 200, payload)
    end
  end
end
