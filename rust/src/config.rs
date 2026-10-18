//! Multi-source configuration: CLI flag > env var > stdin > config.cfg > default.

use std::collections::HashMap;
use std::io::Read;

pub const CONFIG_KEYS: [&str; 4] = ["host", "port", "token", "no_auth"];
pub const CONFIG_FILE_NAME: &str = "config.cfg";
pub const CONFIG_MAX_BYTES: usize = 1024 * 1024;
pub const CONFIG_ENV_PREFIX: &str = "GNUMARUS_";

#[derive(Debug)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for ConfigError {}

#[derive(Debug, Clone)]
pub struct Resolved {
    pub host: String,
    pub port: u16,
    pub token: Option<String>,
    pub no_auth: bool,
    pub sources: HashMap<String, String>,
}

pub fn parse_kv(data: &[u8]) -> HashMap<String, String> {
    let mut cfg = HashMap::new();
    if data.contains(&0) {
        return cfg;
    }
    let text = match std::str::from_utf8(data) {
        Ok(t) => t,
        Err(_) => return cfg,
    };
    for raw_line in text.split('\n') {
        let line = raw_line.trim().trim_end_matches('\r');
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            continue;
        }
        let Some(eq) = line.find('=') else {
            continue;
        };
        let key = line[..eq].trim().to_lowercase();
        let mut value = line[eq + 1..].trim().to_string();
        if value.len() >= 2
            && ((value.starts_with('\'') && value.ends_with('\''))
                || (value.starts_with('"') && value.ends_with('"')))
        {
            value = value[1..value.len() - 1].to_string();
        }
        if CONFIG_KEYS.contains(&key.as_str()) {
            cfg.insert(key, value);
        }
    }
    cfg
}

/// Candidate config.cfg locations: working dir, exe dir.
pub fn candidates() -> Vec<std::path::PathBuf> {
    let mut out = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        out.push(cwd.join(CONFIG_FILE_NAME));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            out.push(dir.join(CONFIG_FILE_NAME));
        }
    }
    out
}

pub fn load_file() -> HashMap<String, String> {
    for cand in candidates() {
        let data = match std::fs::read(&cand) {
            Ok(d) => d,
            Err(_) => continue,
        };
        let capped = if data.len() > CONFIG_MAX_BYTES {
            &data[..CONFIG_MAX_BYTES]
        } else {
            &data[..]
        };
        return parse_kv(capped);
    }
    HashMap::new()
}

pub fn load_stdin() -> HashMap<String, String> {
    use std::io::IsTerminal;
    // Never block: a terminal (or nothing piped) means absent.
    if std::io::stdin().is_terminal() {
        return HashMap::new();
    }
    let mut buf = vec![0u8; CONFIG_MAX_BYTES];
    let mut total = 0;
    let stdin = std::io::stdin();
    let mut handle = stdin.lock();
    while total < buf.len() {
        match handle.read(&mut buf[total..]) {
            Ok(0) => break,
            Ok(n) => total += n,
            Err(_) => break,
        }
    }
    if total == 0 {
        return HashMap::new();
    }
    parse_kv(&buf[..total])
}

pub fn parse_bool(value: &str, source: &str) -> Result<bool, ConfigError> {
    match value.trim().to_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        _ => Err(ConfigError(format!(
            "invalid boolean for no_auth from {source}: '{value}' (use true/false)"
        ))),
    }
}

pub fn parse_port(value: &str, source: &str) -> Result<u16, ConfigError> {
    let bad = || {
        ConfigError(format!(
            "invalid port from {source}: '{value}' (1-65535)"
        ))
    };
    let s = value.trim();
    let digits = s.strip_prefix('+').unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return Err(bad());
    }
    let port: i64 = digits.parse().map_err(|_| bad())?;
    if !(1..=65535).contains(&port) {
        return Err(ConfigError(format!(
            "port from {source} out of range: {port} (1-65535)"
        )));
    }
    Ok(port as u16)
}

pub fn resolve(
    cli: &HashMap<String, String>,
    stdin_cfg: &HashMap<String, String>,
    file_cfg: &HashMap<String, String>,
) -> Result<Resolved, ConfigError> {
    let mut env_cfg = HashMap::new();
    for key in CONFIG_KEYS {
        if let Ok(v) = std::env::var(format!("{CONFIG_ENV_PREFIX}{}", key.to_uppercase())) {
            if !v.is_empty() {
                env_cfg.insert(key.to_string(), v);
            }
        }
    }
    let layers = [
        ("cli", cli),
        ("env", &env_cfg),
        ("stdin", stdin_cfg),
        ("file", file_cfg),
    ];
    let mut raw: HashMap<String, String> = HashMap::new();
    let mut sources: HashMap<String, String> = HashMap::new();
    for key in CONFIG_KEYS {
        sources.insert(key.to_string(), "default".to_string());
        for (source, layer) in &layers {
            if let Some(v) = layer.get(key) {
                raw.insert(key.to_string(), v.clone());
                sources.insert(key.to_string(), source.to_string());
                break;
            }
        }
    }
    let host = raw
        .get("host")
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|| "127.0.0.1".to_string());
    if host.is_empty() {
        return Err(ConfigError(format!(
            "host from {} must not be empty",
            sources["host"]
        )));
    }
    if !crate::hosts::bind_allowed(&host) {
        return Err(ConfigError(format!(
            "host from {} must be a loopback address (127.0.0.1, ::1 or localhost), got '{}'",
            sources["host"], host
        )));
    }
    let port = match raw.get("port") {
        Some(v) => parse_port(v, &sources["port"])?,
        None => 1423,
    };
    let no_auth = match raw.get("no_auth") {
        Some(v) => parse_bool(v, &sources["no_auth"])?,
        None => false,
    };
    let mut token: Option<String> = None;
    if no_auth {
        sources.insert("token".to_string(), "disabled".to_string());
    } else if let Some(v) = raw.get("token") {
        if v.is_empty() {
            return Err(ConfigError(format!(
                "token from {} must not be empty",
                sources["token"]
            )));
        }
        token = Some(v.clone());
    } else {
        let mut bytes = [0u8; 32];
        read_urandom(&mut bytes)?;
        token = Some(base64url_nopad(&bytes));
        sources.insert("token".to_string(), "generated".to_string());
    }
    Ok(Resolved {
        host,
        port,
        token,
        no_auth,
        sources,
    })
}

fn read_urandom(buf: &mut [u8]) -> Result<(), ConfigError> {
    use std::io::Read;
    let mut fh = std::fs::File::open("/dev/urandom")
        .map_err(|e| ConfigError(format!("cannot read /dev/urandom: {e}")))?;
    let mut total = 0;
    while total < buf.len() {
        match fh.read(&mut buf[total..]) {
            Ok(0) => break,
            Ok(n) => total += n,
            Err(e) => return Err(ConfigError(format!("cannot read /dev/urandom: {e}"))),
        }
    }
    if total != buf.len() {
        return Err(ConfigError("short read from /dev/urandom".to_string()));
    }
    Ok(())
}

const B64URL: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn base64url_nopad(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let mut n: u32 = 0;
        for (i, &b) in chunk.iter().enumerate() {
            n |= (b as u32) << (16 - 8 * i);
        }
        let chars = if chunk.len() == 3 {
            4
        } else if chunk.len() == 2 {
            3
        } else {
            2
        };
        for i in 0..chars {
            out.push(B64URL[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}
