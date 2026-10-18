using System.Text.RegularExpressions;

static partial class Tags
{
    [GeneratedRegex(@"^-?(0|[1-9][0-9]*)$")]
    private static partial Regex IntTag();

    [GeneratedRegex(@"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")]
    private static partial Regex FloatTag();

    /// <summary>Encode a value scalar as a tagged string (i:/f:/b:/null).</summary>
    public static string Enc(object? v) => v switch
    {
        null => "null",
        bool b => b ? "b:true" : "b:false",
        long l => $"i:{l}",
        int i => $"i:{i}",
        double d when double.IsFinite(d) => $"f:{d:R}",
        float f when float.IsFinite(f) => $"f:{f:R}",
        _ => throw new ArgumentException("jenc takes null/bool/int/double"),
    };

    /// <summary>Tag a string for fully-tagged positions (row cells, params).</summary>
    public static string TagStr(string s) => "s:" + s;

    /// <summary>Parse a tagged string back to a value. Throws BadTag.</summary>
    public static object? Dec(object? text)
    {
        if (text is not string s) throw new BadTag("expected a tagged string");
        if (s == "null") return null;
        if (s.StartsWith("i:", StringComparison.Ordinal))
        {
            var body = s[2..];
            if (!IntTag().IsMatch(body)) throw new BadTag($"bad int tag: {s}");
            return long.Parse(body, System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture);
        }
        if (s.StartsWith("f:", StringComparison.Ordinal))
        {
            var body = s[2..];
            if (!FloatTag().IsMatch(body)) throw new BadTag($"bad float tag: {s}");
            var v = double.Parse(body,
                System.Globalization.NumberStyles.Float | System.Globalization.NumberStyles.AllowExponent,
                System.Globalization.CultureInfo.InvariantCulture);
            if (!double.IsFinite(v)) throw new BadTag($"bad float tag: {s}");
            return v;
        }
        if (s == "b:true") return true;
        if (s == "b:false") return false;
        if (s.StartsWith("s:", StringComparison.Ordinal)) return s[2..];
        throw new BadTag($"untagged scalar: {s}");
    }
}

sealed class BadTag(string message) : Exception(message);
