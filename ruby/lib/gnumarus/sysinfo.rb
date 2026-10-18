# frozen_string_literal: true

require "etc"
require "rbconfig"

module Gnumarus
  # GET /api/sysinfo: operating-system information about the host.
  module Sysinfo
    module_function

    def read_os_release
      info = {}
      File.foreach("/etc/os-release") do |line|
        text = line.strip
        next if text.empty? || text.start_with?("#") || !text.include?("=")

        eq = text.index("=")
        key = text[0...eq]
        value = text[(eq + 1)..].strip
        value = value.sub(/\A["']/, "").sub(/["']\z/, "")
        info[key] = value
      end
      info
    rescue SystemCallError, IOError
      {}
    end

    def kernel_info
      release = ""
      version = ""
      begin
        release = File.read("/proc/sys/kernel/osrelease").strip
      rescue SystemCallError, IOError
        release = ""
      end
      begin
        version = File.read("/proc/sys/kernel/version").strip
      rescue SystemCallError, IOError
        version = ""
      end
      [release, version]
    end

    def uname
      @uname ||= Etc.uname
    rescue StandardError
      {}
    end

    def sysinfo(conn, _req, _opts)
      raw_system = uname[:sysname] || ""
      system =
        case raw_system
        when "Windows" then "Windows"
        when "Darwin" then "Darwin"
        else "Linux"
        end
      system_name = system == "Darwin" ? "macOS" : system

      distro = "null"
      windows = "null"
      mac = "null"
      if system == "Linux"
        rel = read_os_release
        unless rel.empty?
          distro = {
            "id" => rel["ID"] || "",
            "name" => rel["NAME"] || "",
            "version" => rel["VERSION"] || "",
            "version_id" => rel["VERSION_ID"] || "",
            "pretty_name" => rel["PRETTY_NAME"] || ""
          }
        end
      elsif system == "Windows"
        windows = { "release" => system, "version" => "", "service_pack" => "", "edition" => "" }
      elsif system == "Darwin"
        mac = { "version" => "" }
      end

      release, version = kernel_info
      hostname = begin
        Socket.gethostname
      rescue StandardError
        ""
      end
      cpu = begin
        Etc.nprocessors
      rescue StandardError
        1
      end
      cpu = 1 if cpu < 1

      Http.send_json(conn, 200,
                     "system" => system,
                     "system_name" => system_name,
                     "release" => release,
                     "version" => version,
                     "machine" => (RbConfig::CONFIG["host_cpu"] || ""),
                     "processor" => "",
                     "hostname" => hostname,
                     "ruby_version" => RUBY_VERSION,
                     "cpu_count" => Tags.jenc(cpu),
                     "distro" => distro,
                     "windows" => windows,
                     "mac" => mac)
    end
  end
end
