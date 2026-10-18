using System.Net;
using System.Net;
using System.Net.Sockets;
using System.Text;

/// <summary>
/// Loopback frontend for the HttpListener backend.
///
/// HttpListener matches the Host header against its prefixes before
/// user code runs, so it can never answer our own 403 for foreign
/// Hosts. This shim owns the public socket instead: it parses each
/// request head, enforces the Host allowlist itself (uniform 403
/// JSON), then forwards everything byte-transparent to a backend
/// HttpListener on an ephemeral 127.0.0.1 port (Host rewritten to
/// the backend's own address). WebSocket upgrades flow through
/// untouched once the backend answers 101.
/// </summary>
static class FrontProxy
{
    public static int FreePort()
    {
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        var port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        return port;
    }

    public static async Task<IPAddress> LoopbackAddressAsync(string host)
    {
        var h = host.Trim();
        if (h.StartsWith('[') && h.EndsWith(']') && h.Length > 2)
            h = h[1..^1];
        if (h.Equals("localhost", StringComparison.OrdinalIgnoreCase))
        {
            foreach (var ip in await Dns.GetHostAddressesAsync("localhost"))
                if (IPAddress.IsLoopback(ip)) return ip;
            return IPAddress.Loopback;
        }
        return IPAddress.Parse(h);
    }

    public static async Task RunAsync(TcpListener front, int backendPort)
    {
        while (true)
        {
            TcpClient client;
            try
            {
                client = await front.AcceptTcpClientAsync();
            }
            catch
            {
                break; // listener closed
            }
            _ = Task.Run(() => HandleAsync(client, backendPort));
        }
    }

    private static async Task<byte[]?> ReadHeadAsync(NetworkStream s)
    {
        var buf = new List<byte>(8192);
        var one = new byte[1];
        // Cap: same 1 MB headroom as the other custom servers.
        while (buf.Count < 1024 * 1024)
        {
            int n;
            try
            {
                n = await s.ReadAsync(one, 0, 1);
            }
            catch
            {
                return null;
            }
            if (n == 0) return null;
            buf.Add(one[0]);
            var count = buf.Count;
            if (count >= 4 && buf[count - 4] == (byte)'\r' &&
                buf[count - 3] == (byte)'\n' && buf[count - 2] == (byte)'\r' &&
                buf[count - 1] == (byte)'\n')
            {
                return buf.ToArray();
            }
        }
        return null;
    }

    private static bool TryParseHead(byte[] head, out string method,
        out string target, out List<(string Name, string Value)> headers)
    {
        method = "";
        target = "";
        headers = new List<(string, string)>();
        string text;
        try
        {
            text = Encoding.ASCII.GetString(head);
        }
        catch
        {
            return false;
        }
        var lines = text.Split("\r\n");
        if (lines.Length == 0) return false;
        var parts = lines[0].Split(' ');
        if (parts.Length != 3) return false;
        method = parts[0];
        target = parts[1];
        for (var i = 1; i < lines.Length; i++)
        {
            if (lines[i] == "") break;
            var colon = lines[i].IndexOf(':');
            if (colon < 0) return false;
            headers.Add((lines[i][..colon].Trim(),
                lines[i][(colon + 1)..].Trim()));
        }
        return true;
    }

    private static string? Header(List<(string Name, string Value)> headers,
        string name)
    {
        foreach (var (n, v) in headers)
            if (n.Equals(name, StringComparison.OrdinalIgnoreCase)) return v;
        return null;
    }

    private static async Task<byte[]> ReadBodyAsync(NetworkStream s,
        List<(string Name, string Value)> headers)
    {
        var outBuf = new List<byte>();
        var te = Header(headers, "Transfer-Encoding") ?? "";
        if (te.Split(',').Any(p =>
                p.Trim().Equals("chunked", StringComparison.OrdinalIgnoreCase)))
        {
            // De-chunk (re-framed as Content-Length downstream).
            while (true)
            {
                var line = await ReadLineAsync(s);
                if (line is null) break;
                var semi = line.IndexOf(';');
                var size = line[..(semi >= 0 ? semi : line.Length)].Trim();
                if (!int.TryParse(size,
                        System.Globalization.NumberStyles.HexNumber,
                        System.Globalization.CultureInfo.InvariantCulture,
                        out var n)) break;
                if (n == 0)
                {
                    // Trailer + final CRLF.
                    await ReadLineAsync(s);
                    break;
                }
                var chunk = new byte[n];
                var got = 0;
                while (got < n)
                {
                    int r;
                    try
                    {
                        r = await s.ReadAsync(chunk, got, n - got);
                    }
                    catch
                    {
                        break;
                    }
                    if (r == 0) break;
                    got += r;
                }
                outBuf.AddRange(chunk[..got]);
                if (got < n) break;
                await ReadLineAsync(s); // trailing CRLF
            }
            return outBuf.ToArray();
        }
        var cl = Header(headers, "Content-Length") ?? "";
        if (int.TryParse(cl.Trim(),
                System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture,
                out var len) && len > 0)
        {
            var body = new byte[len];
            var got = 0;
            while (got < len)
            {
                int r;
                try
                {
                    r = await s.ReadAsync(body, got, len - got);
                }
                catch
                {
                    break;
                }
                if (r == 0) break;
                got += r;
            }
            if (got < len) Array.Resize(ref body, got);
            return body;
        }
        return Array.Empty<byte>();
    }

