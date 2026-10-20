//! System information endpoint.

use crate::api::{jstr, send_json_obj};
use crate::http::Response;

fn read_os_release() -> std::collections::HashMap<String, String> {
    let mut info = std::collections::HashMap::new();
    let data = match std::fs::read_to_string("/etc/os-release") {
        Ok(d) => d,
        Err(_) => return info,
    };
    for line in data.lines() {
        let t = line.trim();
        if t.is_empty() || t.starts_with('#') || !t.contains('=') {
            continue;
        }
        let eq = t.find('=').unwrap();
        info.insert(
            t[..eq].to_string(),
            t[eq + 1..].trim().trim_matches(['"', '\'']).to_string(),
        );
    }
    info
}

pub fn handle() -> Response {
    let os = std::env::consts::OS;
    let system = match os {
        "windows" => "Windows",
        "macos" => "Darwin",
        _ => "Linux",
    };
    let mut distro = serde_json::Value::String("null".to_string());
    let mut windows = serde_json::Value::String("null".to_string());
    let mut mac = serde_json::Value::String("null".to_string());
    if system == "Linux" {
        let rel = read_os_release();
        if !rel.is_empty() {
            let mut o = serde_json::Map::new();
            for key in ["id", "name", "version", "version_id", "pretty_name"] {
                let src = match key {
                    "id" => "ID",
                    "name" => "NAME",
                    "version" => "VERSION",
                    "version_id" => "VERSION_ID",
                    _ => "PRETTY_NAME",
                };
                o.insert(
                    key.to_string(),
                    jstr(rel.get(src).map(|s| s.as_str()).unwrap_or("")),
                );
            }
            distro = serde_json::Value::Object(o);
        }
    } else if system == "Windows" {
        let mut o = serde_json::Map::new();
        o.insert("release".to_string(), jstr(os));
        o.insert("version".to_string(), jstr(""));
        o.insert("service_pack".to_string(), jstr(""));
        o.insert("edition".to_string(), jstr(""));
        windows = serde_json::Value::Object(o);
    } else if system == "Darwin" {
        let mut o = serde_json::Map::new();
        o.insert("version".to_string(), jstr(""));
        mac = serde_json::Value::Object(o);
    }
    let (release, version) = kernel_info();
    let hostname = hostname();
    let cpu = std::thread::available_parallelism()
        .map(|n| n.get() as i64)
        .unwrap_or(1)
        .max(1);
    let mut o = serde_json::Map::new();
    o.insert("system".to_string(), jstr(system));
    o.insert(
        "system_name".to_string(),
        jstr(if system == "Darwin" { "macOS" } else { system }),
    );
    o.insert("release".to_string(), jstr(&release));
    o.insert("version".to_string(), jstr(&version));
    o.insert("machine".to_string(), jstr(std::env::consts::ARCH));
    o.insert("processor".to_string(), jstr(""));
    o.insert("hostname".to_string(), jstr(&hostname));
    // NOTE: no runtime-version field here. Managed runtimes report the
    // version of the runtime that executes them (python_version,
    // node_version, php_version, ruby_version, jvm_version,
    // dotnet_version); native ones (Go, Rust, C++) do not report one.
    o.insert("cpu_count".to_string(), jstr(&format!("i:{cpu}")));
    o.insert("distro".to_string(), distro);
    o.insert("windows".to_string(), windows);
    o.insert("mac".to_string(), mac);
    send_json_obj(200, &o)
}

fn kernel_info() -> (String, String) {
    if std::env::consts::OS != "linux" {
        return (String::new(), String::new());
    }
    let release = std::fs::read_to_string("/proc/sys/kernel/osrelease")
        .map(|s| s.trim().to_string())
        .unwrap_or_default();
    let version = std::fs::read_to_string("/proc/sys/kernel/version")
        .map(|s| s.trim().to_string())
        .unwrap_or_default();
    (release, version)
}

fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: fixed buffer, NUL-checked length.
    let ret = unsafe { libc::gethostname(buf.as_mut_ptr() as *mut i8, buf.len()) };
    if ret != 0 {
        return String::new();
    }
    let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..len]).into_owned()
}
