using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;

/// <summary>HTTP API implementation (HttpListener front controller).</summary>
static class Api
{
    public const string VERSION = "0.10.0";
    public const string SERVICE = "GnumarusPythonNativeOperationsHttpService";
    public static readonly string[] OPEN_ROUTES = ["/", "/api/help"];

    public const double SHELL_DEFAULT_TIMEOUT = 30;
    public const double SHELL_MAX_TIMEOUT = 300;
    public const int SHELL_DEFAULT_MAX_OUTPUT = 1024 * 1024;
    public const int SHELL_MAX_OUTPUT = 100 * 1024 * 1024;
    public const double SQLITE_DEFAULT_TIMEOUT = 5;
    public const double SQLITE_MAX_TIMEOUT = 300;

    public sealed record State(bool AuthRequired, string AuthToken, string Cwd);

    // -- plumbing ------------------------------------------------------

    private static void Cors(HttpListenerResponse res)
    {
        res.Headers["Access-Control-Allow-Origin"] = "*";
        res.Headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS";
        res.Headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With";
        res.Headers["Access-Control-Max-Age"] = "86400";
    }

    private static async Task SendJson(HttpListenerResponse res, int status, object data)
    {
        var body = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(data));
        Cors(res);
        res.StatusCode = status;
        res.ContentType = "application/json; charset=utf-8";
        res.ContentLength64 = body.Length;
        await res.OutputStream.WriteAsync(body);
        res.Close();
    }

    private static async Task SendRaw(
        HttpListenerResponse res, int status, byte[] body, string contentType)
    {
        Cors(res);
        res.StatusCode = status;
        res.ContentType = contentType;
        res.ContentLength64 = body.Length;
        await res.OutputStream.WriteAsync(body);
        res.Close();
    }

    private static string Label(int status) => status switch
    {
        400 => "bad_request",
        401 => "unauthorized",
        403 => "forbidden",
        404 => "not_found",
        405 => "method_not_allowed",
        409 => "conflict",
        _ => "error",
    };

    private static Task Err(HttpListenerResponse res, int status, string message, string? path = null)
    {
        var obj = new Dictionary<string, object?> { ["error"] = Label(status), ["message"] = message };
        if (path is not null) obj["path"] = path;
        return SendJson(res, status, obj);
    }

    private static async Task<byte[]> ReadBody(HttpListenerRequest req)
    {
        using var ms = new MemoryStream();
        await req.InputStream.CopyToAsync(ms);
        return ms.ToArray();
    }

    // Strict textual loopback literals: exact "::1", or strict
    // dotted quads (4 parts, no shorthands, no leading zeros) in
    // 127.0.0.0/8. Deliberately NOT IPAddress.TryParse/IsLoopback,
    // whose accepted shapes (IPv4-mapped IPv6, shorthands, etc.)
    // differ per stack.
    internal static bool StrictV4Loopback(string name)
    {
        if (name == "::1") return true;
        var parts = name.Split('.');
        if (parts.Length != 4) return false;
        foreach (var p in parts)
        {
            if (p.Length == 0 || (p.Length > 1 && p[0] == '0')) return false;
            foreach (var c in p) if (c < '0' || c > '9') return false;
            if (p.Length > 3) return false;
            if (int.Parse(p) > 255) return false;
        }
        return parts[0] == "127";
    }

    internal static bool LoopbackName(string name)
    {
        if (name == "") return false;
        if (name.Equals("localhost", StringComparison.OrdinalIgnoreCase))
            return true;
        return StrictV4Loopback(name);
    }

    private static bool IsAsciiDigits(string s)
    {
        if (s == "") return false;
        foreach (var c in s) if (c < '0' || c > '9') return false;
        return true;
    }

    internal static bool CheckHost(string? hostHeader)
    {
        var h = (hostHeader ?? "").Trim();
        if (h == "") return false;
        string name;
        if (h.StartsWith('['))
        {
            var end = h.IndexOf(']');
            if (end < 0) return false;
            var rest = h[(end + 1)..];
            if (rest != "")
            {
                if (!rest.StartsWith(':')) return false;
                if (!IsAsciiDigits(rest[1..])) return false;
            }
            name = h[1..end];
        }
        else
        {
            if (h.Count(c => c == ':') > 1) return false;
            var i = h.LastIndexOf(':');
            if (i >= 0)
            {
                if (!IsAsciiDigits(h[(i + 1)..])) return false;
                name = h[..i];
            }
            else name = h;
        }
        return LoopbackName(name);
    }

    internal const string HostDeniedMsg =
        "Host not allowed (use 127.0.0.1, localhost or ::1).";

    private static string ReqHost(HttpListenerRequest req)
    {
        // "Host" is a protected header under HttpListener and may not
        // appear in Headers: fall back to UserHostName (same value,
        // without the port, which the parser tolerates either way).
        return req.Headers["Host"] ?? req.UserHostName ?? "";
    }

    private static bool CheckAuth(HttpListenerRequest req, HttpListenerResponse res,
        string path, State st)
    {
        if (!CheckHost(ReqHost(req)))
        {
            Err(res, 403, HostDeniedMsg).Wait();
            return false;
        }
        if (!st.AuthRequired) return true;
        if (req.HttpMethod == "GET" && OPEN_ROUTES.Contains(path)) return true;
        var supplied = "";
        var h = req.Headers["Authorization"] ?? "";
        if (h.StartsWith("Bearer ", StringComparison.Ordinal))
            supplied = h["Bearer ".Length..].Trim();
        if (supplied == "") supplied = req.QueryString["token"] ?? "";
        var a = Encoding.UTF8.GetBytes(supplied);
        var b = Encoding.UTF8.GetBytes(st.AuthToken);
        if (st.AuthToken != "" && a.Length == b.Length &&
            System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(a, b))
            return true;
        Cors(res);
        res.StatusCode = 401;
        res.ContentType = "application/json; charset=utf-8";
        res.Headers["WWW-Authenticate"] = "Bearer";
        var body = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["error"] = "unauthorized",
            ["message"] = "Valid API token required: send Authorization: Bearer <token> or ?token=.",
        }));
        res.ContentLength64 = body.Length;
        res.OutputStream.Write(body, 0, body.Length);
        res.Close();
        return false;
    }

    public static string ResolvePath(string rawPath, string cwd)
    {
        var p = rawPath;
        if (p == "~" || p.StartsWith("~/", StringComparison.Ordinal))
            p = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile) + p[1..];
        var abs = Path.IsPathRooted(p) ? p : Path.Combine(cwd, p);
        return NormPath(abs);
    }

    private static string NormPath(string p)
    {
        string root;
        string rest;
        if (p.Length >= 2 && char.IsLetter(p[0]) && p[1] == ':' &&
            (p.Length == 2 || p[2] == '\\' || p[2] == '/'))
        {
            root = p[..2] + "\\";
            rest = p.Length > 2 ? p[2..].TrimStart('\\', '/') : "";
        }
        else if (p.StartsWith(@"\\"))
        {
            root = @"\\";
            rest = p[2..];
        }
        else if (p.StartsWith('/'))
        {
            root = "/";
            rest = p[1..];
        }
        else
        {
            root = "";
            rest = p;
        }
        var sep = (root == "/") ? '/' : '\\';
        var parts = new List<string>();
        foreach (var seg in rest.Split(['/', '\\']))
        {
            if (seg == "" || seg == ".") continue;
            if (seg == "..")
            {
                if (parts.Count > 0) parts.RemoveAt(parts.Count - 1);
                continue;
            }
            parts.Add(seg);
        }
        if (root == "" || root == "/") return root + string.Join("/", parts);
        return root + string.Join("\\", parts);
    }

    private static bool Lexists(string p)
    {
        try
        {
            if (new FileInfo(p).LinkTarget is not null) return true;
        }
        catch { }
        return File.Exists(p) || Directory.Exists(p);
    }

    private static string EntryType(string p)
    {
        try
        {
            if (new FileInfo(p).LinkTarget is not null) return "symlink";
        }
        catch { }
        if (Directory.Exists(p)) return "dir";
        if (File.Exists(p)) return "file";
        return "other";
    }

    private static string ModeString(string p)
    {
        try
        {
            var bits = (int)File.GetUnixFileMode(p) & 0x1FF;
            return "0o" + Convert.ToString(bits, 8);
        }
        catch
        {
            return "0o644";
        }
    }

    private static double EpochSec(DateTime dt) =>
        (dt.ToUniversalTime() - DateTime.UnixEpoch).TotalSeconds;

    private static readonly Dictionary<string, string> MimeByExt = new(StringComparer.OrdinalIgnoreCase)
    {
        ["txt"] = "text/plain", ["html"] = "text/html", ["htm"] = "text/html",
        ["json"] = "application/json", ["xml"] = "text/xml", ["css"] = "text/css",
        ["js"] = "text/javascript", ["png"] = "image/png", ["jpg"] = "image/jpeg",
        ["jpeg"] = "image/jpeg", ["gif"] = "image/gif", ["pdf"] = "application/pdf",
        ["zip"] = "application/zip", ["csv"] = "text/csv", ["md"] = "text/markdown",
    };

    private static string GuessMime(string p)
    {
        var dot = p.LastIndexOf('.');
        if (dot < 0) return "application/octet-stream";
        return MimeByExt.TryGetValue(p[(dot + 1)..], out var m) ? m : "application/octet-stream";
    }

    private static string? DecodeStrict(byte[] data)
    {
        try
        {
            return new UTF8Encoding(false, true).GetString(data);
        }
        catch (DecoderFallbackException)
        {
            return null;
        }
    }

    private static bool HasBinaryControls(string s)
    {
        foreach (var ch in s)
            if (ch < 32 && ch != '\t' && ch != '\n' && ch != '\r') return true;
        return false;
    }

    // -- dispatch ------------------------------------------------------

    public static async Task HandleAsync(HttpListenerContext ctx, State st)
    {
        var req = ctx.Request;
        var res = ctx.Response;
        var path = req.Url?.AbsolutePath ?? "/";
        try
        {
            if (req.HttpMethod == "OPTIONS")
            {
                if (!CheckHost(ReqHost(req)))
                {
                    await Err(res, 403, HostDeniedMsg);
                    return;
                }
                Cors(res);
                res.StatusCode = 204;
                res.Close();
                return;
            }
            if (!CheckAuth(req, res, path, st)) return;
            if (req.HttpMethod == "GET" && path == "/api/socket/ws")
            {
                await HandleSocketWs(ctx, st);
                return;
            }
            switch (req.HttpMethod)
            {
                case "GET": await DoGet(req, res, path, st); break;
                case "POST": await DoPost(req, res, path, st); break;
                case "PUT": await DoPut(req, res, path, st); break;
                case "DELETE": await DoDelete(req, res, path, st); break;
                default: await Err(res, 405, $"Method {req.HttpMethod} not supported."); break;
            }
        }
        catch (Exception e)
        {
            try
            {
                await Err(res, 500, $"Internal error: {e.Message}");
            }
            catch { }
        }
    }

    private static string? ReqPath(HttpListenerRequest req, out string raw)
    {
        raw = req.QueryString["path"] ?? "";
        return string.IsNullOrEmpty(raw) ? null : ResolvePath(raw, "");
    }

    private static async Task DoGet(HttpListenerRequest req, HttpListenerResponse res,
        string path, State st)
    {
        switch (path)
        {
            case "/api/help":
                await SendJson(res, 200, new Dictionary<string, object?>
                {
                    ["version"] = VERSION,
                    ["count"] = Tags.Enc(Routes.Count),
                    ["routes"] = Routes,
                    ["auth"] = new Dictionary<string, object?>
                    {
                        ["required"] = Tags.Enc(st.AuthRequired),
                        ["open_routes"] = new List<object?>(
                            OPEN_ROUTES.Append("OPTIONS *").Cast<object?>()),
                        ["how"] = "Authorization: Bearer <token> header (preferred) or ?token= query parameter.",
                    },
                });
                return;
            case "/api/dir": await HandleReadDir(req, res, st); return;
            case "/api/file": await HandleReadFile(req, res, st); return;
            case "/":
                await SendJson(res, 200, new Dictionary<string, object?>
                {
                    ["service"] = SERVICE, ["version"] = VERSION, ["help"] = "/api/help",
                });
                return;
            case "/api/shell":
            case "/api/sqlite/create":
            case "/api/sqlite/query":
            case "/api/socket/open":
            case "/api/socket/send":
            case "/api/socket/close":
            case "/api/socket/starttls":
                await Err(res, 405, "Method GET not supported for this route (use POST).");
                return;
            case "/api/socket/recv": await HandleSocketRecv(req, res); return;
            case "/api/socket/ws":
                await Err(res, 400, "This route requires a WebSocket upgrade (Upgrade: websocket).");
                return;
            case "/api/sysinfo": await HandleSysinfo(res); return;
            default: await Err(res, 404, $"Unknown route: {path}"); return;
        }
    }

    private static async Task DoPost(HttpListenerRequest req, HttpListenerResponse res,
        string path, State st)
    {
        switch (path)
        {
            case "/api/file": await HandleCreateFile(req, res, st); return;
            case "/api/shell": await HandleExecShell(req, res, st); return;
            case "/api/sqlite/create": await HandleSqliteCreate(req, res, st); return;
            case "/api/sqlite/query": await HandleSqliteQuery(req, res, st); return;
            case "/api/socket/open": await HandleSocketOpen(req, res); return;
            case "/api/socket/send": await HandleSocketSend(req, res); return;
            case "/api/socket/close": await HandleSocketClose(req, res); return;
            case "/api/socket/starttls": await HandleSocketStarttls(req, res); return;
            case "/":
            case "/api/help":
            case "/api/dir":
            case "/api/dir/shallow":
            case "/api/shallow_dir":
            case "/api/dir_shallow":
            case "/api/sysinfo":
            case "/api/socket/recv":
            case "/api/socket/ws":
                await Err(res, 405, "Method POST not supported for this route.");
                return;
            default: await Err(res, 404, $"Unknown route: {path}"); return;
        }
    }

    private static async Task DoPut(HttpListenerRequest req, HttpListenerResponse res,
        string path, State st)
    {
        if (path == "/api/file")
        {
            await HandleOverwriteFile(req, res, st);
            return;
        }
        string[] known = ["/", "/api/help", "/api/dir", "/api/dir/shallow", "/api/shallow_dir", "/api/dir_shallow", "/api/shell",
            "/api/sqlite/create", "/api/sqlite/query", "/api/sysinfo",
            "/api/socket/open", "/api/socket/send", "/api/socket/recv",
            "/api/socket/close", "/api/socket/starttls", "/api/socket/ws"];
        if (known.Contains(path))
        {
            await Err(res, 405, "Method PUT not supported for this route.");
            return;
        }
        await Err(res, 404, $"Unknown route: {path}");
    }

    private static async Task DoDelete(HttpListenerRequest req, HttpListenerResponse res,
        string path, State st)
    {
        if (path == "/api/file")
        {
            await HandleDeleteFile(req, res, st);
            return;
        }
        if (path == "/api/dir")
        {
            await HandleDeleteDir(req, res, st);
            return;
        }
        if (path is "/api/dir/shallow" or "/api/shallow_dir" or "/api/dir_shallow")
        {
            await HandleDeleteDirShallow(req, res, st);
            return;
        }
        string[] known = ["/", "/api/help", "/api/shell",
            "/api/sqlite/create", "/api/sqlite/query", "/api/sysinfo",
            "/api/socket/open", "/api/socket/send", "/api/socket/recv",
            "/api/socket/close", "/api/socket/starttls", "/api/socket/ws"];
        if (known.Contains(path))
        {
            await Err(res, 405, "Method DELETE not supported for this route.");
            return;
        }
        await Err(res, 404, $"Unknown route: {path}");
    }

    // -- read endpoints --------------------------------------------------

    private static async Task HandleReadDir(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (!Lexists(abs))
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        bool isDir;
        try
        {
            isDir = new DirectoryInfo(abs).Attributes.HasFlag(FileAttributes.Directory);
        }
        catch
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        if (!isDir)
        {
            await Err(res, 400, "Path is not a directory", abs);
            return;
        }
        string[] names;
        try
        {
            names = Directory.GetFileSystemEntries(abs)
                .Select(Path.GetFileName).Where(n => n is not null).Cast<string>()
                .OrderBy(n => n, StringComparer.Ordinal).ToArray();
        }
        catch (UnauthorizedAccessException)
        {
            await Err(res, 403, "Permission denied", abs);
            return;
        }
        catch (Exception e)
        {
            await Err(res, 403, $"Cannot list directory: {e.Message}", abs);
            return;
        }
        var entries = new List<object?>();
        foreach (var name in names)
        {
            var full = Path.Combine(abs, name);
            try
            {
                var entryIsDir = new DirectoryInfo(full).Attributes
                    .HasFlag(FileAttributes.Directory);
                long size;
                double? ctime = null;
                double? crtime = null;
                if (entryIsDir)
                {
                    // FileInfo.Length throws on directories: take st_size
                    // from statx where available (Linux), else 0.
                    var stx = Native.StatxAll(full);
                    ctime = stx.Ctime;
                    crtime = stx.Btime;
                    size = stx.Size ?? 0L;
                }
                else
                {
                    var fi = new FileInfo(full);
                    _ = fi.Attributes;
                    size = fi.Length;
                    var stx = Native.StatxAll(full);
                    ctime = stx.Ctime;
                    crtime = stx.Btime;
                }
                if (ctime is null)
                    ctime = EpochSec(File.GetLastWriteTimeUtc(full));
                if (crtime is null)
                    crtime = CreationOrNull(full);
                var at = EpochSec(File.GetLastAccessTimeUtc(full));
                var mt = EpochSec(File.GetLastWriteTimeUtc(full));
                entries.Add(new Dictionary<string, object?>
                {
                    ["name"] = name,
                    ["path"] = full,
                    ["type"] = EntryType(full),
                    ["size"] = Tags.Enc(size),
                    ["atime"] = Tags.Enc(at),
                    ["mtime"] = Tags.Enc(mt),
                    ["ctime"] = Tags.Enc(ctime),
                    ["crtime"] = crtime is null ? "null" : Tags.Enc(crtime),
                    ["mode"] = ModeString(full),
                });
            }
            catch
            {
                continue;
            }
        }
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["path"] = raw,
            ["absolute_path"] = abs,
            ["count"] = Tags.Enc(entries.Count),
            ["entries"] = entries,
        });
    }

    private static double? CreationOrNull(string p)
    {
        try
        {
            var t = File.GetCreationTimeUtc(p);
            var epoch = EpochSec(t);
            return epoch <= 0 ? null : epoch;
        }
        catch
        {
            return null;
        }
    }

    private static async Task HandleReadFile(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (!Lexists(abs))
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        try
        {
            if (Directory.Exists(abs) && new FileInfo(abs).LinkTarget is null)
            {
                await Err(res, 400, "Path is a directory, not a file", abs);
                return;
            }
        }
        catch
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        var encoding = (req.QueryString["encoding"] ?? "auto").ToLowerInvariant();
        if (encoding != "auto" && encoding != "text" && encoding != "base64")
        {
            await Err(res, 400, $"Invalid encoding param: '{encoding}' (use auto|text|base64)");
            return;
        }
        var rawMode = req.QueryString["raw"] == "1";
        byte[] data;
        try
        {
            data = await File.ReadAllBytesAsync(abs);
        }
        catch (FileNotFoundException)
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        catch (DirectoryNotFoundException)
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        catch (UnauthorizedAccessException)
        {
            await Err(res, 403, "Permission denied", abs);
            return;
        }
        catch (IOException e)
        {
            await Err(res, 403, $"Cannot read file: {e.Message}", abs);
            return;
        }
        string mtime;
        try
        {
            mtime = Tags.Enc(EpochSec(File.GetLastWriteTimeUtc(abs)));
        }
        catch
        {
            mtime = "null";
        }
        if (rawMode)
        {
            await SendRaw(res, 200, data, GuessMime(abs));
            return;
        }
        var mime = GuessMime(abs);
        Dictionary<string, object?> Meta(bool binary, string enc, string content) => new()
        {
            ["path"] = raw,
            ["absolute_path"] = abs,
            ["size"] = Tags.Enc((long)data.Length),
            ["mtime"] = mtime,
            ["mime_type"] = mime,
            ["is_binary"] = Tags.Enc(binary),
            ["encoding"] = enc,
            ["content"] = content,
        };
        if (encoding == "text")
        {
            await SendJson(res, 200, Meta(false, "utf-8",
                Encoding.UTF8.GetString(data)));
            return;
        }
        if (encoding == "base64")
        {
            await SendJson(res, 200, Meta(true, "base64",
                Convert.ToBase64String(data)));
            return;
        }
        var text = DecodeStrict(data);
        if (text is not null && !HasBinaryControls(text))
            await SendJson(res, 200, Meta(false, "utf-8", text));
        else
            await SendJson(res, 200, Meta(true, "base64",
                Convert.ToBase64String(data)));
    }

    // -- write/delete endpoints --------------------------------------------

    private sealed record WriteBody(bool Ok, byte[] Data, string Error);

    private static async Task<WriteBody> ParseWriteBody(HttpListenerRequest req)
    {
        var ct = req.Headers["Content-Type"] ?? "";
        using var ms = new MemoryStream();
        await req.InputStream.CopyToAsync(ms);
        var body = ms.ToArray();
        if (!ct.Contains("application/json", StringComparison.OrdinalIgnoreCase))
            return new WriteBody(true, body, "");
        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(body.Length == 0 ? "{}" : Encoding.UTF8.GetString(body));
        }
        catch
        {
            return new WriteBody(false, [], "Invalid JSON body.");
        }
        using (doc)
        {
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
                return new WriteBody(false, [], "JSON body must be an object.");
            var root = doc.RootElement;
            var hasContent = root.TryGetProperty("content", out var content);
            var encoding = "text";
            if (root.TryGetProperty("encoding", out var encEl))
            {
                if (encEl.ValueKind != JsonValueKind.String)
                    return new WriteBody(false, [], "Invalid encoding: (use text|base64).");
                encoding = encEl.GetString()!.ToLowerInvariant();
            }
            if (encoding == "text")
            {
                if (!hasContent || content.ValueKind == JsonValueKind.Null)
                    return new WriteBody(true, [], "");
                if (content.ValueKind != JsonValueKind.String)
                    return new WriteBody(false, [],
                        "'content' must be a string for text encoding.");
                return new WriteBody(true, Encoding.UTF8.GetBytes(content.GetString()!), "");
            }
            if (encoding == "base64")
            {
                if (!hasContent || content.ValueKind == JsonValueKind.Null)
                    return new WriteBody(true, [], "");
                if (content.ValueKind != JsonValueKind.String)
                    return new WriteBody(false, [], "'content' must be a base64 string.");
                try
                {
                    return new WriteBody(true, Convert.FromBase64String(content.GetString()!), "");
                }
                catch (FormatException)
                {
                    return new WriteBody(false, [], "Invalid base64 'content'.");
                }
            }
            return new WriteBody(false, [],
                $"Invalid encoding: '{encoding}' (use text|base64).");
        }
    }

    private static async Task HandleCreateFile(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (Lexists(abs))
        {
            await Err(res, 409, "Path already exists (use PUT to overwrite).", abs);
            return;
        }
        var parent = Path.GetDirectoryName(abs);
        if (parent is null || !Directory.Exists(parent))
        {
            await Err(res, 404, "Parent directory does not exist.", abs);
            return;
        }
        var parsed = await ParseWriteBody(req);
        if (!parsed.Ok)
        {
            await Err(res, 400, parsed.Error);
            return;
        }
        try
        {
            using var fh = new FileStream(abs, FileMode.CreateNew, FileAccess.Write);
            await fh.WriteAsync(parsed.Data);
        }
        catch (IOException)
        {
            if (Lexists(abs))
            {
                await Err(res, 409, "Path already exists (use PUT to overwrite).", abs);
                return;
            }
            await Err(res, 400, "Cannot create file", abs);
            return;
        }
        catch (UnauthorizedAccessException)
        {
            await Err(res, 403, "Permission denied", abs);
            return;
        }
        await SendJson(res, 201, new Dictionary<string, object?>
        {
            ["path"] = raw,
            ["absolute_path"] = abs,
            ["size"] = Tags.Enc((long)parsed.Data.Length),
            ["mime_type"] = GuessMime(abs),
        });
    }

    private static async Task HandleOverwriteFile(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (!Lexists(abs))
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        try
        {
            if (Directory.Exists(abs) && new FileInfo(abs).LinkTarget is null)
            {
                await Err(res, 400, "Path is a directory, not a file", abs);
                return;
            }
        }
        catch
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        var parsed = await ParseWriteBody(req);
        if (!parsed.Ok)
        {
            await Err(res, 400, parsed.Error);
            return;
        }
        try
        {
            await File.WriteAllBytesAsync(abs, parsed.Data);
        }
        catch (UnauthorizedAccessException)
        {
            await Err(res, 403, "Permission denied", abs);
            return;
        }
        catch (FileNotFoundException)
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        catch (IOException e)
        {
            await Err(res, 400, $"Cannot write file: {e.Message}", abs);
            return;
        }
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["path"] = raw,
            ["absolute_path"] = abs,
            ["size"] = Tags.Enc((long)parsed.Data.Length),
            ["mime_type"] = GuessMime(abs),
        });
    }

    private static async Task HandleDeleteFile(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (!Lexists(abs))
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        try
        {
            if (Directory.Exists(abs) && new FileInfo(abs).LinkTarget is null)
            {
                await Err(res, 400,
                    "Path is a directory, not a file (use DELETE /api/dir).", abs);
                return;
            }
        }
        catch
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        try
        {
            File.Delete(abs);
        }
        catch (FileNotFoundException)
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        catch (UnauthorizedAccessException)
        {
            await Err(res, 403, "Permission denied", abs);
            return;
        }
        catch (IOException e)
        {
            await Err(res, 400, $"Cannot delete file: {e.Message}", abs);
            return;
        }
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["path"] = raw, ["absolute_path"] = abs, ["deleted"] = Tags.Enc(true),
        });
    }

    private static async Task HandleDeleteDir(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        var sh = (req.QueryString["shallow"] ?? "").Trim().ToLowerInvariant();
        if (sh is "1" or "true" or "yes" or "on")
        {
            await HandleDeleteDirShallow(req, res, st);
            return;
        }
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (!Lexists(abs))
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        try
        {
            if (new FileInfo(abs).LinkTarget is not null || !Directory.Exists(abs))
            {
                await Err(res, 400, "Path is not a directory", abs);
                return;
            }
        }
        catch
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        try
        {
            Directory.Delete(abs, false);
        }
        catch (DirectoryNotFoundException)
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        catch (UnauthorizedAccessException)
        {
            await Err(res, 403, "Permission denied", abs);
            return;
        }
        catch (IOException)
        {
            if (Lexists(abs))
            {
                string[] left = [];
                try
                {
                    left = Directory.GetFileSystemEntries(abs);
                }
                catch { }
                if (left.Length > 0)
                {
                    await Err(res, 400,
                        $"Cannot delete directory: Directory not empty: '{abs}'", abs);
                    return;
                }
            }
            else
            {
                await Err(res, 404, "Path does not exist", abs);
                return;
            }
            await Err(res, 400, "Cannot delete directory", abs);
            return;
        }
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["path"] = raw, ["absolute_path"] = abs, ["deleted"] = Tags.Enc(true),
        });
    }

    private static async Task HandleDeleteDirShallow(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (!Lexists(abs))
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        try
        {
            if (new FileInfo(abs).LinkTarget is not null || !Directory.Exists(abs))
            {
                await Err(res, 400, "Path is not a directory", abs);
                return;
            }
        }
        catch
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        string[] entries;
        try
        {
            entries = Directory.GetFileSystemEntries(abs);
        }
        catch (Exception e)
        {
            await Err(res, 400, $"Cannot delete directory: {e.Message}", abs);
            return;
        }
        foreach (var entry in entries)
        {
            try
            {
                if (new FileInfo(entry).LinkTarget is null && Directory.Exists(entry))
                {
                    if (Directory.GetFileSystemEntries(entry).Length > 0)
                    {
                        await Err(res, 400,
                            "Directory is not shallow (contains a non-empty subdirectory)", abs);
                        return;
                    }
                }
            }
            catch { }
        }
        try
        {
            foreach (var entry in entries)
            {
                if (new FileInfo(entry).LinkTarget is null && Directory.Exists(entry))
                {
                    Directory.Delete(entry, false);
                }
                else
                {
                    File.Delete(entry);
                }
            }
            Directory.Delete(abs, false);
        }
        catch (DirectoryNotFoundException)
        {
            await Err(res, 404, "Path does not exist", abs);
            return;
        }
        catch (UnauthorizedAccessException)
        {
            await Err(res, 403, "Permission denied", abs);
            return;
        }
        catch (IOException)
        {
            if (Lexists(abs))
            {
                string[] left = [];
                try { left = Directory.GetFileSystemEntries(abs); } catch { }
                if (left.Length > 0)
                {
                    await Err(res, 400,
                        $"Cannot delete directory: Directory not empty: '{abs}'", abs);
                    return;
                }
            }
            else
            {
                await Err(res, 404, "Path does not exist", abs);
                return;
            }
            await Err(res, 400, "Cannot delete directory", abs);
            return;
        }
        catch (Exception e)
        {
            await Err(res, 400, $"Cannot delete directory: {e.Message}", abs);
            return;
        }
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["path"] = raw, ["absolute_path"] = abs, ["deleted"] = Tags.Enc(true),
        });
    }

    // -- shell ------------------------------------------------------------------

    private sealed record Capped(byte[] Data, bool Truncated);

    private static async Task<Capped> DrainCapped(Stream stream, int cap,
        CancellationToken ct)
    {
        using var ms = new MemoryStream();
        var tmp = new byte[65536];
        var truncated = false;
        try
        {
            while (true)
            {
                var n = await stream.ReadAsync(tmp, ct);
                if (n <= 0) break;
                var room = cap - (int)ms.Length;
                if (room > 0) ms.Write(tmp, 0, Math.Min(n, room));
                if (n > Math.Max(room, 0)) truncated = true;
            }
        }
        catch (OperationCanceledException) { }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
        return new Capped(ms.ToArray(), truncated);
    }

    private static async Task HandleExecShell(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        string ct = req.Headers["Content-Type"] ?? "";
        if (!ct.Contains("application/json", StringComparison.OrdinalIgnoreCase))
        {
            await Err(res, 400,
                "Content-Type must be application/json with body { command, timeout?, cwd?, max_stdout?, max_stderr? }.");
            return;
        }
        JsonDocument doc;
        try
        {
            using var ms = new MemoryStream();
            await req.InputStream.CopyToAsync(ms);
            doc = JsonDocument.Parse(ms.Length == 0 ? "{}" : Encoding.UTF8.GetString(ms.ToArray()));
        }
        catch
        {
            await Err(res, 400, "Invalid JSON body.");
            return;
        }
        using (doc)
        {
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
            {
                await Err(res, 400, "JSON body must be an object.");
                return;
            }
            var root = doc.RootElement;
            if (!root.TryGetProperty("command", out var cmdEl) ||
                cmdEl.ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(cmdEl.GetString()))
            {
                await Err(res, 400, "Missing or empty required field: command.");
                return;
            }
            var command = cmdEl.GetString()!;

            var timeout = SHELL_DEFAULT_TIMEOUT;
            if (root.TryGetProperty("timeout", out var tEl) &&
                tEl.ValueKind != JsonValueKind.Null)
            {
                if (tEl.ValueKind != JsonValueKind.String)
                {
                    await Err(res, 400,
                        "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
                    return;
                }
                double t;
                try
                {
                    var v = Tags.Dec(tEl.GetString()!);
                    if (v is not long && v is not double && v is not int)
                        throw new BadTag("x");
                    t = Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture);
                }
                catch
                {
                    await Err(res, 400,
                        "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
                    return;
                }
                timeout = Math.Max(1, Math.Min(t, SHELL_MAX_TIMEOUT));
            }

            var cwd = st.Cwd;
            if (root.TryGetProperty("cwd", out var cwdEl) &&
                cwdEl.ValueKind != JsonValueKind.Null)
            {
                if (cwdEl.ValueKind != JsonValueKind.String)
                {
                    await Err(res, 400, "Field 'cwd' must be a string.");
                    return;
                }
                cwd = ResolvePath(cwdEl.GetString()!, st.Cwd);
                if (!Directory.Exists(cwd))
                {
                    await Err(res, 400, "Working directory does not exist.", cwd);
                    return;
                }
            }

            int? LimitOf(string key)
            {
                if (!root.TryGetProperty(key, out var el) ||
                    el.ValueKind == JsonValueKind.Null)
                    return SHELL_DEFAULT_MAX_OUTPUT;
                if (el.ValueKind != JsonValueKind.String)
                {
                    ErrSync(res, 400,
                        $"Field '{key}' must be an integer >= 0 (i:<int>).");
                    return null;
                }
                object? v;
                try
                {
                    v = Tags.Dec(el.GetString()!);
                }
                catch
                {
                    ErrSync(res, 400,
                        $"Field '{key}' must be an integer >= 0 (i:<int>).");
                    return null;
                }
                if (v is not long && v is not int)
                {
                    ErrSync(res, 400,
                        $"Field '{key}' must be an integer >= 0 (i:<int>).");
                    return null;
                }
                var l = Convert.ToInt64(v);
                if (l < 0)
                {
                    ErrSync(res, 400,
                        $"Field '{key}' must be an integer >= 0 (i:<int>).");
                    return null;
                }
                if (l > SHELL_MAX_OUTPUT)
                {
                    ErrSync(res, 400,
                        $"Field '{key}' exceeds the {SHELL_MAX_OUTPUT} byte cap.");
                    return null;
                }
                return (int)l;
            }

            var maxOut = LimitOf("max_stdout");
            if (maxOut is null) return;
            var maxErr = LimitOf("max_stderr");
            if (maxErr is null) return;

            string stdinRaw = "";
            if (root.TryGetProperty("stdin", out var stdinEl) &&
                stdinEl.ValueKind != JsonValueKind.Null)
            {
                if (stdinEl.ValueKind != JsonValueKind.String)
                {
                    await Err(res, 400, "Field 'stdin' must be a string.");
                    return;
                }
                stdinRaw = stdinEl.GetString()!;
            }
            string stdinEnc = "text";
            if (root.TryGetProperty("stdin_encoding", out var seEl) &&
                seEl.ValueKind != JsonValueKind.Null)
            {
                if (seEl.ValueKind != JsonValueKind.String)
                {
                    await Err(res, 400, "Field 'stdin_encoding' must be text|base64.");
                    return;
                }
                stdinEnc = seEl.GetString()!.ToLowerInvariant();
            }
            byte[] stdinData;
            if (stdinEnc == "text")
            {
                stdinData = Encoding.UTF8.GetBytes(stdinRaw);
            }
            else if (stdinEnc == "base64")
            {
                try
                {
                    stdinData = Convert.FromBase64String(stdinRaw);
                }
                catch (FormatException)
                {
                    await Err(res, 400, "Invalid base64 'stdin'.");
                    return;
                }
            }
            else
            {
                await Err(res, 400, "Field 'stdin_encoding' must be text|base64.");
                return;
            }

            Process proc;
            try
            {
                var psi = new ProcessStartInfo("setsid", "")
                {
                    WorkingDirectory = cwd,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                };
                psi.ArgumentList.Add("bash");
                psi.ArgumentList.Add("-c");
                psi.ArgumentList.Add(command);
                proc = Process.Start(psi)!;
                if (proc is null) throw new InvalidOperationException("start failed");
            }
            catch (Exception e)
            {
                await Err(res, 400, $"Cannot start shell: {e.Message}");
                return;
            }
            using (proc)
            {
                // Feeder task: queued write + close (EOF). A command that
                // never reads cannot block the timeout: killing the group
                // faults the pending write and the task ends.
                var feedTask = FeedStdinAsync(proc, stdinData);
                using var cts = new CancellationTokenSource(
                    TimeSpan.FromSeconds(timeout));
                var outTask = DrainCapped(proc.StandardOutput.BaseStream, maxOut.Value, cts.Token);
                var errTask = DrainCapped(proc.StandardError.BaseStream, maxErr.Value, cts.Token);
                bool timedOut = false;
                int returncode;
                try
                {
                    await proc.WaitForExitAsync(cts.Token);
                    returncode = proc.ExitCode;
                }
                catch (OperationCanceledException)
                {
                    timedOut = true;
                    try
                    {
                        Native.KillGroup(proc.Id);
                    }
                    catch { }
                    try
                    {
                        if (!proc.HasExited) proc.Kill();
                    }
                    catch { }
                    try
                    {
                        await proc.WaitForExitAsync(
                            new CancellationTokenSource(5000).Token);
                    }
                    catch { }
                    returncode = 124;
                }
                Capped outCap, errCap;
                try
                {
                    await Task.WhenAll(outTask, errTask).WaitAsync(TimeSpan.FromSeconds(5));
                    outCap = await outTask;
                    errCap = await errTask;
                }
                catch
                {
                    outCap = new Capped([], true);
                    errCap = new Capped([], true);
                }
                try
                {
                    await feedTask.WaitAsync(TimeSpan.FromSeconds(5));
                }
                catch { }
                await SendJson(res, 200, new Dictionary<string, object?>
                {
                    ["command"] = command,
                    ["cwd"] = cwd,
                    ["returncode"] = Tags.Enc((long)returncode),
                    ["stdout"] = Encoding.UTF8.GetString(outCap.Data),
                    ["stderr"] = Encoding.UTF8.GetString(errCap.Data),
                    ["timed_out"] = Tags.Enc(timedOut),
                    ["stdout_truncated"] = Tags.Enc(outCap.Truncated),
                    ["stderr_truncated"] = Tags.Enc(errCap.Truncated),
                    ["max_stdout"] = Tags.Enc((long)maxOut.Value),
                    ["max_stderr"] = Tags.Enc((long)maxErr.Value),
                    ["stdin_bytes"] = Tags.Enc((long)stdinData.Length),
                });
            }
        }
    }

    private static void ErrSync(HttpListenerResponse res, int status, string message)
    {
        // Synchronous variant for use inside non-async local functions.
        Cors(res);
        res.StatusCode = status;
        res.ContentType = "application/json; charset=utf-8";
        var body = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(
            new Dictionary<string, object?>
            {
                ["error"] = Label(status),
                ["message"] = message,
            }));
        res.ContentLength64 = body.Length;
        res.OutputStream.Write(body, 0, body.Length);
        res.Close();
    }

    /// <summary>
    /// Write input bytes to the child stdin, then close it (EOF).
    /// Never throws: a command that never reads faults the write once
    /// the group is killed, and the task ends.
    /// </summary>
    private static async Task FeedStdinAsync(Process proc, byte[] data)
    {
        try
        {
            await proc.StandardInput.BaseStream.WriteAsync(data, 0, data.Length);
        }
        catch { }
        try
        {
            proc.StandardInput.Close();
        }
        catch { }
    }

    // -- sqlite -------------------------------------------------------------------

    private static bool SqliteAvailable()
    {
        try
        {
            using var conn = new Microsoft.Data.Sqlite.SqliteConnection(
                "Data Source=:memory:;Pooling=False");
            conn.Open();
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static async Task HandleSqliteCreate(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        if (!SqliteAvailable())
        {
            await Err(res, 503, "SQLite support unavailable: driver failed to load.");
            return;
        }
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (Lexists(abs))
        {
            await Err(res, 409, "Database file already exists.", abs);
            return;
        }
        var parent = Path.GetDirectoryName(abs);
        if (parent is null || !Directory.Exists(parent))
        {
            await Err(res, 404, "Parent directory does not exist.", abs);
            return;
        }
        string version;
        try
        {
            // NOTE: Pooling=False is load-bearing: pooled connections
            // survive file deletion and would let create/query succeed
            // against a stale handle (statelessness requires fresh opens).
            using var conn = new Microsoft.Data.Sqlite.SqliteConnection(
                $"Data Source={abs};Pooling=False");
            conn.Open();
            using var cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT sqlite_version()";
            version = (cmd.ExecuteScalar()?.ToString()) ?? "";
        }
        catch (Microsoft.Data.Sqlite.SqliteException e)
        {
            try
            {
                File.Delete(abs);
            }
            catch { }
            await Err(res, 400, $"Cannot create database: {e.Message}", abs);
            return;
        }
        catch (Exception e)
        {
            await Err(res, 403, $"Cannot create database: {e.Message}", abs);
            return;
        }
        long size = 0;
        try
        {
            size = new FileInfo(abs).Length;
        }
        catch { }
        await SendJson(res, 201, new Dictionary<string, object?>
        {
            ["path"] = raw,
            ["absolute_path"] = abs,
            ["size"] = Tags.Enc(size),
            ["sqlite_version"] = version,
        });
    }

    private static async Task HandleSqliteQuery(
        HttpListenerRequest req, HttpListenerResponse res, State st)
    {
        if (!SqliteAvailable())
        {
            await Err(res, 503, "SQLite support unavailable: driver failed to load.");
            return;
        }
        var raw = req.QueryString["path"] ?? "";
        if (raw == "")
        {
            await Err(res, 400, "Missing required query parameter: path");
            return;
        }
        var abs = ResolvePath(raw, st.Cwd);
        if (!Lexists(abs))
        {
            await Err(res, 404, "Database file does not exist", abs);
            return;
        }
        string ct = req.Headers["Content-Type"] ?? "";
        if (!ct.Contains("application/json", StringComparison.OrdinalIgnoreCase))
        {
            await Err(res, 400,
                "Content-Type must be application/json with body { sql, params?, timeout?, readonly? }.");
            return;
        }
        JsonDocument doc;
        try
        {
            using var ms = new MemoryStream();
            await req.InputStream.CopyToAsync(ms);
            doc = JsonDocument.Parse(ms.Length == 0 ? "{}" : Encoding.UTF8.GetString(ms.ToArray()));
        }
        catch
        {
            await Err(res, 400, "Invalid JSON body.");
            return;
        }
        using (doc)
        {
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
            {
                await Err(res, 400, "JSON body must be an object.");
                return;
            }
            var root = doc.RootElement;
            if (!root.TryGetProperty("sql", out var sqlEl) ||
                sqlEl.ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(sqlEl.GetString()))
            {
                await Err(res, 400, "Missing or empty required field: sql.");
                return;
            }
            var sql = sqlEl.GetString()!;
            object? bound;
            if (!root.TryGetProperty("params", out var paramsEl) ||
                paramsEl.ValueKind == JsonValueKind.Null)
            {
                bound = null;
            }
            else if (paramsEl.ValueKind == JsonValueKind.Array)
            {
                var list = new List<object?>();
                foreach (var v in paramsEl.EnumerateArray())
                {
                    if (v.ValueKind != JsonValueKind.String)
                    {
                        await Err(res, 400,
                            "Array params must be tagged scalars (i:/f:/s:/b:/null).");
                        return;
                    }
                    try
                    {
                        list.Add(Tags.Dec(v.GetString()!));
                    }
                    catch
                    {
                        await Err(res, 400,
                            "Array params must be tagged scalars (i:/f:/s:/b:/null).");
                        return;
                    }
                }
                bound = list;
            }
            else if (paramsEl.ValueKind == JsonValueKind.Object)
            {
                var dict = new Dictionary<string, object?>();
                foreach (var kv in paramsEl.EnumerateObject())
                {
                    if (kv.Value.ValueKind != JsonValueKind.String)
                    {
                        await Err(res, 400,
                            "Object params must be tagged scalars (i:/f:/s:/b:/null).");
                        return;
                    }
                    try
                    {
                        dict[kv.Name] = Tags.Dec(kv.Value.GetString()!);
                    }
                    catch
                    {
                        await Err(res, 400,
                            "Object params must be tagged scalars (i:/f:/s:/b:/null).");
                        return;
                    }
                }
                bound = dict;
            }
            else
            {
                await Err(res, 400, "Field 'params' must be an array or an object.");
                return;
            }
            var timeout = SQLITE_DEFAULT_TIMEOUT;
            if (root.TryGetProperty("timeout", out var tEl) &&
                tEl.ValueKind != JsonValueKind.Null)
            {
                if (tEl.ValueKind != JsonValueKind.String)
                {
                    await Err(res, 400,
                        "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
                    return;
                }
                object? t;
                try
                {
                    t = Tags.Dec(tEl.GetString()!);
                }
                catch
                {
                    await Err(res, 400,
                        "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
                    return;
                }
                if (t is not long && t is not int && t is not double && t is not float)
                {
                    await Err(res, 400,
                        "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
                    return;
                }
                var d = Convert.ToDouble(t,
                    System.Globalization.CultureInfo.InvariantCulture);
                if (!(d > 0 && d <= SQLITE_MAX_TIMEOUT))
                {
                    await Err(res, 400, "Field 'timeout' must be within (0, 300].");
                    return;
                }
                timeout = d;
            }
            var readOnly = false;
            if (root.TryGetProperty("readonly", out var rEl) &&
                rEl.ValueKind != JsonValueKind.Null)
            {
                if (rEl.ValueKind != JsonValueKind.String)
                {
                    await Err(res, 400, "Field 'readonly' must be b:true or b:false.");
                    return;
                }
                object? r;
                try
                {
                    r = Tags.Dec(rEl.GetString()!);
                }
                catch
                {
                    await Err(res, 400, "Field 'readonly' must be b:true or b:false.");
                    return;
                }
                if (r is not bool)
                {
                    await Err(res, 400, "Field 'readonly' must be b:true or b:false.");
                    return;
                }
                readOnly = (bool)r;
            }

            Sql.Scan scan;
            try
            {
                scan = Sql.ScanSql(sql);
            }
            catch (Exception e)
            {
                await Err(res, 400, e.Message, abs);
                return;
            }
            if (scan.Statements.Count != 1)
            {
                await Err(res, 400, "Send exactly one SQL statement per request.", abs);
                return;
            }
            // Native binding: Microsoft.Data.Sqlite takes named params
            // only, so positional `?` are rewritten to `$pN` (tokens
            // only, never values); `:named` pass through untouched.
            string finalSql;
            bool isNamed = bound is Dictionary<string, object?>;
            try
            {
                (finalSql, _) = Sql.RewritePlaceholders(scan.Statements[0], isNamed);
            }
            catch (Exception e)
            {
                await Err(res, 400, e.Message, abs);
                return;
            }
            var rowsMode = Sql.RowFirst.Contains(scan.First) || scan.Returning;
            Dictionary<string, object?> payload;
            try
            {
                var csb = new Microsoft.Data.Sqlite.SqliteConnectionStringBuilder
                {
                    DataSource = abs,
                    Pooling = false,
                    Mode = readOnly
                        ? Microsoft.Data.Sqlite.SqliteOpenMode.ReadOnly
                        : Microsoft.Data.Sqlite.SqliteOpenMode.ReadWriteCreate,
                };
                using var conn = new Microsoft.Data.Sqlite.SqliteConnection(
                    csb.ToString());
                conn.Open();
                using (var pragma = conn.CreateCommand())
                {
                    pragma.CommandText = $"PRAGMA busy_timeout={(int)(timeout * 1000)}";
                    pragma.ExecuteNonQuery();
                }
                if (rowsMode)
                {
                    using var cmd = conn.CreateCommand();
                    cmd.CommandText = finalSql;
                    BindDbParams(cmd, bound);
                    using var reader = cmd.ExecuteReader();
                    var columns = new List<object?>();
                    for (var i = 0; i < reader.FieldCount; i++)
                        columns.Add(reader.GetName(i));
                    var rows = new List<object?>();
                    while (reader.Read())
                    {
                        var row = new List<object?>();
                        for (var i = 0; i < reader.FieldCount; i++)
                            row.Add(CellToTag(reader.GetValue(i)));
                        rows.Add(row);
                    }
                    payload = new Dictionary<string, object?>
                    {
                        ["columns"] = columns,
                        ["rows"] = rows,
                        ["rowcount"] = Tags.Enc((long)rows.Count),
                    };
                }
                else
                {
                    int affected;
                    using (var cmd = conn.CreateCommand())
                    {
                        cmd.CommandText = finalSql;
                        BindDbParams(cmd, bound);
                        affected = cmd.ExecuteNonQuery();
                    }
                    long lastId;
                    using (var cmd = conn.CreateCommand())
                    {
                        cmd.CommandText = "SELECT last_insert_rowid()";
                        lastId = (long)(cmd.ExecuteScalar() ?? 0L);
                    }
                    var rc = Sql.DdlFirst.Contains(scan.First) ? -1L : (long)affected;
                    payload = new Dictionary<string, object?>
                    {
                        ["rowcount"] = Tags.Enc(rc),
                        ["lastrowid"] = Tags.Enc(lastId),
                    };
                }
            }
            catch (Microsoft.Data.Sqlite.SqliteException e)
            {
                await Err(res, 400, $"SQLite error: {e.Message}", abs);
                return;
            }
            payload["path"] = raw;
            payload["absolute_path"] = abs;
            await SendJson(res, 200, payload);
        }
    }

    private static string CellToTag(object? v) => v switch
    {
        null or DBNull => "null",
        bool b => Tags.Enc(b),
        long l => Tags.Enc(l),
        int i => Tags.Enc((long)i),
        double d => Tags.Enc(d),
        float f => Tags.Enc((double)f),
        byte[] bytes => Tags.TagStr(Convert.ToBase64String(bytes)),
        string s => Tags.TagStr(s),
        _ => Tags.TagStr(v.ToString() ?? ""),
    };

    /// <summary>Attach decoded tag values to a command natively.</summary>
    private static void BindDbParams(
        Microsoft.Data.Sqlite.SqliteCommand cmd, object? bound)
    {
        if (bound is List<object?> list)
        {
            for (var i = 0; i < list.Count; i++)
                cmd.Parameters.AddWithValue("$p" + (i + 1), Sql.ToDb(list[i]));
        }
        else if (bound is Dictionary<string, object?> dict)
        {
            foreach (var (name, value) in dict)
            {
                var pname = name.StartsWith(':') || name.StartsWith('@') ||
                    name.StartsWith('$') ? name : ":" + name;
                cmd.Parameters.AddWithValue(pname, Sql.ToDb(value));
            }
        }
        // Missing named params surface as driver errors (400 below);
        // extra values are ignored by the driver.
    }

    // -- sysinfo --------------------------------------------------------------------

    private static Dictionary<string, string> ReadOsRelease()
    {
        var info = new Dictionary<string, string>();
        try
        {
            foreach (var line in File.ReadLines("/etc/os-release"))
            {
                var t = line.Trim();
                if (t == "" || t.StartsWith('#') || !t.Contains('=')) continue;
                var eq = t.IndexOf('=');
                info[t[..eq]] = t[(eq + 1)..].Trim().Trim('"', '\'');
            }
        }
        catch { }
        return info;
    }

    private static async Task HandleSysinfo(HttpListenerResponse res)
    {
        var osDesc = System.Runtime.InteropServices.RuntimeInformation.OSDescription;
        var system = OperatingSystem.IsWindows() ? "Windows"
            : OperatingSystem.IsMacOS() ? "Darwin" : "Linux";
        object? distro = "null";
        object? windows = "null";
        object? mac = "null";
        if (system == "Linux")
        {
            var rel = ReadOsRelease();
            if (rel.Count > 0)
            {
                distro = new Dictionary<string, object?>
                {
                    ["id"] = rel.GetValueOrDefault("ID", ""),
                    ["name"] = rel.GetValueOrDefault("NAME", ""),
                    ["version"] = rel.GetValueOrDefault("VERSION", ""),
                    ["version_id"] = rel.GetValueOrDefault("VERSION_ID", ""),
                    ["pretty_name"] = rel.GetValueOrDefault("PRETTY_NAME", ""),
                };
            }
        }
        else if (system == "Windows")
        {
            windows = new Dictionary<string, object?>
            {
                ["release"] = osDesc,
                ["version"] = Environment.OSVersion.VersionString,
                ["service_pack"] = "",
                ["edition"] = "",
            };
        }
        else if (system == "Darwin")
        {
            mac = new Dictionary<string, object?>
            {
                ["version"] = Environment.OSVersion.VersionString,
            };
        }
        string release, version;
        try
        {
            release = File.ReadAllText("/proc/sys/kernel/osrelease").Trim();
        }
        catch
        {
            release = osDesc;
        }
        try
        {
            version = File.ReadAllText("/proc/sys/kernel/version").Trim();
        }
        catch
        {
            version = osDesc;
        }
        var arch = System.Runtime.InteropServices.RuntimeInformation.OSArchitecture.ToString();
        var machine = arch == "X64" ? "x64" : arch;
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["system"] = system,
            ["system_name"] = system == "Darwin" ? "macOS" : system,
            ["release"] = release,
            ["version"] = version,
            ["machine"] = machine,
            ["processor"] = "",
            ["hostname"] = Environment.MachineName,
            ["dotnet_version"] = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
            ["cpu_count"] = Tags.Enc((long)Environment.ProcessorCount),
            ["distro"] = distro,
            ["windows"] = windows,
            ["mac"] = mac,
        });
    }

    // -- socket proxy -----------------------------------------------------------------

    public const double SOCK_DEFAULT_CONNECT_TIMEOUT = 5;
    public const double SOCK_MAX_CONNECT_TIMEOUT = 30;
    public const double SOCK_DEFAULT_RECV_TIMEOUT = 5;
    public const double SOCK_MAX_RECV_TIMEOUT = 60;
    public const int SOCK_DEFAULT_MAX_BYTES = 65536;
    public const int SOCK_MAX_BYTES = 1024 * 1024;
    public const int SOCK_MAX_SESSIONS = 128;
    public const int SOCK_IDLE_SECS = 300;

    private sealed class SockSession
    {
        public TcpClient Client = null!;
        public string Host = "";
        public long Port;
        // "tcp" or "tls": TLS is a transport property of the session,
        // negotiated at open (implicit) or via starttls, and verified
        // against the system roots (+ optional ca_pem) with hostname
        // check and SNI. send/recv/close/WS see cleartext.
        public string Kind = "tcp";
        public System.Net.Security.SslStream? Tls;
        public DateTime Last = DateTime.UtcNow;
        public bool Eof;
        public bool RecvBusy;
        // wsAttached marks a live WebSocket bridge: the idle sweeper
        // skips it and HTTP send/recv are refused while set.
        public bool WsAttached;
    }

    private static System.IO.Stream SockStream(SockSession sess) =>
        sess.Tls is not null ? sess.Tls : sess.Client.GetStream();

    private static void SockCloseAll(SockSession sess)
    {
        try { sess.Tls?.Dispose(); } catch { }
        try { sess.Client.Close(); } catch { }
    }

    private static readonly Dictionary<string, SockSession> SockSessions = new();
    private static readonly object SockLock = new();
    private static int SockSweeper;

    private static void SockEnsureSweeper()
    {
        if (Interlocked.CompareExchange(ref SockSweeper, 1, 0) != 0) return;
        var timer = new System.Threading.Timer(_ =>
        {
            lock (SockLock)
            {
                var now = DateTime.UtcNow;
                foreach (var sid in SockSessions.Keys.ToList())
                    if (!SockSessions[sid].WsAttached &&
                        (now - SockSessions[sid].Last).TotalSeconds > SOCK_IDLE_SECS)
                    {
                        try { SockCloseAll(SockSessions[sid]); } catch { }
                        SockSessions.Remove(sid);
                    }
            }
        }, null, TimeSpan.FromSeconds(60), TimeSpan.FromSeconds(60));
        GC.KeepAlive(timer);
    }

    private static string SockMintId()
    {
        var bytes = new byte[16];
        System.Security.Cryptography.RandomNumberGenerator.Fill(bytes);
        return Convert.ToBase64String(bytes).TrimEnd('=')
            .Replace('+', '-').Replace('/', '_');
    }

    private static bool SockLookup(HttpListenerRequest req,
        HttpListenerResponse res, out string sid, out SockSession? sess)
    {
        sid = "";
        sess = null;
        var id = req.QueryString["id"];
        if (string.IsNullOrEmpty(id))
        {
            Err(res, 400, "Missing required query parameter: id").Wait();
            return false;
        }
        lock (SockLock)
        {
            if (!SockSessions.TryGetValue(id, out sess))
            {
                sess = null;
                Err(res, 404, $"Unknown socket session: {id}").Wait();
                return false;
            }
            sess.Last = DateTime.UtcNow;
            sid = id;
            return true;
        }
    }

    private static async Task<JsonDocument?> SockReqObj(
        HttpListenerRequest req, HttpListenerResponse res, byte[] body)
    {
        var ct = req.Headers["Content-Type"] ?? "";
        if (!ct.Contains("application/json", StringComparison.OrdinalIgnoreCase))
        {
            await Err(res, 400,
                "Content-Type must be application/json with body { host, port, timeout? }.");
            return null;
        }
        try
        {
            var doc = JsonDocument.Parse(body.Length == 0 ? "{}" : Encoding.UTF8.GetString(body));
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
            {
                doc.Dispose();
                await Err(res, 400, "JSON body must be an object.");
                return null;
            }
            return doc;
        }
        catch
        {
            await Err(res, 400, "Invalid JSON body.");
            return null;
        }
    }

    private static (string Sni, string CaPem, string? Error) ParseTlsIdentity(
        JsonElement root)
    {
        var sni = "";
        if (root.TryGetProperty("sni", out var sniEl) &&
            sniEl.ValueKind != JsonValueKind.Null)
        {
            if (sniEl.ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(sniEl.GetString()))
                return ("", "", "Field 'sni' must be a non-empty string.");
            sni = sniEl.GetString()!.Trim();
        }
        var caPem = "";
        if (root.TryGetProperty("ca_pem", out var caEl) &&
            caEl.ValueKind != JsonValueKind.Null)
        {
            if (caEl.ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(caEl.GetString()))
                return ("", "", "Field 'ca_pem' must be a PEM CA bundle string.");
            caPem = caEl.GetString()!;
        }
        return (sni, caPem, null);
    }

    private static System.Security.Cryptography.X509Certificates.X509Certificate2Collection?
        ParseExtraCerts(string caPem, out string? error)
    {
        error = null;
        var col = new System.Security.Cryptography.X509Certificates.X509Certificate2Collection();
        if (caPem == "") return col;
        try
        {
            col.Import(Encoding.ASCII.GetBytes(caPem));
            if (col.Count == 0)
            {
                error = "Field 'ca_pem' must be a valid PEM CA bundle.";
                return null;
            }
            return col;
        }
        catch
        {
            error = "Field 'ca_pem' must be a valid PEM CA bundle.";
            return null;
        }
    }

    private static bool SockMatchDns(string pattern, string name)
    {
        if (pattern.StartsWith("*.", StringComparison.Ordinal))
        {
            var suffix = pattern[2..];
            if (suffix.Length == 0) return false;
            if (!name.EndsWith("." + suffix, StringComparison.OrdinalIgnoreCase))
                return false;
            var left = name[..^(suffix.Length + 1)];
            return left.Length > 0 && !left.Contains('.');
        }
        return pattern.Equals(name, StringComparison.OrdinalIgnoreCase);
    }

    private static (List<string> Dns, List<System.Net.IPAddress> Ips) SockParseSan(
        byte[] raw)
    {
        var dns = new List<string>();
        var ips = new List<System.Net.IPAddress>();
        try
        {
            var i = 0;
            // RawData may arrive OCTET-STRING-wrapped; unwrap once.
            if (raw[i] == 0x04)
            {
                i++;
                int l0 = raw[i++];
                if ((l0 & 0x80) != 0)
                {
                    var n0 = l0 & 0x7F;
                    l0 = 0;
                    for (var k = 0; k < n0; k++) l0 = (l0 << 8) | raw[i++];
                }
            }
            if (raw[i++] != 0x30) return (dns, ips);
            int len = raw[i++];
            if ((len & 0x80) != 0)
            {
                var n = len & 0x7F;
                len = 0;
                for (var k = 0; k < n; k++) len = (len << 8) | raw[i++];
            }
            var end = i + len;
            while (i < end)
            {
                var tag = raw[i++] & 0x1F; // context-specific GeneralName number
                int l = raw[i++];
                if ((l & 0x80) != 0)
                {
                    var n = l & 0x7F;
                    l = 0;
                    for (var k = 0; k < n; k++) l = (l << 8) | raw[i++];
                }
                if (i + l > raw.Length) break;
                if (tag == 2) dns.Add(Encoding.ASCII.GetString(raw, i, l));
                else if (tag == 7 && (l == 4 || l == 16))
                    ips.Add(new System.Net.IPAddress(raw[i..(i + l)]));
                i += l;
            }
        }
        catch { }
        return (dns, ips);
    }

    private static bool SockCheckHostname(
        string sni, System.Security.Cryptography.X509Certificates.X509Certificate2 leaf)
    {
        var san = leaf.Extensions["2.5.29.17"];
        if (System.Net.IPAddress.TryParse(sni, out var wantIp))
        {
            if (san is null) return false;
            foreach (var ip in SockParseSan(san.RawData).Ips)
                if (ip.Equals(wantIp)) return true;
            return false;
        }
        var dns = san is null ? new List<string>() : SockParseSan(san.RawData).Dns;
        if (dns.Count > 0)
        {
            foreach (var d in dns)
                if (SockMatchDns(d, sni)) return true;
            return false;
        }
        foreach (var part in leaf.Subject.Split(','))
        {
            var t = part.Trim();
            if (t.StartsWith("CN=", StringComparison.OrdinalIgnoreCase))
                return SockMatchDns(t[3..], sni);
        }
        return false;
    }

    private static System.Net.Security.RemoteCertificateValidationCallback SockValidator(
        string sni,
        System.Security.Cryptography.X509Certificates.X509Certificate2Collection extra)
    {
        return (sender, cert, sysChain, errors) =>
        {
            if (cert is null) return false;
            if (errors == System.Net.Security.SslPolicyErrors.None) return true;
            try
            {
                var leaf = cert as System.Security.Cryptography.X509Certificates.X509Certificate2
                    ?? new System.Security.Cryptography.X509Certificates.X509Certificate2(
                        cert.Export(System.Security.Cryptography.X509Certificates.X509ContentType.Cert));
                using var chain = new System.Security.Cryptography.X509Certificates.X509Chain();
                chain.ChainPolicy.TrustMode =
                    System.Security.Cryptography.X509Certificates.X509ChainTrustMode.CustomRootTrust;
                chain.ChainPolicy.RevocationMode =
                    System.Security.Cryptography.X509Certificates.X509RevocationMode.NoCheck;
                try
                {
                    using var store = new System.Security.Cryptography.X509Certificates.X509Store(
                        System.Security.Cryptography.X509Certificates.StoreName.Root,
                        System.Security.Cryptography.X509Certificates.StoreLocation.LocalMachine);
                    store.Open(System.Security.Cryptography.X509Certificates.OpenFlags.ReadOnly);
                    chain.ChainPolicy.CustomTrustStore.AddRange(store.Certificates);
                }
                catch { }
                chain.ChainPolicy.CustomTrustStore.AddRange(extra);
                if (sysChain is not null)
                    foreach (var el in sysChain.ChainElements)
                        chain.ChainPolicy.ExtraStore.Add(el.Certificate);
                if (!chain.Build(leaf)) return false;
                return SockCheckHostname(sni, leaf);
            }
            catch { return false; }
        };
    }

    private static async Task<System.Net.Security.SslStream> SockTlsHandshakeAsync(
        System.IO.Stream inner, string sni,
        System.Security.Cryptography.X509Certificates.X509Certificate2Collection extra,
        double timeoutSec, bool leaveOpen)
    {
        var ssl = new System.Net.Security.SslStream(
            inner, leaveOpen, SockValidator(sni, extra));
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSec));
        try
        {
            await ssl.AuthenticateAsClientAsync(
                new System.Net.Security.SslClientAuthenticationOptions
                {
                    TargetHost = sni,
                    EnabledSslProtocols = System.Security.Authentication.SslProtocols.Tls12 |
                        System.Security.Authentication.SslProtocols.Tls13,
                    CertificateRevocationCheckMode =
                        System.Security.Cryptography.X509Certificates.X509RevocationMode.NoCheck,
                }, cts.Token);
            return ssl;
        }
        catch
        {
            try { ssl.Dispose(); } catch { }
            throw;
        }
    }

    private static async Task HandleSocketOpen(
        HttpListenerRequest req, HttpListenerResponse res)
    {
        SockEnsureSweeper();
        var body = await ReadBody(req);
        using var doc = await SockReqObj(req, res, body);
        if (doc is null) return;
        var root = doc.RootElement;
        if (!root.TryGetProperty("host", out var hostEl) ||
            hostEl.ValueKind != JsonValueKind.String ||
            string.IsNullOrWhiteSpace(hostEl.GetString()))
        {
            await Err(res, 400, "Missing or empty required field: host.");
            return;
        }
        var host = hostEl.GetString()!;
        if (!root.TryGetProperty("port", out var portEl) ||
            portEl.ValueKind == JsonValueKind.Null)
        {
            await Err(res, 400, "Missing required field: port (i:<1-65535>).");
            return;
        }
        long port;
        if (portEl.ValueKind != JsonValueKind.String ||
            !portEl.GetString()!.StartsWith("i:", StringComparison.Ordinal))
        {
            await Err(res, 400, "Field 'port' must be an integer 1-65535 (i:<int>).");
            return;
        }
        try
        {
            var v = Tags.Dec(portEl.GetString()!);
            if (v is not long l) throw new BadTag("x");
            port = l;
        }
        catch
        {
            await Err(res, 400, "Field 'port' must be an integer 1-65535 (i:<int>).");
            return;
        }
        if (port < 1 || port > 65535)
        {
            await Err(res, 400, "Field 'port' must be an integer 1-65535 (i:<int>).");
            return;
        }
        var timeout = SOCK_DEFAULT_CONNECT_TIMEOUT;
        if (root.TryGetProperty("timeout", out var tEl) &&
            tEl.ValueKind != JsonValueKind.Null)
        {
            if (tEl.ValueKind != JsonValueKind.String)
            {
                await Err(res, 400,
                    "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
                return;
            }
            try
            {
                var v = Tags.Dec(tEl.GetString()!);
                timeout = v switch
                {
                    long l => (double)l,
                    double d => d,
                    int i => (double)i,
                    _ => throw new BadTag("x"),
                };
            }
            catch
            {
                await Err(res, 400,
                    "Field 'timeout' must be a tagged number (i:<int> or f:<float>).");
                return;
            }
        }
        if (!(timeout > 0 && timeout <= SOCK_MAX_CONNECT_TIMEOUT))
        {
            await Err(res, 400,
                $"Field 'timeout' must be within (0, {SOCK_MAX_CONNECT_TIMEOUT}].");
            return;
        }
        var tlsMode = false;
        if (root.TryGetProperty("tls", out var tlsEl) &&
            tlsEl.ValueKind != JsonValueKind.Null)
        {
            if (tlsEl.ValueKind != JsonValueKind.String)
            {
                await Err(res, 400, "Field 'tls' must be b:true or b:false.");
                return;
            }
            object? t;
            try { t = Tags.Dec(tlsEl.GetString()!); }
            catch
            {
                await Err(res, 400, "Field 'tls' must be b:true or b:false.");
                return;
            }
            if (t is not bool b)
            {
                await Err(res, 400, "Field 'tls' must be b:true or b:false.");
                return;
            }
            tlsMode = b;
        }
        var (sni0, caPem, identErr) = ParseTlsIdentity(root);
        if (identErr is not null)
        {
            await Err(res, 400, identErr);
            return;
        }
        var sni = sni0 == "" ? host : sni0;
        var extra = ParseExtraCerts(caPem, out var caErr);
        if (caErr is not null)
        {
            await Err(res, 400, caErr);
            return;
        }
        lock (SockLock)
        {
            var now = DateTime.UtcNow;
            foreach (var sid in SockSessions.Keys.ToList())
                if ((now - SockSessions[sid].Last).TotalSeconds > SOCK_IDLE_SECS)
                {
                    try { SockCloseAll(SockSessions[sid]); } catch { }
                    SockSessions.Remove(sid);
                }
            if (SockSessions.Count >= SOCK_MAX_SESSIONS)
            {
                Err(res, 400, $"Too many open socket sessions ({SOCK_MAX_SESSIONS}).").Wait();
                return;
            }
        }
        var client = new TcpClient();
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeout));
            await client.ConnectAsync(host, (int)port, cts.Token);
        }
        catch (Exception e)
        {
            try { client.Close(); } catch { }
            await Err(res, 400, $"Cannot connect to {host}:{port}: {e.Message}");
            return;
        }
        var kind = "tcp";
        System.Net.Security.SslStream? tls = null;
        if (tlsMode)
        {
            try
            {
                tls = await SockTlsHandshakeAsync(
                    client.GetStream(), sni, extra!, timeout, false);
            }
            catch (Exception e)
            {
                try { client.Close(); } catch { }
                await Err(res, 400, $"TLS handshake with {host}:{port}: {e.Message}");
                return;
            }
            kind = "tls";
        }
        var sid2 = SockMintId();
        lock (SockLock)
            SockSessions[sid2] = new SockSession
            {
                Client = client, Host = host, Port = port, Kind = kind,
                Tls = tls, Last = DateTime.UtcNow,
            };
        await SendJson(res, 201, new Dictionary<string, object?>
        {
            ["id"] = sid2, ["kind"] = kind, ["host"] = host,
            ["port"] = Tags.Enc(port),
        });
    }

    private static bool SockStrictB64(string s)
    {
        if (s.Length % 4 != 0) return false;
        foreach (var c in s)
            if (!(c >= 'A' && c <= 'Z') && !(c >= 'a' && c <= 'z') &&
                !(c >= '0' && c <= '9') && c != '+' && c != '/' && c != '=')
                return false;
        return true;
    }

    private static async Task HandleSocketSend(
        HttpListenerRequest req, HttpListenerResponse res)
    {
        if (!SockLookup(req, res, out var sid, out var sess)) return;
        lock (SockLock)
        {
            if (sess!.WsAttached)
            {
                Err(res, 400, "Socket session is attached to a websocket (HTTP send refused while attached).").Wait();
                return;
            }
        }
        var body = await ReadBody(req);
        var ct = req.Headers["Content-Type"] ?? "";
        byte[] data;
        if (ct.Contains("application/json", StringComparison.OrdinalIgnoreCase))
        {
            JsonDocument doc;
            try
            {
                doc = JsonDocument.Parse(
                    body.Length == 0 ? "{}" : Encoding.UTF8.GetString(body));
            }
            catch
            {
                await Err(res, 400, "Invalid JSON body.");
                return;
            }
            using (doc)
            {
                if (doc.RootElement.ValueKind != JsonValueKind.Object)
                {
                    await Err(res, 400, "JSON body must be an object.");
                    return;
                }
                var root = doc.RootElement;
                var content = "";
                if (root.TryGetProperty("content", out var cEl) &&
                    cEl.ValueKind != JsonValueKind.Null)
                {
                    if (cEl.ValueKind != JsonValueKind.String)
                    {
                        await Err(res, 400, "'content' must be a string.");
                        return;
                    }
                    content = cEl.GetString()!;
                }
                var encoding = "text";
                if (root.TryGetProperty("encoding", out var eEl) &&
                    eEl.ValueKind != JsonValueKind.Null)
                {
                    if (eEl.ValueKind != JsonValueKind.String)
                    {
                        await Err(res, 400, "Invalid encoding: (use text|base64).");
                        return;
                    }
                    encoding = eEl.GetString()!.ToLowerInvariant();
                }
                if (encoding == "text") data = Encoding.UTF8.GetBytes(content);
                else if (encoding == "base64")
                {
                    if (!SockStrictB64(content))
                    {
                        await Err(res, 400, "Invalid base64 'content'.");
                        return;
                    }
                    try { data = Convert.FromBase64String(content); }
                    catch
                    {
                        await Err(res, 400, "Invalid base64 'content'.");
                        return;
                    }
                }
                else
                {
                    await Err(res, 400,
                        $"Invalid encoding: '{encoding}' (use text|base64).");
                    return;
                }
            }
        }
        else data = body;
        try
        {
            var stream = SockStream(sess!);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
            await stream.WriteAsync(data, cts.Token);
        }
        catch (Exception e)
        {
            await Err(res, 400, $"Socket error: {e.Message}");
            return;
        }
        lock (SockLock)
            if (SockSessions.TryGetValue(sid, out var s2)) s2.Last = DateTime.UtcNow;
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["id"] = sid, ["bytes"] = Tags.Enc((long)data.Length),
        });
    }

    private static async Task HandleSocketRecv(
        HttpListenerRequest req, HttpListenerResponse res)
    {
        var timeout = SOCK_DEFAULT_RECV_TIMEOUT;
        var timeoutRaw = req.QueryString["timeout"];
        if (!string.IsNullOrEmpty(timeoutRaw))
        {
            if (!double.TryParse(timeoutRaw,
                    System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture,
                    out timeout) ||
                !(timeout > 0 && timeout <= SOCK_MAX_RECV_TIMEOUT))
            {
                await Err(res, 400,
                    $"Invalid timeout param (seconds within (0, {SOCK_MAX_RECV_TIMEOUT}]).");
                return;
            }
        }
        var maxBytes = SOCK_DEFAULT_MAX_BYTES;
        var maxRaw = req.QueryString["max_bytes"];
        if (!string.IsNullOrEmpty(maxRaw))
        {
            if (!int.TryParse(maxRaw,
                    System.Globalization.NumberStyles.Integer,
                    System.Globalization.CultureInfo.InvariantCulture,
                    out maxBytes) ||
                maxBytes < 1 || maxBytes > SOCK_MAX_BYTES)
            {
                await Err(res, 400,
                    $"Invalid max_bytes param (1-{SOCK_MAX_BYTES}).");
                return;
            }
        }
        if (!SockLookup(req, res, out var sid, out var sess)) return;
        lock (SockLock)
        {
            if (sess!.WsAttached)
            {
                Err(res, 400, "Socket session is attached to a websocket (HTTP recv refused while attached).").Wait();
                return;
            }
            if (sess!.Eof)
            {
                SendJson(res, 200, new Dictionary<string, object?>
                {
                    ["id"] = sid, ["encoding"] = "base64", ["content"] = "",
                    ["bytes"] = Tags.Enc(0L), ["eof"] = Tags.Enc(true),
                    ["timed_out"] = Tags.Enc(false),
                }).Wait();
                return;
            }
            if (sess.RecvBusy)
            {
                Err(res, 400, "Another recv is already pending on this session.").Wait();
                return;
            }
            sess.RecvBusy = true;
        }
        var buf = new byte[maxBytes];
        int n;
        string? failure = null;
        var eof = false;
        var timedOut = false;
        try
        {
            var stream = SockStream(sess!);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeout));
            n = await stream.ReadAsync(buf, cts.Token);
            if (n == 0) eof = true;
        }
        catch (OperationCanceledException)
        {
            n = 0;
            timedOut = true;
        }
        catch (Exception e)
        {
            n = 0;
            failure = e.Message;
        }
        lock (SockLock)
        {
            if (SockSessions.TryGetValue(sid, out var s2))
            {
                s2.RecvBusy = false;
                s2.Last = DateTime.UtcNow;
                if (eof) s2.Eof = true;
            }
        }
        if (failure is not null)
        {
            await Err(res, 400, $"Socket error: {failure}");
            return;
        }
        if (eof)
        {
            await SendJson(res, 200, new Dictionary<string, object?>
            {
                ["id"] = sid, ["encoding"] = "base64", ["content"] = "",
                ["bytes"] = Tags.Enc(0L), ["eof"] = Tags.Enc(true),
                ["timed_out"] = Tags.Enc(false),
            });
            return;
        }
        if (timedOut)
        {
            await SendJson(res, 200, new Dictionary<string, object?>
            {
                ["id"] = sid, ["encoding"] = "base64", ["content"] = "",
                ["bytes"] = Tags.Enc(0L), ["eof"] = Tags.Enc(false),
                ["timed_out"] = Tags.Enc(true),
            });
            return;
        }
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["id"] = sid, ["encoding"] = "base64",
            ["content"] = Convert.ToBase64String(buf, 0, n),
            ["bytes"] = Tags.Enc((long)n), ["eof"] = Tags.Enc(false),
            ["timed_out"] = Tags.Enc(false),
        });
    }

    private static async Task HandleSocketClose(
        HttpListenerRequest req, HttpListenerResponse res)
    {
        if (!SockLookup(req, res, out var sid, out var sess)) return;
        lock (SockLock) SockSessions.Remove(sid);
        try { SockCloseAll(sess!); } catch { }
        await SendJson(res, 200, new Dictionary<string, object?>
        {
            ["id"] = sid, ["closed"] = Tags.Enc(true),
        });
    }

    private static async Task HandleSocketStarttls(
        HttpListenerRequest req, HttpListenerResponse res)
    {
        if (!SockLookup(req, res, out var sid, out var sess)) return;
        lock (SockLock)
        {
            if (sess!.WsAttached)
            {
                Err(res, 400, "Socket session is attached to a websocket (STARTTLS refused while attached).").Wait();
                return;
            }
            if (sess.Kind == "tls")
            {
                Err(res, 400, "Socket session is already TLS.").Wait();
                return;
            }
            if (sess.RecvBusy)
            {
                Err(res, 400, "Another recv is already pending on this session.").Wait();
                return;
            }
            sess.RecvBusy = true;
        }
        try
        {
            var body = await ReadBody(req);
            JsonElement root = default;
            var hasBody = body.Length != 0;
            if (hasBody)
            {
                var ct = req.Headers["Content-Type"] ?? "";
                if (!ct.Contains("application/json", StringComparison.OrdinalIgnoreCase))
                {
                    await Err(res, 400,
                        "Content-Type must be application/json with body { sni?, ca_pem? }.");
                    return;
                }
                JsonDocument doc;
                try
                {
                    doc = JsonDocument.Parse(Encoding.UTF8.GetString(body));
                }
                catch
                {
                    await Err(res, 400, "Invalid JSON body.");
                    return;
                }
                using (doc)
                {
                    if (doc.RootElement.ValueKind != JsonValueKind.Object)
                    {
                        await Err(res, 400, "JSON body must be an object.");
                        return;
                    }
                    root = doc.RootElement.Clone();
                }
            }
            else
            {
                using var empty = JsonDocument.Parse("{}");
                root = empty.RootElement.Clone();
            }
            var (sni0, caPem, identErr) = ParseTlsIdentity(root);
            if (identErr is not null)
            {
                await Err(res, 400, identErr);
                return;
            }
            var sni = sni0 == "" ? sess!.Host : sni0;
            var extra = ParseExtraCerts(caPem, out var caErr);
            if (caErr is not null)
            {
                await Err(res, 400, caErr);
                return;
            }
            System.Net.Security.SslStream tls;
            try
            {
                tls = await SockTlsHandshakeAsync(
                    sess!.Client.GetStream(), sni, extra!,
                    SOCK_MAX_CONNECT_TIMEOUT, true);
            }
            catch (Exception e)
            {
                await Err(res, 400, $"TLS handshake with {sess!.Host}:{sess.Port}: {e.Message}");
                return;
            }
            lock (SockLock)
            {
                if (SockSessions.TryGetValue(sid, out var cur) && cur == sess)
                {
                    sess.Tls = tls;
                    sess.Kind = "tls";
                    sess.Last = DateTime.UtcNow;
                }
                else
                {
                    try { tls.Dispose(); } catch { }
                }
            }
            await SendJson(res, 200, new Dictionary<string, object?>
            {
                ["id"] = sid, ["kind"] = "tls",
            });
        }
        finally
        {
            lock (SockLock)
                if (SockSessions.TryGetValue(sid, out var cur) && cur == sess)
                    cur.RecvBusy = false;
        }
    }

    private static void SockWsEndSession(string sid, SockSession? sess)
    {
        lock (SockLock)
        {
            if (sess is not null && SockSessions.TryGetValue(sid, out var cur) &&
                cur == sess)
                SockSessions.Remove(sid);
        }
        if (sess is not null)
        {
            try { SockCloseAll(sess); } catch { }
        }
    }

    private const int WS_MAX_MESSAGE = 4 * 1024 * 1024;

    internal static bool WsHasToken(string? headerVal, string token)
    {
        if (string.IsNullOrEmpty(headerVal)) return false;
        foreach (var part in headerVal.Split(','))
            if (part.Trim().Equals(token, StringComparison.OrdinalIgnoreCase))
                return true;
        return false;
    }

    private static async Task HandleSocketWs(
        HttpListenerContext ctx, State st)
    {
        _ = st;
        var req = ctx.Request;
        var res = ctx.Response;
        bool upgrade = WsHasToken(req.Headers["Connection"], "upgrade") &&
            string.Equals(req.Headers["Upgrade"]?.Trim(), "websocket",
                StringComparison.OrdinalIgnoreCase);
        if (!upgrade)
        {
            await Err(res, 400, "This route requires a WebSocket upgrade (Upgrade: websocket).");
            return;
        }
        if ((req.Headers["Sec-WebSocket-Version"] ?? "").Trim() != "13")
        {
            await Err(res, 400, "Unsupported WebSocket version (need 13).");
            return;
        }
        if (string.IsNullOrEmpty(req.Headers["Sec-WebSocket-Key"]))
        {
            await Err(res, 400, "Missing Sec-WebSocket-Key.");
            return;
        }
        if (!SockLookup(req, res, out var sid, out var sess)) return;
        lock (SockLock)
        {
            if (sess!.WsAttached)
            {
                Err(res, 400, "Socket session is already attached to a websocket.").Wait();
                return;
            }
            if (sess.RecvBusy)
            {
                Err(res, 400, "Another recv is already pending on this session.").Wait();
                return;
            }
            sess.WsAttached = true;
            sess.Last = DateTime.UtcNow;
        }
        WebSocket ws;
        try
        {
            var wsCtx = await ctx.AcceptWebSocketAsync(null);
            ws = wsCtx.WebSocket;
        }
        catch (Exception e)
        {
            lock (SockLock)
            {
                if (SockSessions.TryGetValue(sid, out var s2) && s2 == sess)
                    s2.WsAttached = false;
            }
            await Err(res, 500, $"WebSocket upgrade failed: {e.Message}");
            return;
        }
        var done = new TaskCompletionSource<object?>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var sendGate = new SemaphoreSlim(1, 1);
        var closedFlag = 0;
        async Task WsSend(byte[] buf, WebSocketMessageType type)
        {
            await sendGate.WaitAsync();
            try
            {
                await ws.SendAsync(new ArraySegment<byte>(buf), type,
                    true, CancellationToken.None);
            }
            finally { sendGate.Release(); }
        }
        async Task WsClose(WebSocketCloseStatus status)
        {
            if (Interlocked.Exchange(ref closedFlag, 1) != 0) return;
            try
            {
                await ws.CloseAsync(status, "", CancellationToken.None);
            }
            catch { }
        }
        void Finish()
        {
            SockWsEndSession(sid, sess);
            done.TrySetResult(null);
        }
        // --- TCP -> WS pump ---
        var pump = Task.Run(async () =>
        {
            var stream = SockStream(sess!);
            var buf = new byte[32768];
            while (true)
            {
                int n;
                try
                {
                    n = await stream.ReadAsync(buf, CancellationToken.None);
                }
                catch
                {
                    await WsClose(WebSocketCloseStatus.InternalServerError);
                    break;
                }
                if (n > 0)
                {
                    lock (SockLock)
                        if (SockSessions.TryGetValue(sid, out var s2) && s2 == sess)
                            s2.Last = DateTime.UtcNow;
                    try
                    {
                        await WsSend(buf[..n], WebSocketMessageType.Binary);
                    }
                    catch { break; }
                    continue;
                }
                lock (SockLock)
                {
                    if (SockSessions.TryGetValue(sid, out var s2) && s2 == sess)
                    {
                        s2.Eof = true;
                        s2.Last = DateTime.UtcNow;
                    }
                }
                await WsClose(WebSocketCloseStatus.NormalClosure);
                break;
            }
            // TCP ended: grace period for the close echo, then abort.
            try
            {
                await Task.WhenAny(done.Task, Task.Delay(2000));
            }
            catch { }
            try { ws.Abort(); } catch { }
        });
        // --- WS -> TCP reader (this request task) ---
        try
        {
            var stream = SockStream(sess!);
            var buf = new byte[32768];
            var msg = new MemoryStream();
            while (true)
            {
                WebSocketReceiveResult r;
                try
                {
                    r = await ws.ReceiveAsync(new ArraySegment<byte>(buf),
                        CancellationToken.None);
                }
                catch
                {
                    Finish();
                    break;
                }
                if (r.MessageType == WebSocketMessageType.Close)
                {
                    // Delete BEFORE echo: the echo implies "gone".
                    SockWsEndSession(sid, sess);
                    try
                    {
                        await ws.CloseAsync(
                            r.CloseStatus ?? WebSocketCloseStatus.NormalClosure,
                            r.CloseStatusDescription ?? "", CancellationToken.None);
                    }
                    catch { }
                    done.TrySetResult(null);
                    break;
                }
                if (r.MessageType != WebSocketMessageType.Text &&
                    r.MessageType != WebSocketMessageType.Binary)
                    continue;
                msg.Write(buf, 0, r.Count);
                if (msg.Length > WS_MAX_MESSAGE)
                {
                    await WsClose(WebSocketCloseStatus.MessageTooBig);
                    Finish();
                    break;
                }
                if (!r.EndOfMessage) continue;
                var payload = msg.ToArray();
                msg.SetLength(0);
                if (r.MessageType == WebSocketMessageType.Text)
                {
                    try
                    {
                        new System.Text.UTF8Encoding(false, true)
                            .GetString(payload);
                    }
                    catch
                    {
                        await WsClose(WebSocketCloseStatus.InvalidPayloadData);
                        Finish();
                        break;
                    }
                }
                try
                {
                    await stream.WriteAsync(payload, CancellationToken.None);
                }
                catch
                {
                    Finish();
                    break;
                }
                lock (SockLock)
                    if (SockSessions.TryGetValue(sid, out var s2) && s2 == sess)
                        s2.Last = DateTime.UtcNow;
            }
        }
        finally
        {
            Finish();
            try { await Task.WhenAny(pump, Task.Delay(5000)); }
            catch { }
            try { ws.Dispose(); } catch { }
        }
    }

    // -- route catalog ----------------------------------------------------------------

    private static Dictionary<string, object?> Param(string name, string location,
        bool required, string type, string desc, string? def = null,
        List<object?>? allowed = null)
    {
        var p = new Dictionary<string, object?>
        {
            ["name"] = name,
            ["location"] = location,
            ["required"] = Tags.Enc(required),
            ["type"] = type,
            ["description"] = desc,
        };
        if (def is not null) p["default"] = def;
        if (allowed is not null) p["allowed"] = allowed;
        return p;
    }

    private static Dictionary<string, object?> Route(string method, string url,
        string name, string desc, List<object?> pars, string response,
        string? errors = null)
    {
        var r = new Dictionary<string, object?>
        {
            ["method"] = method,
            ["url"] = url,
            ["name"] = name,
            ["description"] = desc,
            ["params"] = pars,
            ["response"] = response,
        };
        if (errors is not null) r["errors"] = errors;
        return r;
    }

    public static readonly List<object?> Routes =
    [
        Route("GET", "/api/help", "help",
            "Return the full list of implemented routes with URL, name, description and parameter formats. Wire convention: JSON carries only string/array/object - numbers, booleans and nulls travel as tagged strings (i:<int>, f:<float>, b:true|false, null). Fixed vocabulary and free text stay raw; sqlite row cells and params values are fully tagged (strings take an s: prefix there).",
            [], "JSON object { version, count, routes: [...] }."),
        Route("GET", "/api/dir?path=<path>", "read_directory",
            "List the contents of a directory.",
            [Param("path", "query", true, "string",
                "Absolute or relative path of the directory. Relative paths resolve against the server working directory. Paths starting with ~/ expand to the server user's home directory.")],
            "JSON { path, absolute_path, count, entries: [{ name, path, type, size, atime, mtime, ctime, crtime, mode }] }. type is one of file|dir|symlink|other. Times are epoch seconds: atime (last access), mtime (last modification), ctime (last status change; creation on Windows), crtime (creation/birth, null where unavailable).",
            "400 missing path / not-a-directory, 404 not found, 403 forbidden."),
        Route("GET", "/api/file?path=<path>[&encoding=text|base64|auto][&raw=1]", "read_file",
            "Read a file. Supports text and binary files. Default JSON mode auto-detects: UTF-8-decodable content is returned as text, otherwise as base64. With raw=1 the raw bytes are returned with a guessed Content-Type.",
            [Param("path", "query", true, "string",
                "Absolute or relative path of the file. Relative paths resolve against the server working directory; ~/ expands to the user's home directory."),
            Param("encoding", "query", false, "string",
                "auto: try UTF-8 text, fall back to base64. text: force UTF-8 decode with replacement of invalid sequences. base64: force base64.",
                "auto", ["auto", "text", "base64"]),
            Param("raw", "query", false, "string",
                "If '1', respond with raw file bytes and a guessed Content-Type instead of JSON.",
                "0", ["0", "1"])],
            "JSON mode: { path, absolute_path, size, mtime, mime_type, is_binary, encoding, content }. Raw mode: file bytes with Content-Type + Content-Length headers.",
            "400 missing path / is-a-directory, 404 not found, 403 forbidden."),
        Route("POST", "/api/file?path=<path>", "create_file",
            "Create a file, empty when no body is sent or with initial data (text or binary). Fails if the path already exists (use PUT to overwrite).",
            [Param("path", "query", true, "string",
                "Absolute or relative path of the file to create. ~/ expands to the server user's home directory."),
            Param("content", "body (JSON)", false, "string",
                "Initial content. Plain string for encoding=text, base64 string for encoding=base64. Omitted means empty file. Alternatively send raw bytes with a non-JSON Content-Type."),
            Param("encoding", "body (JSON)", false, "string",
                "How to interpret the JSON body content.", "text", ["text", "base64"])],
            "201 JSON { path, absolute_path, size, mime_type }.",
            "400 missing path / bad body, 404 parent directory missing, 409 already exists, 403 forbidden."),
        Route("PUT", "/api/file?path=<path>", "overwrite_file",
            "Overwrite the content of an existing file (text or binary). Same body rules as create_file.",
            [Param("path", "query", true, "string",
                "Absolute or relative path of the file to overwrite. ~/ expands to the server user's home directory."),
            Param("content", "body (JSON)", false, "string",
                "New content (see create_file). Omitted truncates the file to zero bytes."),
            Param("encoding", "body (JSON)", false, "string",
                "How to interpret the JSON body content.", "text", ["text", "base64"])],
            "200 JSON { path, absolute_path, size, mime_type }.",
            "400 missing path / is-a-directory / bad body, 404 not found, 403 forbidden."),
        Route("DELETE", "/api/file?path=<path>", "delete_file",
            "Delete a file.",
            [Param("path", "query", true, "string",
                "Absolute or relative path of the file to delete. ~/ expands to the server user's home directory.")],
            "200 JSON { path, absolute_path, deleted: true }.",
            "400 missing path / is-a-directory, 404 not found, 403 forbidden."),
        Route("DELETE", "/api/dir?path=<path>", "delete_directory",
            "Delete an empty directory (rmdir equivalent). Fails when the directory is not empty.",
            [Param("path", "query", true, "string",
                "Absolute or relative path of the empty directory to delete. ~/ expands to the server user's home directory.")],
            "200 JSON { path, absolute_path, deleted: true }.",
            "400 missing path / not-a-directory / not-empty, 404 not found, 403 forbidden."),
        Route("DELETE", "/api/dir/shallow?path=<path>", "delete_directory_shallow",
            "Delete a shallow directory: a directory containing only files (including symlinks) and empty subdirectories. Fails when it contains a non-empty real subdirectory. Does not delete recursively.",
            [Param("path", "query", true, "string",
                "Absolute or relative path of the shallow directory to delete. ~/ expands to the server user's home directory.")],
            "200 JSON { path, absolute_path, deleted: true }.",
            "400 missing path / not-a-directory / not-shallow, 404 not found, 403 forbidden."),
        Route("POST", "/api/shell", "exec_shell",
            "Execute a bash command string (pipes, redirections and all bash syntax allowed) and return the exit code plus separately captured stdout and stderr. WARNING: this is arbitrary remote code execution on the host; the server binds localhost by default and relies on that for protection.",
            [Param("command", "body (JSON)", true, "string",
                "Exact string as typed in bash, e.g. \"cat < a.txt | sort > b.txt\"."),
            Param("timeout", "body (JSON)", false, "number",
                "Seconds before the process group is killed (max 300). Timed-out calls return 124 with timed_out=true plus partial output.",
                "i:30"),
            Param("cwd", "body (JSON)", false, "string",
                "Working directory for the command (default: server working directory)."),
            Param("max_stdout", "body (JSON)", false, "integer",
                "Max captured stdout bytes sent back (1 MB default, 100 MB hard cap). The command runs fully; only the response is truncated.",
                "i:1048576"),
            Param("max_stderr", "body (JSON)", false, "integer",
                "Max captured stderr bytes sent back (same rules as max_stdout).",
                "i:1048576"),
            Param("stdin", "body (JSON)", false, "string",
                "Input piped to the command's stdin (as if from a previous pipe stage). Plain string for stdin_encoding=text, base64 for stdin_encoding=base64. Omitted means empty stdin (EOF at once)."),
            Param("stdin_encoding", "body (JSON)", false, "string",
                "How to interpret the stdin field.",
                "text", ["text", "base64"])],
            "200 JSON { command, cwd, returncode, stdout, stderr, timed_out, stdout_truncated, stderr_truncated, max_stdout, max_stderr, stdin_bytes }. returncode is the exit status of the last pipeline element (plain bash semantics). stdout/stderr are what the shell process emitted on its own fds, so in-command redirections apply: '> f' leaves captured stdout empty, '2>&1' merges into captured stdout. stdin_bytes is how many input bytes were fed.",
            "400 missing/empty command / bad JSON / bad timeout / bad cwd / bad or over-cap output limits, 405 wrong method."),
        Route("POST", "/api/sqlite/create?path=<path>", "sqlite_create",
            "Create a new SQLite database file. The file is permanent; only the per-request connection is closed afterwards.",
            [Param("path", "query", true, "string", "Full path of the database file to create.")],
            "201 JSON { path, absolute_path, size, sqlite_version }.",
            "400 missing path, 404 parent directory missing, 409 file already exists, 403 forbidden."),
        Route("POST", "/api/sqlite/query?path=<db-path>", "sqlite_query",
            "Execute one arbitrary SQL statement on a SQLite database. Stateless: a fresh connection is opened per request and closed right after. SELECT-style statements return rows; anything else is committed and returns the change counters.",
            [Param("path", "query", true, "string", "Path of the database file."),
            Param("sql", "body (JSON)", true, "string",
                "Exactly one SQL statement. Multi-statement strings are rejected (send one per request)."),
            Param("params", "body (JSON)", false, "array | object",
                "Bound parameters: array for ? placeholders, object for :named placeholders. Values must be tagged scalars (i:/f:/s:/b:/null); BLOB input is not supported.",
                "[]"),
            Param("timeout", "body (JSON)", false, "number",
                "Busy timeout in seconds while waiting on a locked database (max 300).", "i:5"),
            Param("readonly", "body (JSON)", false, "boolean",
                "Open the database read-only.", "b:false")],
            "200 JSON, SELECT-style: { path, absolute_path, columns, rows, rowcount } with BLOB values as base64 strings. Writes: { path, absolute_path, rowcount, lastrowid }.",
            "400 missing path/sql, bad JSON/params/timeout, multi-statement, or the SQLite error text itself (e.g. no such table). 404 database file not found, 403 forbidden, 405 wrong method."),
        Route("GET", "/api/sysinfo", "system_info",
            "Return operating-system information about the host running the server. Takes no parameters.",
            [],
            "200 JSON { system, system_name, release, version, machine, processor, hostname, cpu_count, distro, windows, mac }. system is the raw platform name (Linux/Windows/Darwin); system_name is the friendly one (Darwin -> macOS). distro is parsed from /etc/os-release on Linux and null elsewhere; windows/mac objects appear only on those systems.",
            "401 missing/invalid token, 405 wrong method."),
        Route("POST", "/api/socket/open", "socket_open",
            "Open a TCP connection to a network peer and hold it as a session, so browser pages (which cannot open raw sockets) can speak arbitrary TCP protocols through the local service (e.g. SMTP/IMAP for a browser email client). With tls:b:true the handshake runs immediately (implicit TLS, e.g. IMAPS on 993), verified against the system roots (+ optional ca_pem) with hostname check and SNI. Returns an opaque session id for send/recv/close/starttls. WARNING: this is an open network proxy on the host; the server binds localhost by default and relies on that for protection.",
            [Param("host", "body (JSON)", true, "string",
                "Peer hostname or IP address to connect to."),
            Param("port", "body (JSON)", true, "integer",
                "Peer TCP port, 1-65535 (i:<int>)."),
            Param("timeout", "body (JSON)", false, "number",
                "Connect timeout in seconds (max 30).", "i:5"),
            Param("tls", "body (JSON)", false, "boolean",
                "Handshake TLS immediately and verify the peer (implicit TLS).", "b:false"),
            Param("sni", "body (JSON)", false, "string",
                "Server name for SNI and certificate verification (default: host)."),
            Param("ca_pem", "body (JSON)", false, "string",
                "Extra PEM CA bundle appended to the system roots (corporate intercepts, test PKIs).")],
            "201 JSON { id, kind (tcp|tls), host, port }. The id is an opaque session token for the other socket routes.",
            "400 missing/invalid host/port/timeout/tls/sni/ca_pem, bad JSON, connect or TLS-handshake failure (engine error text), or too many open sessions. 405 wrong method."),
        Route("POST", "/api/socket/send?id=<id>", "socket_send",
            "Write bytes to an open socket session (transparent: the peer receives exactly these bytes).",
            [Param("id", "query", true, "string",
                "Session id from socket_open."),
            Param("content", "body (JSON)", false, "string",
                "Bytes to write. Plain string for encoding=text, base64 string for encoding=base64. Alternatively send raw bytes with a non-JSON Content-Type."),
            Param("encoding", "body (JSON)", false, "string",
                "How to interpret the JSON body content.", "text", ["text", "base64"])],
            "200 JSON { id, bytes } with the written byte count.",
            "400 missing id / bad body / socket error, 404 unknown session, 405 wrong method."),
        Route("GET", "/api/socket/recv?id=<id>[&timeout=][&max_bytes=]", "socket_recv",
            "Read bytes from an open socket session (transparent: whatever the peer sent). Long-polls up to timeout seconds; returns whatever arrived, an EOF flag when the peer closed, or a timeout flag when nothing arrived in time.",
            [Param("id", "query", true, "string",
                "Session id from socket_open."),
            Param("timeout", "query", false, "string",
                "Seconds to wait for data (max 60).", "5"),
            Param("max_bytes", "query", false, "string",
                "Max bytes per call (1-1048576).", "65536")],
            "200 JSON { id, encoding (base64), content, bytes, eof, timed_out }. content carries base64 bytes (may be empty); eof is true only with empty content after the peer closed; timed_out is true only with empty content when nothing arrived in time.",
            "400 missing id / bad timeout/max_bytes / socket error, 404 unknown session, 405 wrong method."),
        Route("POST", "/api/socket/close?id=<id>", "socket_close",
            "Close an open socket session and release it. Idle sessions also expire automatically.",
            [Param("id", "query", true, "string",
                "Session id from socket_open.")],
            "200 JSON { id, closed: true }.",
            "400 missing id, 404 unknown session, 405 wrong method."),
        Route("POST", "/api/socket/starttls?id=<id>", "socket_starttls",
            "Upgrade a plaintext socket session to TLS in place (STARTTLS-style protocols: drive the cleartext prelude over the session first, then call this before sending any more application bytes). Verified like implicit TLS (system roots + optional ca_pem, hostname check, SNI). A failed handshake leaves the plaintext session untouched; refused while a WebSocket owns the session, while a recv is pending, or when already TLS.",
            [Param("id", "query", true, "string",
                "Session id from socket_open."),
            Param("sni", "body (JSON)", false, "string",
                "Server name for SNI and certificate verification (default: the session host)."),
            Param("ca_pem", "body (JSON)", false, "string",
                "Extra PEM CA bundle appended to the system roots.")],
            "200 JSON { id, kind (tls) }.",
            "400 missing id / bad body / already TLS / recv pending / attached / handshake failure (engine error text), 404 unknown session, 405 wrong method."),
        Route("GET", "/api/socket/ws?id=<id>", "socket_ws",
            "Attach a WebSocket to an open socket session (the browser-native byte transport). Upgrade with `Upgrade: websocket` (token via `?token=`); after the 101 handshake, WS message payloads flow transparently client->TCP (text frames as UTF-8 bytes) and TCP bytes flow back as binary frames; ping gets an auto-pong. A close from either side, or any TCP/protocol error, ends the whole session (a WS close is a session close, like HTTP close). HTTP send/recv are refused while attached; HTTP close kills the bridge too.",
            [Param("id", "query", true, "string",
                "Session id from socket_open.")],
            "101 Switching Protocols, then transparent bidirectional bytes (see description).",
            "400 not-an-upgrade / bad handshake / missing id / already attached, 404 unknown session, 405 wrong method."),
        Route("GET", "/", "index",
            "Service index with version and pointer to /api/help.",
            [], "JSON { service, version, help }."),
    ];
}
