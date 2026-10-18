import { resolveConfig, loadStdinConfig, loadFileConfig, ConfigError } from "./config.js";
import { startServer, VERSION } from "./api.js";

function usage(): void {
  process.stderr.write(
    "Usage: node dist/server.js [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]\n" +
      "\nEvery setting can also come from a GNUMARUS_* environment variable\n" +
      "(GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from\n" +
      "nodejs/config.cfg (also checked next to dist/ and the working dir).\n" +
      "Precedence (highest first): CLI flag > env var > stdin > config.cfg > default.\n"
  );
}

function fail(msg: string): never {
  process.stderr.write(`config error: ${msg}\n`);
  process.exit(2);
}

function main(): void {
  const cli: Record<string, unknown> = {};
  const argv = process.argv.slice(2);
  const takeValue = (flag: string, i: number): string => {
    if (i + 1 >= argv.length) fail(`${flag} needs a value`);
    return argv[i + 1];
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--host") {
      cli["host"] = takeValue(a, i++);
    } else if (a === "--port") {
      cli["port"] = takeValue(a, i++);
    } else if (a === "--token") {
      cli["token"] = takeValue(a, i++);
    } else if (a === "--no-auth") {
      if (cli["no_auth"] === false) fail("--auth and --no-auth are mutually exclusive");
      cli["no_auth"] = true;
    } else if (a === "--auth") {
      if (cli["no_auth"] === true) fail("--auth and --no-auth are mutually exclusive");
      cli["no_auth"] = false;
    } else if (a === "-h" || a === "--help") {
      usage();
      process.exit(0);
    } else if (a.startsWith("--port=")) {
      cli["port"] = a.slice(7);
    } else if (a.startsWith("--host=")) {
      cli["host"] = a.slice(7);
    } else if (a.startsWith("--token=")) {
      cli["token"] = a.slice(8);
    } else {
      fail(`unknown argument: ${a}`);
    }
  }

  const cwd = process.cwd();
  let cfg;
  try {
    cfg = resolveConfig(cli, loadStdinConfig(), loadFileConfig());
  } catch (e) {
    fail((e as Error).message);
  }
  const s = cfg.sources;
  process.stdout.write(
    `config sources: host=${s["host"]} port=${s["port"]} token=${s["token"]} no_auth=${s["no_auth"]}\n`
  );

  const server = startServer(
    { authRequired: !cfg.noAuth, authToken: cfg.token ?? "", cwd },
    cfg.host,
    cfg.port
  );
  server.on("error", (e: NodeJS.ErrnoException) => {
    process.stderr.write(`cannot listen: ${e.message}\n`);
    process.exit(1);
  });
  process.stdout.write(
    `GnumarusNativeOps v${VERSION} serving on http://${cfg.host}:${cfg.port} (Ctrl+C to stop)\n`
  );
  if (!cfg.noAuth) {
    process.stdout.write(`API token: ${cfg.token}\n`);
    process.stdout.write(
      "Send it as 'Authorization: Bearer <token>' (or ?token=). /api/help stays open.\n"
    );
  } else {
    process.stdout.write(
      `WARNING: authentication DISABLED (no_auth from ${s["no_auth"]}). Anyone ` +
        "able to reach this server has full access, including shell execution.\n"
    );
  }
}

main();
