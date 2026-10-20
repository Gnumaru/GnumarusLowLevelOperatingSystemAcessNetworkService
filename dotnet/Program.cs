using System.Net;

/// <summary>
/// Gnumarus native-operations HTTP service (C#/.NET port).
///
/// Usage:
///     dotnet publish/GnumarusOSBridge.dll [--host H] [--port P] [--token TEXT] [--no-auth | --auth]
///
/// Settings resolve as: CLI flag > GNUMARUS_* env var > stdin key=value
/// lines > dotnet/publish/config.cfg (also checked next to the working
/// dir and the executable) > default.
/// </summary>
static class Program
{
    static void Usage()
    {
        Console.Error.WriteLine(
            "Usage: dotnet publish/GnumarusOSBridge.dll [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]\n" +
            "\nEvery setting can also come from a GNUMARUS_* environment variable\n" +
            "(GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from\n" +
            "config.cfg (working dir, executable dir or publish dir).\n" +
            "Precedence (highest first): CLI flag > env var > stdin > config.cfg > default.");
    }

    static void Fail(string msg)
    {
        Console.Error.WriteLine($"config error: {msg}");
        Environment.Exit(2);
    }

    static async Task Main(string[] args)
    {
        var cli = new Dictionary<string, object?>();
        string TakeValue(string flag, ref int i)
        {
            if (i + 1 >= args.Length) Fail($"{flag} needs a value");
            i++;
            return args[i];
        }
        for (var i = 0; i < args.Length; i++)
        {
            var a = args[i];
            if (a == "--host") cli["host"] = TakeValue(a, ref i);
            else if (a == "--port") cli["port"] = TakeValue(a, ref i);
            else if (a == "--token") cli["token"] = TakeValue(a, ref i);
            else if (a == "--no-auth")
            {
                if (cli.TryGetValue("no_auth", out var v) && v is false) Fail("--auth and --no-auth are mutually exclusive");
                cli["no_auth"] = true;
            }
            else if (a == "--auth")
            {
                if (cli.TryGetValue("no_auth", out var v) && v is true) Fail("--auth and --no-auth are mutually exclusive");
                cli["no_auth"] = false;
            }
            else if (a == "-h" || a == "--help")
            {
                Usage();
                return;
            }
            else if (a.StartsWith("--port=")) cli["port"] = a[7..];
            else if (a.StartsWith("--host=")) cli["host"] = a[7..];
            else if (a.StartsWith("--token=")) cli["token"] = a[8..];
            else Fail($"unknown argument: {a}");
        }

        var cwd = Directory.GetCurrentDirectory();
        Config.Resolved cfg;
        try
        {
            cfg = Config.Resolve(cli, Config.LoadStdin(), Config.LoadFile());
        }
        catch (Config.ConfigError e)
        {
            Fail(e.Message);
            return;
        }
        var s = cfg.Sources;
        Console.WriteLine(
            $"config sources: host={s["host"]} port={s["port"]} token={s["token"]} no_auth={s["no_auth"]}");

        var listener = new HttpListener();
        var backendPort = 0;
        for (var attempt = 0; attempt < 5; attempt++)
        {
            backendPort = FrontProxy.FreePort();
            listener.Prefixes.Clear();
            listener.Prefixes.Add($"http://127.0.0.1:{backendPort}/");
            try
            {
                listener.Start();
                break;
            }
            catch
            {
                if (attempt == 4) throw;
            }
        }
        var st = new Api.State(!cfg.NoAuth, cfg.Token ?? "", cwd);
        Console.WriteLine(
            $"GnumarusNativeOps v{Api.VERSION} serving on http://{cfg.Host}:{cfg.Port} (Ctrl+C to stop)");
        if (!cfg.NoAuth)
        {
            Console.WriteLine($"API token: {cfg.Token}");
            Console.WriteLine(
                "Send it as 'Authorization: Bearer <token>' (or ?token=). /api/help stays open.");
        }
        else
        {
            Console.WriteLine(
                $"WARNING: authentication DISABLED (no_auth from {s["no_auth"]}). Anyone " +
                "able to reach this server has full access, including shell execution.");
        }
        _ = Task.Run(async () =>
        {
            while (true)
            {
                HttpListenerContext ctx;
                try
                {
                    ctx = await listener.GetContextAsync();
                }
                catch (HttpListenerException)
                {
                    break;
                }
                catch (ObjectDisposedException)
                {
                    break;
                }
                _ = Task.Run(() => Api.HandleAsync(ctx, st));
            }
        });
        // Public socket: loopback only (cfg.Host was validated).
        // The Host allowlist is enforced here, in front of the
        // backend, so denied names get our 403 instead of
        // HttpListener's prefix 404.
        var front = new System.Net.Sockets.TcpListener(
            await FrontProxy.LoopbackAddressAsync(cfg.Host), cfg.Port);
        try
        {
            front.Start();
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"cannot listen: {e.Message}");
            Environment.Exit(1);
        }
        await FrontProxy.RunAsync(front, backendPort);
    }
}
