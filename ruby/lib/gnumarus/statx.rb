# frozen_string_literal: true

require "fiddle"

module Gnumarus
  # Linux birth time via the raw statx syscall (Fiddle, no gems).
  # File::Stat#birthtime raises NotImplementedError on this Ruby build,
  # so we call libc's statx() with STATX_BTIME directly (the same 256-byte
  # struct the Go/Rust/C++ ports read: mask at 0, btime at 80).
  module Statx
    AT_FDCWD = -100
    AT_SYMLINK_NOFOLLOW = 0x100
    STATX_BTIME = 0x800
    SIZE = 256
    OFF_MASK = 0
    OFF_BTIME = 80

    module_function

    def fun
      return @fun if defined?(@fun)

      @fun = begin
        Fiddle::Function.new(
          Fiddle::Handle::DEFAULT["statx"],
          [Fiddle::TYPE_INT, Fiddle::TYPE_VOIDP, Fiddle::TYPE_INT,
           Fiddle::TYPE_UINT, Fiddle::TYPE_VOIDP],
          Fiddle::TYPE_INT
        )
      rescue StandardError
        nil
      end
    end

    # Returns epoch seconds (Float) or nil when unavailable.
    def birth_time(path)
      fn = fun
      return nil if fn.nil?

      buffer = Fiddle::Pointer.malloc(SIZE)
      buffer[0, SIZE] = "\0" * SIZE
      rc = fn.call(AT_FDCWD, path, AT_SYMLINK_NOFOLLOW, STATX_BTIME, buffer)
      return nil unless rc.zero?

      mask = buffer[OFF_MASK, 4].unpack1("V")
      return nil if (mask & STATX_BTIME).zero?

      sec = buffer[OFF_BTIME, 8].unpack1("q<")
      nsec = buffer[OFF_BTIME + 8, 4].unpack1("V")
      sec + nsec / 1e9
    rescue StandardError
      nil
    end
  end
end
