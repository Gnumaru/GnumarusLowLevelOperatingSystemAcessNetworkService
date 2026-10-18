# frozen_string_literal: true

module Gnumarus
  # Tagged wire scalars: JSON carries only string/array/object. Dynamic
  # values travel as "i:<int>", "f:<float>", "b:true|false", "null";
  # fixed vocabulary and free text stay raw; sqlite row cells and
  # params values are fully tagged (strings take an "s:" prefix).
  module Tags
    # Matches the Go reference exactly (no shorthand forms, no leading
    # zeros for ints).
    INT_RE = /\A-?(0|[1-9][0-9]*)\z/
    FLOAT_RE = /\A-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\z/

    class BadTag < StandardError; end

    module_function

    # Go's strconv.FormatFloat(f, 'f', -1, 64): the shortest decimal
    # that round-trips, never in exponent form.
    def format_float(value)
      raise ArgumentError, "non-finite float" unless value.finite?

      text = value.to_s
      if text =~ /[eE]/
        mant, exp_text = text.split(/[eE]/, 2)
        exp = exp_text.to_i
        negative = mant.start_with?("-")
        mant = mant[1..] if negative
        int_part, frac_part = mant.split(".", 2)
        frac_part ||= ""
        digits = int_part + frac_part
        point = int_part.length + exp
        out =
          if point <= 0
            "0." + ("0" * -point) + digits
          elsif point >= digits.length
            digits + ("0" * (point - digits.length))
          else
            digits[0...point] + "." + digits[point..]
          end
        out = out.sub(/\.?0+\z/, "") if out.include?(".")
        out = "0" if out.empty? || out == "-"
        out = "-#{out}" if negative && out != "0"
        return out
      end
      text.sub(/\.0\z/, "")
    end

    # Encodes a value scalar as a tagged string.
    def jenc(value)
      case value
      when nil then "null"
      when true then "b:true"
      when false then "b:false"
      when Integer then "i:#{value}"
      when Float then "f:#{format_float(value)}"
      else
        raise ArgumentError, "jenc takes nil/bool/int/float"
      end
    end

    # Tags a string for fully-tagged positions (row cells, params).
    def jtagstr(text)
      "s:#{text}"
    end

    # Parses a tagged string back to a value.
    def jdec(text)
      raise BadTag, "expected a tagged string" unless text.is_a?(String)
      return nil if text == "null"

      if text.length > 2 && text[0] == "i" && text[1] == ":"
        body = text[2..]
        raise BadTag, "bad int tag: #{text}" unless INT_RE.match?(body)

        return Integer(body, 10)
      end
      if text.length > 2 && text[0] == "f" && text[1] == ":"
        body = text[2..]
        raise BadTag, "bad float tag: #{text}" unless FLOAT_RE.match?(body)

        value = Float(body)
        raise BadTag, "bad float tag: #{text}" unless value.finite?

        return value
      end
      return true if text == "b:true"
      return false if text == "b:false"
      return text[2..] if text.length > 2 && text[0] == "s" && text[1] == ":"

      raise BadTag, "untagged scalar: #{text}"
    end
  end
end