    private static async Task<string?> ReadLineAsync(NetworkStream s)
    {
        var buf = new List<byte>(128);
        while (buf.Count < 8192)
        {
            var one = new byte[1];
            int n;
            try
            {
                n = await s.ReadAsync(one, 0, 1);
            }
            catch
            {
                return null;
            }
            if (n == 0) return null;
            buf.Add(one[0]);
            var c = buf.Count;
            if (c >= 2 && buf[c - 2] == (byte)'\r' && buf[c - 1] == (byte)'\n')
                return Encoding.ASCII.GetString(buf.ToArray(), 0, c - 2);
        }
        return null;
    }

    private static async Task PumpAsync(NetworkStream from, NetworkStream to)
    {
        var buf = new byte[65536];
        while (true)
        {
            int n;
            try
            {
                n = await from.ReadAsync(buf, 0, buf.Length);
            }
            catch
            {
                break;
            }
            if (n == 0) break;
            try
            {
                await to.WriteAsync(buf, 0, n);
            }
            catch
            {
                break;
            }
        }
    }

    private static async Task HandleAsync(TcpClient client, int backendPort)
    {
        using (client)
        using (var backend = new TcpClient())
        {
            NetworkStream cs;
            try
            {
                cs = client.GetStream();
            }
            catch
            {
                return;
            }
            var head = await ReadHeadAsync(cs);
            if (head is null) return;
            if (!TryParseHead(head, out var method, out var target,
                    out var headers)) return;
            var host = headers.Find(h => h.Name.Equals(
                "Host", StringComparison.OrdinalIgnoreCase)).Value;
            if (!Api.CheckHost(host))
            {
                var denied = Encoding.UTF8.GetBytes(
                    "{\"error\":\"forbidden\",\"message\":\"" +
                    Api.HostDeniedMsg + "\"}");
                var deniedHead = Encoding.ASCII.GetBytes(
                    "HTTP/1.1 403 Forbidden\r\n" +
                    "Content-Type: application/json; charset=utf-8\r\n" +
                    $"Content-Length: {denied.Length}\r\n" +
                    "Connection: close\r\n" +
                    "Access-Control-Allow-Origin: *\r\n" +
                    "Access-Control-Allow-Methods: GET, POST, PUT, DELETE, PATCH, OPTIONS\r\n" +
                    "Access-Control-Allow-Headers: Content-Type, Authorization, X-Requested-With\r\n" +
                    "Access-Control-Max-Age: 86400\r\n\r\n");
                try
                {
                    await cs.WriteAsync(deniedHead, 0, deniedHead.Length);
                    await cs.WriteAsync(denied, 0, denied.Length);
                }
                catch { }
                return;
            }
            try
            {
                await backend.ConnectAsync("127.0.0.1", backendPort);
            }
            catch
            {
                return;
            }
            var bs = backend.GetStream();
            // Forward: same request line, Host rewritten to the
            // backend's own address, connection forced closed (one
            // request per connection, like every other custom server
            // here), body re-framed as Content-Length.
            var body = await ReadBodyAsync(cs, headers);
            var fwd = new List<string> { $"{method} {target} HTTP/1.1" };
            var seenHost = false;
            var seenConn = false;
            var seenCl = false;
            var seenTe = false;
            foreach (var (n, v) in headers)
            {
                if (n.Equals("Host", StringComparison.OrdinalIgnoreCase))
                {
                    fwd.Add($"Host: 127.0.0.1:{backendPort}");
                    seenHost = true;
                }
                else if (n.Equals("Connection", StringComparison.OrdinalIgnoreCase))
                {
                    // An upgrade handshake must keep its Connection
                    // token; everything else goes Connection: close
                    // (one request per connection).
                    if (Api.WsHasToken(v, "upgrade")) fwd.Add($"{n}: {v}");
                    else fwd.Add("Connection: close");
                    seenConn = true;
                }
                else if (n.Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
                {
                    seenCl = true; // replaced below
                }
                else if (n.Equals("Transfer-Encoding", StringComparison.OrdinalIgnoreCase))
                {
                    seenTe = true; // replaced below (de-chunked)
                }
                else fwd.Add($"{n}: {v}");
            }
            if (!seenHost) fwd.Add($"Host: 127.0.0.1:{backendPort}");
            if (!seenConn) fwd.Add("Connection: close");
            fwd.Add($"Content-Length: {body.Length}");
            fwd.Add("");
            fwd.Add("");
            var fwdHead = Encoding.ASCII.GetBytes(string.Join("\r\n", fwd));
            try
            {
                await bs.WriteAsync(fwdHead, 0, fwdHead.Length);
                if (body.Length > 0)
                    await bs.WriteAsync(body, 0, body.Length);
            }
            catch
            {
                return;
            }
            // Backend answers are forwarded verbatim (status line,
            // headers, body framing untouched). 101 switches to a
            // blind bidirectional pipe.
            while (true)
            {
                var respHead = await ReadHeadAsync(bs);
                if (respHead is null) return;
                try
                {
                    await cs.WriteAsync(respHead, 0, respHead.Length);
                }
                catch
                {
                    return;
                }
                var statusLine = Encoding.ASCII.GetString(respHead)
                    .Split("\r\n")[0];
                if (statusLine.StartsWith("HTTP/1.1 101 ") ||
                    statusLine.StartsWith("HTTP/1.0 101 "))
                {
                    var up = PumpAsync(bs, cs);
                    await PumpAsync(cs, bs);
                    try { await up; } catch { }
                    return;
                }
                if (statusLine.Contains(" 100 ")) continue;
                // Final head: the backend always closes (we forced
                // Connection: close), so the body runs to EOF.
                await PumpAsync(bs, cs);
                return;
            }
        }
    }
}
