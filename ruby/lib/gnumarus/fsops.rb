# frozen_string_literal: true

require "base64"
require_relative "tags"
require_relative "util"
require_relative "statx"

module Gnumarus
  # Filesystem endpoints: read_directory, read_file, create/overwrite_file,
  # delete_file and delete_directory.
  module FsOps
    module_function

    # -- helpers ---------------------------------------------------------

    def dir?(path)
      st = File.stat(path)
      st.directory?
    rescue SystemCallError
      false
    end

    # Mirrors the Go/Rust/Python "is a directory, not a file" gate:
    # stat() follows symlinks; a symlink to a dir is allowed through.
    def directory_not_file?(abs)
      st = begin
        File.stat(abs)
      rescue SystemCallError
        return false
      end
      return false unless st.directory?

      li = begin
        File.lstat(abs)
      rescue SystemCallError
        return true
      end
      !li.symlink?
    end

    # Parses a write body: raw bytes for non-JSON content types, else
    # {"content","encoding"} with encoding text|base64.
    def parse_write_body(req)
      body = req.body
      content_type = req.header("content-type").to_s
      return [body, "", true] unless content_type.include?("application/json")

      obj, errmsg = Util.json_parse_object(body)
      return [nil, errmsg, false] if errmsg

      has_content = obj.key?("content") && !obj["content"].nil?
      encoding = "text"
      if obj.key?("encoding") && !obj["encoding"].nil?
        raw_encoding = obj["encoding"]
        return [nil, "Invalid encoding: (use text|base64).", false] unless raw_encoding.is_a?(String)

        encoding = raw_encoding.downcase
      end

      case encoding
      when "text"
        return ["".b, "", true] unless has_content
        return [nil, "'content' must be a string for text encoding.", false] unless obj["content"].is_a?(String)

        [obj["content"].b, "", true]
      when "base64"
        return ["".b, "", true] unless has_content
        return [nil, "'content' must be a base64 string.", false] unless obj["content"].is_a?(String)

        begin
          [Base64.strict_decode64(obj["content"]), "", true]
        rescue ArgumentError
          [nil, "Invalid base64 'content'.", false]
        end
      else
        [nil, "Invalid encoding: '#{encoding}' (use text|base64).", false]
      end
    end

    # -- GET /api/dir ----------------------------------------------------

    def read_dir(conn, req, opts)
      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      unless Util.lexists?(abs)
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      end
      st = begin
        File.stat(abs)
      rescue SystemCallError
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      end
      unless st.directory?
        Http.send_err(conn, 400, "Path is not a directory", abs)
        return
      end

      names =
        begin
          Dir.children(abs).sort
        rescue Errno::EACCES, Errno::EPERM
          Http.send_err(conn, 403, "Cannot list directory", abs)
          return
        rescue SystemCallError
          Http.send_err(conn, 403, "Cannot list directory", abs)
          return
        end

      entries = []
      names.each do |name|
        full = File.join(abs, name)
        li = begin
          File.lstat(full)
        rescue SystemCallError
          next
        end
        birth = Statx.birth_time(full)
        entries << {
          "name" => name,
          "path" => full,
          "type" => Util.entry_type(full),
          "size" => Tags.jenc(li.size),
          "atime" => Tags.jenc(Util.epoch(li.atime)),
          "mtime" => Tags.jenc(Util.epoch(li.mtime)),
          "ctime" => Tags.jenc(Util.epoch(li.ctime)),
          "crtime" => birth.nil? ? "null" : Tags.jenc(birth),
          "mode" => Util.mode_string(full)
        }
      end

      Http.send_json(conn, 200,
                     "path" => raw, "absolute_path" => abs,
                     "count" => Tags.jenc(entries.length), "entries" => entries)
    end

    # -- GET /api/file ---------------------------------------------------

    def read_file(conn, req, opts)
      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      unless Util.lexists?(abs)
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      end
      if directory_not_file?(abs)
        Http.send_err(conn, 400, "Path is a directory, not a file", abs)
        return
      end

      encoding = Util.first_query(req.query, "encoding")
      encoding = "auto" if encoding.empty?
      encoding = encoding.downcase
      unless %w[auto text base64].include?(encoding)
        Http.send_err(conn, 400,
                      "Invalid encoding param: '#{encoding}' (use auto|text|base64)", abs)
        return
      end
      raw_mode = Util.first_query(req.query, "raw") == "1"

      data =
        begin
          File.binread(abs)
        rescue Errno::ENOENT
          Http.send_err(conn, 404, "Path does not exist", abs)
          return
        rescue Errno::EACCES, Errno::EPERM
          Http.send_err(conn, 403, "Permission denied", abs)
          return
        rescue SystemCallError => e
          Http.send_err(conn, 403, "Cannot read file: #{e}", abs)
          return
        end

      mtime = "null"
      begin
        mtime = Tags.jenc(Util.epoch(File.lstat(abs).mtime))
      rescue SystemCallError
        mtime = "null"
      end

      if raw_mode
        Http.send_raw(conn, 200, data, Util.guess_mime(abs))
        return
      end

      meta = {
        "path" => raw, "absolute_path" => abs,
        "size" => Tags.jenc(data.bytesize), "mtime" => mtime,
        "mime_type" => Util.guess_mime(abs)
      }

      case encoding
      when "text"
        Http.send_json(conn, 200, meta.merge(
                                   "is_binary" => "b:false", "encoding" => "utf-8",
                                   "content" => Util.utf8(data)))
      when "base64"
        Http.send_json(conn, 200, meta.merge(
                                   "is_binary" => "b:true", "encoding" => "base64",
                                   "content" => Base64.strict_encode64(data)))
      else
        if Util.decodable?(data)
          text = Util.utf8(data)
          if Util.has_binary_controls?(text)
            Http.send_json(conn, 200, meta.merge(
                                       "is_binary" => "b:true", "encoding" => "base64",
                                       "content" => Base64.strict_encode64(data)))
          else
            Http.send_json(conn, 200, meta.merge(
                                       "is_binary" => "b:false", "encoding" => "utf-8",
                                       "content" => text))
          end
        else
          Http.send_json(conn, 200, meta.merge(
                                     "is_binary" => "b:true", "encoding" => "base64",
                                     "content" => Base64.strict_encode64(data)))
        end
      end
    end

    # -- POST /api/file (create) -----------------------------------------

    def create_file(conn, req, opts)
      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      if Util.lexists?(abs)
        Http.send_err(conn, 409, "Path already exists (use PUT to overwrite).", abs)
        return
      end
      unless dir?(File.dirname(abs))
        Http.send_err(conn, 404, "Parent directory does not exist.", abs)
        return
      end

      data, errmsg, ok = parse_write_body(req)
      unless ok
        Http.send_err(conn, 400, errmsg, abs)
        return
      end

      begin
        File.open(abs, File::WRONLY | File::CREAT | File::EXCL, 0o666) do |fh|
          fh.binmode
          fh.write(data)
        end
      rescue Errno::EEXIST
        if Util.lexists?(abs)
          Http.send_err(conn, 409, "Path already exists (use PUT to overwrite).", abs)
        else
          Http.send_err(conn, 400, "Cannot create file: File exists", abs)
        end
        return
      rescue Errno::ENOENT
        Http.send_err(conn, 404, "Parent directory does not exist.", abs)
        return
      rescue Errno::EACCES, Errno::EPERM
        Http.send_err(conn, 403, "Permission denied", abs)
        return
      rescue SystemCallError => e
        Http.send_err(conn, 400, "Cannot create file: #{e}", abs)
        return
      end

      Http.send_json(conn, 201,
                     "path" => raw, "absolute_path" => abs,
                     "size" => Tags.jenc(data.bytesize),
                     "mime_type" => Util.guess_mime(abs))
    end

    # -- PUT /api/file (overwrite) ---------------------------------------

    def overwrite_file(conn, req, opts)
      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      unless Util.lexists?(abs)
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      end
      if directory_not_file?(abs)
        Http.send_err(conn, 400,
                      "Path is a directory, not a file (use DELETE /api/dir).", abs)
        return
      end

      data, errmsg, ok = parse_write_body(req)
      unless ok
        Http.send_err(conn, 400, errmsg, abs)
        return
      end

      begin
        File.binwrite(abs, data)
      rescue Errno::ENOENT
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      rescue Errno::EACCES, Errno::EPERM
        Http.send_err(conn, 403, "Permission denied", abs)
        return
      rescue SystemCallError => e
        Http.send_err(conn, 400, "Cannot write file: #{e}", abs)
        return
      end

      Http.send_json(conn, 200,
                     "path" => raw, "absolute_path" => abs,
                     "size" => Tags.jenc(data.bytesize),
                     "mime_type" => Util.guess_mime(abs))
    end

    # -- DELETE /api/file ------------------------------------------------

    def delete_file(conn, req, opts)
      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      unless Util.lexists?(abs)
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      end
      if directory_not_file?(abs)
        Http.send_err(conn, 400,
                      "Path is a directory, not a file (use DELETE /api/dir).", abs)
        return
      end

      begin
        File.delete(abs)
      rescue Errno::ENOENT
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      rescue Errno::EACCES, Errno::EPERM
        Http.send_err(conn, 403, "Permission denied", abs)
        return
      rescue SystemCallError => e
        Http.send_err(conn, 400, "Cannot delete file: #{e}", abs)
        return
      end

      Http.send_json(conn, 200,
                     "path" => raw, "absolute_path" => abs, "deleted" => "b:true")
    end

    # -- DELETE /api/dir -------------------------------------------------

    def delete_dir(conn, req, opts)
      shallow = Util.first_query(req.query, "shallow")
      if ["1", "true", "yes", "on"].include?(shallow.strip.downcase)
        delete_dir_shallow(conn, req, opts)
        return
      end
      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      unless Util.lexists?(abs)
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      end

      li = begin
        File.lstat(abs)
      rescue SystemCallError
        nil
      end
      if li.nil? || li.symlink?
        Http.send_err(conn, 400, "Path is not a directory", abs)
        return
      end
      st = begin
        File.stat(abs)
      rescue SystemCallError
        nil
      end
      if st.nil? || !st.directory?
        Http.send_err(conn, 400, "Path is not a directory", abs)
        return
      end

      begin
        Dir.rmdir(abs)
      rescue Errno::ENOENT
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      rescue Errno::ENOTEMPTY
        if not_empty?(abs)
          Http.send_err(conn, 400,
                        "Cannot delete directory: Directory not empty: '#{abs}'", abs)
        else
          Http.send_err(conn, 400, "Cannot delete directory: Directory not empty", abs)
        end
        return
      rescue Errno::EACCES, Errno::EPERM
        Http.send_err(conn, 403, "Permission denied", abs)
        return
      rescue SystemCallError => e
        Http.send_err(conn, 400, "Cannot delete directory: #{e}", abs)
        return
      end

      Http.send_json(conn, 200,
                     "path" => raw, "absolute_path" => abs, "deleted" => "b:true")
    end

    def delete_dir_shallow(conn, req, opts)
      raw = Util.first_query(req.query, "path")
      if raw.empty?
        Http.send_err(conn, 400, "Missing required query parameter: path")
        return
      end
      abs = Util.resolve_path(raw, opts.cwd)
      unless Util.lexists?(abs)
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      end

      li = begin
        File.lstat(abs)
      rescue SystemCallError
        nil
      end
      if li.nil? || li.symlink?
        Http.send_err(conn, 400, "Path is not a directory", abs)
        return
      end
      st = begin
        File.stat(abs)
      rescue SystemCallError
        nil
      end
      if st.nil? || !st.directory?
        Http.send_err(conn, 400, "Path is not a directory", abs)
        return
      end

      begin
        children = Dir.children(abs)
      rescue SystemCallError => e
        Http.send_err(conn, 400, "Cannot delete directory: #{e}", abs)
        return
      end

      children.each do |name|
        sub = File.join(abs, name)
        if File.directory?(sub) && !File.symlink?(sub)
          begin
            sub_children = Dir.children(sub)
          rescue SystemCallError => e
            Http.send_err(conn, 400, "Cannot delete directory: #{e}", abs)
            return
          end
          if !sub_children.empty?
            Http.send_err(conn, 400,
                          "Directory is not shallow (contains a non-empty subdirectory)", abs)
            return
          end
        end
      end

      begin
        children.each do |name|
          p = File.join(abs, name)
          if File.directory?(p) && !File.symlink?(p)
            Dir.rmdir(p)
          else
            File.delete(p)
          end
        end
        Dir.rmdir(abs)
      rescue Errno::ENOENT
        Http.send_err(conn, 404, "Path does not exist", abs)
        return
      rescue Errno::EACCES, Errno::EPERM
        Http.send_err(conn, 403, "Permission denied", abs)
        return
      rescue Errno::ENOTEMPTY
        Http.send_err(conn, 400, "Cannot delete directory: Directory not empty: '#{abs}'", abs)
        return
      rescue SystemCallError => e
        Http.send_err(conn, 400, "Cannot delete directory: #{e}", abs)
        return
      end

      Http.send_json(conn, 200,
                     "path" => raw, "absolute_path" => abs, "deleted" => "b:true")
    end

    def not_empty?(dir)
      Dir.children(dir).any?
    rescue SystemCallError
      false
    end
  end
end
