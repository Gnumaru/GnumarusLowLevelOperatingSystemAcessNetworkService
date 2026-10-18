using System.Security.Cryptography;

/// <summary>Multi-source configuration: CLI flag > env var > stdin > config.cfg > default.</summary>
static class Config
{
    public static readonly string[] Keys = ["host", "port", "token", "no_auth"];
    public const string FileName = "config.cfg";
    public const int MaxBytes = 1024 * 1024;
    public const string EnvPrefix = "GNUMARUS_";

    public static bool BindAllowed(string host)
    {
        var h = host.Trim();
        if (h.StartsWith('[') && h.EndsWith(']') && h.Length > 2)
            h = h[1..^1];
        return Api.LoopbackName(h);
    }

    public sealed record Resolved(
        string Host, int Port, string? Token, bool NoAuth,
        Dictionary<string, string> Sources);

    public sealed class ConfigError(string message) : Exception(message);

    public static Dictionary<string, string> ParseKv(byte[] data)
    {
        if (data.Contains((byte)0)) return new();
        string text;
        try
        {
            // throwOnInvalidBytes: rejects non-UTF-8 wholesale.
            text = new System.Text.UTF8Encoding(false, true).GetString(data);
        }
        catch
        {
            return new();
        }
        var cfg = new Dictionary<string, string>();
        foreach (var rawLine in text.Split(['\r', '\n']))
        {
            var line = rawLine.Trim();
            if (line.Length == 0 || line[0] == '#' || line[0] == ';') continue;
            if (line[0] == '[' && line.EndsWith(']')) continue;
            var eq = line.IndexOf('=');
            if (eq < 0) continue;
            var key = line[..eq].Trim().ToLowerInvariant();
            var value = line[(eq + 1)..].Trim();
            if (value.Length >= 2 && ((value[0] == '\'' && value[^1] == '\'') ||
                    (value[0] == '"' && value[^1] == '"')))
                value = value[1..^1];
            if (Keys.Contains(key)) cfg[key] = value;
        }
        return cfg;
    }

    /// <summary>Candidate config.cfg locations: working dir, exe dir.</summary>
    public static IEnumerable<string> Candidates()
    {
        yield return Path.Combine(Directory.GetCurrentDirectory(), FileName);
        var exe = AppContext.BaseDirectory;
        if (!string.IsNullOrEmpty(exe)) yield return Path.Combine(exe, FileName);
    }

    public static Dictionary<string, string> LoadFile()
    {
        foreach (var cand in Candidates())
        {
            try
            {
                if (!File.Exists(cand)) continue;
                using var fh = File.OpenRead(cand);
                var buf = new byte[MaxBytes];
                var n = 0;
                int r;
                while (n < MaxBytes &&
                    (r = fh.Read(buf, n, MaxBytes - n)) > 0) n += r;
                return ParseKv(buf[..n]);
            }
            catch
            {
                continue;
            }
        }
        return new();
    }

    public static Dictionary<string, string> LoadStdin()
    {
        // Never block: a terminal (or nothing piped) means absent.
        try
        {
            if (!Console.IsInputRedirected) return new();
            using var stdin = Console.OpenStandardInput();
            var ms = new MemoryStream();
            var buf = new byte[65536];
            var left = MaxBytes;
            int n;
            while (left > 0 && (n = stdin.Read(buf, 0, Math.Min(buf.Length, left))) > 0)
            {
                ms.Write(buf, 0, n);
                left -= n;
            }
            var data = ms.ToArray();
            if (data.Length == 0) return new();
            return ParseKv(data);
        }
        catch
        {
            return new();
        }
    }

    public static bool ParseBool(object? value, string source)
    {
        if (value is bool b) return b;
        var t = value?.ToString()?.Trim().ToLowerInvariant() ?? "";
        if (t is "1" or "true" or "yes" or "on") return true;
        if (t is "0" or "false" or "no" or "off") return false;
        throw new ConfigError($"invalid boolean for no_auth from {source}: '{value}' (use true/false)");
    }

    public static int ParsePort(object? value, string source)
    {
        if (value is bool)
            throw new ConfigError($"invalid port from {source}: '{value}' (1-65535)");
        long port;
        if (value is long l) port = l;
        else if (value is int i) port = i;
        else if (value is string s && System.Text.RegularExpressions.Regex.IsMatch(
                s.Trim(), @"^[+-]?\d+$"))
            port = long.Parse(s.Trim(), System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture);
        else
            throw new ConfigError($"invalid port from {source}: '{value}' (1-65535)");
        if (port < 1 || port > 65535)
            throw new ConfigError($"port from {source} out of range: {port} (1-65535)");
        return (int)port;
    }

    public static Resolved Resolve(
        Dictionary<string, object?> cli,
        Dictionary<string, string> stdinCfg,
        Dictionary<string, string> fileCfg)
    {
        var envCfg = new Dictionary<string, string>();
        foreach (var key in Keys)
        {
            var v = Environment.GetEnvironmentVariable(EnvPrefix + key.ToUpperInvariant());
            if (!string.IsNullOrEmpty(v)) envCfg[key] = v;
        }
        var layers = new (string Source, IDictionary<string, object?> Layer)[]
        {
            ("cli", cli.ToDictionary(kv => kv.Key, kv => kv.Value)),
            ("env", envCfg.ToDictionary(kv => kv.Key, kv => (object?)kv.Value)),
            ("stdin", stdinCfg.ToDictionary(kv => kv.Key, kv => (object?)kv.Value)),
            ("file", fileCfg.ToDictionary(kv => kv.Key, kv => (object?)kv.Value)),
        };
        var raw = new Dictionary<string, object?>();
        var sources = new Dictionary<string, string>();
        foreach (var key in Keys)
        {
            sources[key] = "default";
            foreach (var (source, layer) in layers)
            {
                if (layer.TryGetValue(key, out var v) && v is not null)
                {
                    raw[key] = v;
                    sources[key] = source;
                    break;
                }
            }
        }
        var host = (raw.GetValueOrDefault("host")?.ToString() ?? "127.0.0.1").Trim();
        if (host == "") throw new ConfigError($"host from {sources["host"]} must not be empty");
        if (!BindAllowed(host)) throw new ConfigError($"host from {sources["host"]} must be a loopback address (127.0.0.1, ::1 or localhost), got '{host}'");
        var port = raw.ContainsKey("port") ? ParsePort(raw["port"], sources["port"]) : 1423;
        var noAuth = raw.ContainsKey("no_auth") ? ParseBool(raw["no_auth"], sources["no_auth"]) : false;
        string? token = null;
        if (noAuth)
        {
            sources["token"] = "disabled";
        }
        else if (raw.ContainsKey("token"))
        {
            token = raw["token"]?.ToString() ?? "";
            if (token == "") throw new ConfigError($"token from {sources["token"]} must not be empty");
        }
        else
        {
            var bytes = RandomNumberGenerator.GetBytes(32);
            token = Convert.ToBase64String(bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=');
            sources["token"] = "generated";
        }
        return new Resolved(host, port, token, noAuth, sources);
    }
}
