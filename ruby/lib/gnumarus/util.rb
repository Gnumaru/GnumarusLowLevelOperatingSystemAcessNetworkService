# frozen_string_literal: true

require "json"

module Gnumarus
  # Shared plumbing: wire JSON, path resolution, metadata helpers,
  # MIME guessing, query parsing and the loopback/Host allowlist.
  module Util
    MIME_BY_EXT = {
      "txt" => "text/plain", "html" => "text/html", "htm" => "text/html",
      "json" => "application/json", "xml" => "text/xml", "css" => "text/css",
      "js" => "text/javascript", "png" => "image/png", "jpg" => "image/jpeg",
      "jpeg" => "image/jpeg", "gif" => "image/gif", "pdf" => "application/pdf",
      "zip" => "application/zip", "csv" => "text/csv", "md" => "text/markdown"
    }.freeze

    module_function

    # -- wire JSON -------------------------------------------------------

    # JSON.generate raises on invalid UTF-8; the Go reference replaces
    # invalid bytes with U+FFFD (encoding/json). Retry after scrubbing.
    def json_generate(obj)
      JSON.generate(obj)
    rescue JSON::GeneratorError, Encoding::UndefinedConversionError
      JSON.generate(deep_scrub(obj))
    end

    def deep_scrub(obj)
      case obj
      when Hash
        obj.each_with_object({}) { |(k, v), out| out[scrub(k)] = deep_scrub(v) }
      when Array
        obj.map { |v| deep_scrub(v) }
      when String
        scrub(obj)
      else
        obj
      end
    end

    def scrub(text)
      text.dup.force_encoding(Encoding::UTF_8).scrub("\uFFFD")
    end

    # Forced UTF-8 decode with replacement (Go's string(bytes)).
    def utf8(bytes)
      bytes.dup.force_encoding(Encoding::UTF_8).scrub("\uFFFD")
    end

    def json_parse_object(body)
      text = body.to_s
      return [{}] if text.strip.empty?

      obj = JSON.parse(text)
      return [nil, "JSON body must be an object."] unless obj.is_a?(Hash)

      [obj]
    rescue JSON::ParserError
      [nil, "Invalid JSON body."]
    end

    # -- paths -----------------------------------------------------------

    def resolve_path(raw, cwd)
      p = raw
      if p == "~" || p.start_with?("~/")
        home = home_dir
        p = home + p[1..] if home && !home.empty?
      end
      p = File.join(cwd, p) unless absolute_path?(p)
      clean(p)
    end

    def home_dir
      Dir.home
    rescue StandardError
      nil
    end

    def absolute_path?(p)
      p.start_with?("/")
    end

    # filepath.Clean equivalent (also collapses "..").
    def clean(p)
      File.expand_path(p)
    end

    def lexists?(p)
      File.lstat(p)
      true
    rescue SystemCallError
      false
    end

    def entry_type(p)
      li = begin
        File.lstat(p)
      rescue SystemCallError
        nil
      end
      return "other" if li.nil?
      return "symlink" if li.symlink?

      st = begin
        File.stat(p)
      rescue SystemCallError
        nil
      end
      if st
        return "dir" if st.directory?
        return "file" if st.file?
      end
      "other"
    end

    def mode_string(p)
      li = File.lstat(p)
      "0o" + (li.mode & 0o777).to_s(8)
    rescue SystemCallError
      "0o644"
    end

    def epoch(time)
      return nil if time.nil?

      time.to_f
    end

    def guess_mime(p)
      ext = File.extname(p).sub(/\A\./, "").downcase
      return "application/octet-stream" if ext.empty?

      MIME_BY_EXT[ext] || "application/octet-stream"
    end

    # Strict UTF-8 check matching the intent of the Go reference.
    def decodable?(bytes)
      bytes.dup.force_encoding(Encoding::UTF_8).valid_encoding?
    end

    def has_binary_controls?(text)
      text.each_char.any? do |ch|
        ord = ch.ord
        ord < 32 && ord != 9 && ord != 10 && ord != 13
      end
    end

    # -- loopback / Host allowlist ---------------------------------------

    def all_digits?(text)
      !text.empty? && text.each_char.all? { |c| c >= "0" && c <= "9" }
    end

    # Strict dotted quads (no shorthands, no leading zeros) in
    # 127.0.0.0/8, or exactly "::1".
    def quad_loopback?(name)
      return true if name == "::1"

      parts = name.split(".", -1)
      return false unless parts.length == 4

      parts.each do |p|
        return false if p.empty? || (p.length > 1 && p[0] == "0")
        return false unless p.each_char.all? { |c| c >= "0" && c <= "9" }

        return false if p.to_i > 255
      end
      parts[0] == "127"
    end

    def host_allowed?(host_header)
      h = host_header.to_s.strip
      return false if h.empty?

      if h.start_with?("[")
        ending = h.index("]")
        return false if ending.nil?

        rest = h[(ending + 1)..]
        unless rest.empty?
          return false unless rest.start_with?(":") && all_digits?(rest[1..])
        end
        name = h[1...ending]
      else
        return false if h.count(":") > 1

        last = h.rindex(":")
        if last
          return false unless all_digits?(h[(last + 1)..])

          name = h[0...last]
        else
          name = h
        end
      end
      return false if name.empty?
      return true if name.casecmp?("localhost")

      quad_loopback?(name)
    end

    # Whether an address may be bound: loopback only.
    def bind_allowed?(host)
      h = host.to_s.strip
      h = h[1..-2] if h.start_with?("[") && h.end_with?("]") && h.length > 2
      return true if h.casecmp?("localhost")

      quad_loopback?(h)
    end

    # -- query strings ---------------------------------------------------

    def parse_query(query)
      out = {}
      query.to_s.split("&", -1).each do |pair|
        next if pair.empty?

        key, _sep, value = pair.partition("=")
        key = unescape(key)
        value = unescape(value)
        (out[key] ||= []) << value
      end
      out
    end

    def first_query(query, key)
      values = query[key]
      values && !values.empty? ? values[0] : ""
    end

    def unescape(text)
      text.gsub(/\+|%[0-9A-Fa-f]{2}/) do |m|
        m == "+" ? " " : [m[1, 2]].pack("H2")
      end.force_encoding(Encoding::UTF_8)
    end
  end
end
