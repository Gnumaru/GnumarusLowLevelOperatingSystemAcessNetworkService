//! Entry point: CLI, config resolution, server boot.

mod api;
mod codec;
mod config;
mod help;
mod hosts;
mod http;
mod shell;
mod socket;
mod socket_ws;
mod sqlite_api;
mod sysinfo;
mod tags;
mod timex;

pub const VERSION: &str = "0.10.0";

fn usage() {
    eprintln!(
        "Usage: ./bin/gnumarus [--host HOST] [--port PORT] [--token TOKEN] [--no-auth | --auth]\n\
         \nEvery setting can also come from a GNUMARUS_* environment variable\n\
         (GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin key=value lines, or from\n\
         config.cfg (working dir or executable dir).\n\
         Precedence (highest first): CLI flag > env var > stdin > config.cfg > default."
    );
}

fn fail(msg: &str) -> ! {
    eprintln!("config error: {msg}");
    std::process::exit(2);
}

fn main() {
    let mut cli: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let mut no_auth: Option<bool> = None;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        if a == "--host" || a == "--port" || a == "--token" {
            if i + 1 >= args.len() {
                fail(&format!("{a} needs a value"));
            }
            i += 1;
            cli.insert(a[2..].to_string(), args[i].clone());
        } else if a == "--no-auth" {
            if no_auth == Some(false) {
                fail("--auth and --no-auth are mutually exclusive");
            }
            no_auth = Some(true);
        } else if a == "--auth" {
            if no_auth == Some(true) {
                fail("--auth and --no-auth are mutually exclusive");
            }
            no_auth = Some(false);
        } else if a == "-h" || a == "--help" {
            usage();
            return;
        } else if let Some(v) = a.strip_prefix("--port=") {
            cli.insert("port".to_string(), v.to_string());
        } else if let Some(v) = a.strip_prefix("--host=") {
            cli.insert("host".to_string(), v.to_string());
        } else if let Some(v) = a.strip_prefix("--token=") {
            cli.insert("token".to_string(), v.to_string());
        } else {
            fail(&format!("unknown argument: {a}"));
        }
        i += 1;
    }

    let cwd = std::env::current_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| ".".to_string());
    if let Some(na) = no_auth {
        cli.insert("no_auth".to_string(), na.to_string());
    }
    let cfg = match config::resolve(&cli, &config::load_stdin(), &config::load_file()) {
        Ok(c) => c,
        Err(e) => fail(&e.to_string()),
    };
    let s = &cfg.sources;
    println!(
        "config sources: host={} port={} token={} no_auth={}",
        s["host"], s["port"], s["token"], s["no_auth"]
    );
    use std::io::Write as _;
    let _ = std::io::stdout().flush();

    // NOTE: resolve() takes &HashMap<String,String>; adapt types below.
    start_server(cfg, cwd);
}

fn start_server(cfg: config::Resolved, cwd: String) {
    let state = api::State {
        auth_required: !cfg.no_auth,
        auth_token: cfg.token.clone().unwrap_or_default(),
        cwd: cwd.clone(),
    };
    let addr = format!("{}:{}", cfg.host, cfg.port);
    let listener = match std::net::TcpListener::bind(&addr) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("cannot listen: {e}");
            std::process::exit(1);
        }
    };
    println!("GnumarusNativeOps v{VERSION} serving on http://{addr} (Ctrl+C to stop)");
    if !cfg.no_auth {
        println!("API token: {}", cfg.token.as_deref().unwrap_or(""));
        println!("Send it as 'Authorization: Bearer <token>' (or ?token=). /api/help stays open.");
    } else {
        println!(
            "WARNING: authentication DISABLED (no_auth from {}). Anyone able to reach this server has full access, including shell execution.",
            cfg.sources["no_auth"]
        );
    }
    use std::io::Write as _;
    let _ = std::io::stdout().flush();
    let state = std::sync::Arc::new(state);
    crate::http::serve(listener, state.clone(), move |req| {
        let state = state.clone();
        crate::api::handle(req, &state)
    });
}
