/**
 * Gnumarus native-operations HTTP service (Kotlin/JVM port).
 *
 * Usage:
 *     GnumarusOSBridge [--host H] [--port P] [--token TEXT] [--no-auth | --auth]
 *
 * Settings resolve as: CLI flag > GNUMARUS_* env var > stdin key=value
 * lines > config.cfg next to the installed app > default.
 */
fun printUsage() {
    System.err.println(
        "Usage: GnumarusOSBridge [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]\n" +
            "\nEvery setting can also come from a GNUMARUS_* environment variable\n" +
            "(GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from\n" +
            "config.cfg next to the installed app. Precedence (highest first): CLI\n" +
            "flag > env var > stdin > config.cfg > default.")
}

fun main(args: Array<String>) {
    val cli = HashMap<String, Any?>()
    var i = 0
    fun needValue(flag: String): String {
        if (i + 1 >= args.size) {
            System.err.println("config error: $flag needs a value")
            kotlin.system.exitProcess(2)
        }
        i++
        return args[i]
    }
    while (i < args.size) {
        when (val a = args[i]) {
            "--host" -> cli["host"] = needValue(a)
            "--port" -> cli["port"] = needValue(a)
            "--token" -> cli["token"] = needValue(a)
            "--no-auth" -> {
                if (cli["no_auth"] == false) {
                    System.err.println("config error: --auth and --no-auth are mutually exclusive")
                    kotlin.system.exitProcess(2)
                }
                cli["no_auth"] = true
            }
            "--auth" -> {
                if (cli["no_auth"] == true) {
                    System.err.println("config error: --auth and --no-auth are mutually exclusive")
                    kotlin.system.exitProcess(2)
                }
                cli["no_auth"] = false
            }
            "-h", "--help" -> {
                printUsage()
                return
            }
            else -> {
                if (a.startsWith("--port=")) cli["port"] = a.substringAfter("=")
                else if (a.startsWith("--host=")) cli["host"] = a.substringAfter("=")
                else if (a.startsWith("--token=")) cli["token"] = a.substringAfter("=")
                else {
                    System.err.println("config error: unknown argument: $a")
                    kotlin.system.exitProcess(2)
                }
            }
        }
        i++
    }

    CWD = System.getProperty("user.dir")
    val cfg: ResolvedConfig
    try {
        cfg = resolveConfig(cli, loadStdinConfig(), loadFileConfig())
    } catch (e: ConfigError) {
        System.err.println("config error: ${e.message}")
        kotlin.system.exitProcess(2)
    }
    val s = cfg.sources
    println("config sources: host=${s["host"]} port=${s["port"]} token=${s["token"]} no_auth=${s["no_auth"]}")
    System.out.flush()

    val api = ApiHandler(!cfg.noAuth, cfg.token ?: "")
    val server = WebServer(cfg.host, cfg.port) { ex -> api.handle(ex) }
    server.start()
    println("GnumarusNativeOps v$VERSION serving on http://${cfg.host}:${cfg.port} (Ctrl+C to stop)")
    if (!cfg.noAuth) {
        println("API token: ${cfg.token}")
        println("Send it as 'Authorization: Bearer <token>' (or ?token=). /api/help stays open.")
    } else {
        println("WARNING: authentication DISABLED (no_auth from ${s["no_auth"]}). Anyone " +
            "able to reach this server has full access, including shell execution.")
    }
    System.out.flush()
    Thread.currentThread().join()
}
