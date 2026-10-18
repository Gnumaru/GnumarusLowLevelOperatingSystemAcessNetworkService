//! Shell execution: bash -c with streaming capped capture, process-group kill.

use crate::api::{err, jstr, resolve_path, send_json_obj, State};
use crate::http::{Request, Response};
use crate::tags::{jdec, Tagged};
use std::io::Read;
use std::sync::{Arc, Mutex};

pub const SHELL_DEFAULT_TIMEOUT: f64 = 30.0;
pub const SHELL_MAX_TIMEOUT: f64 = 300.0;
pub const SHELL_DEFAULT_MAX_OUTPUT: i64 = 1024 * 1024;
pub const SHELL_MAX_OUTPUT: i64 = 100 * 1024 * 1024;

struct Capped {
    buf: Vec<u8>,
    cap: usize,
    truncated: bool,
}

impl Capped {
    fn push(&mut self, chunk: &[u8]) {
        let room = self.cap.saturating_sub(self.buf.len());
        if room > 0 {
            let take = chunk.len().min(room);
            self.buf.extend_from_slice(&chunk[..take]);
        }
        if chunk.len() > room {
            self.truncated = true;
        }
    }
}

fn drain(mut stream: impl Read + Send + 'static, acc: Arc<Mutex<Capped>>) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let mut tmp = [0u8; 65536];
        loop {
            match stream.read(&mut tmp) {
                Ok(0) => break,
                Ok(n) => acc.lock().unwrap().push(&tmp[..n]),
                Err(_) => break,
            }
        }
    })
}

fn req_obj(req: &Request) -> Result<serde_json::Map<String, serde_json::Value>, Response> {
    let ct = req.headers.get("content-type").map(|s| s.as_str()).unwrap_or("");
    if !ct.contains("application/json") {
        return Err(err(
            400,
            "Content-Type must be application/json with body { command, timeout?, cwd?, max_stdout?, max_stderr?, stdin?, stdin_encoding? }.",
            None,
        ));
    }
    if req.body.is_empty() {
        return Ok(serde_json::Map::new());
    }
    match serde_json::from_slice::<serde_json::Value>(&req.body) {
        Ok(serde_json::Value::Object(m)) => Ok(m),
        Ok(_) => Err(err(400, "JSON body must be an object.", None)),
        Err(_) => Err(err(400, "Invalid JSON body.", None)),
    }
}

pub fn handle(req: &Request, state: &State) -> Response {
    let obj = match req_obj(req) {
        Ok(o) => o,
        Err(r) => return r,
    };
    let command = match obj.get("command") {
        Some(serde_json::Value::String(s)) if !s.trim().is_empty() => s.clone(),
        _ => return err(400, "Missing or empty required field: command.", None),
    };

    let timeout = match obj.get("timeout") {
        None | Some(serde_json::Value::Null) => SHELL_DEFAULT_TIMEOUT,
        Some(serde_json::Value::String(s)) => match jdec(s) {
            Ok(Tagged::Int(n)) => (n as f64).clamp(1.0, SHELL_MAX_TIMEOUT),
            Ok(Tagged::Float(f)) => f.clamp(1.0, SHELL_MAX_TIMEOUT),
            _ => {
                return err(
                    400,
                    "Field 'timeout' must be a tagged number (i:<int> or f:<float>).",
                    None,
                )
            }
        },
        Some(_) => {
            return err(
                400,
                "Field 'timeout' must be a tagged number (i:<int> or f:<float>).",
                None,
            )
        }
    };

    let mut cwd = state.cwd.clone();
    if let Some(v) = obj.get("cwd") {
        if !v.is_null() {
            let Some(s) = v.as_str() else {
                return err(400, "Field 'cwd' must be a string.", None);
            };
            cwd = resolve_path(s, &state.cwd);
            if !std::path::Path::new(&cwd).is_dir() {
                return err(400, "Working directory does not exist.", Some(&cwd));
            }
        }
    }

    fn limit(
        obj: &serde_json::Map<String, serde_json::Value>,
        key: &str,
    ) -> Result<i64, Response> {
        let raw = match obj.get(key) {
            None | Some(serde_json::Value::Null) => return Ok(SHELL_DEFAULT_MAX_OUTPUT),
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(_) => {
                return Err(err(
                    400,
                    &format!("Field '{key}' must be an integer >= 0 (i:<int>)."),
                    None,
                ))
            }
        };
        match jdec(&raw) {
            Ok(Tagged::Int(n)) if n >= 0 => {
                if n > SHELL_MAX_OUTPUT {
                    Err(err(
                        400,
                        &format!("Field '{key}' exceeds the {SHELL_MAX_OUTPUT} byte cap."),
                        None,
                    ))
                } else {
                    Ok(n)
                }
            }
            _ => Err(err(
                400,
                &format!("Field '{key}' must be an integer >= 0 (i:<int>)."),
                None,
            )),
        }
    }

    let max_out = match limit(&obj, "max_stdout") {
        Ok(n) => n as usize,
        Err(r) => return r,
    };
    let max_err = match limit(&obj, "max_stderr") {
        Ok(n) => n as usize,
        Err(r) => return r,
    };

    let stdin_raw: &str = match obj.get("stdin") {
        None | Some(serde_json::Value::Null) => "",
        Some(serde_json::Value::String(s)) => s,
        Some(_) => {
            return err(400, "Field 'stdin' must be a string.", None);
        }
    };
    let stdin_enc: &str = match obj.get("stdin_encoding") {
        None | Some(serde_json::Value::Null) => "text",
        Some(serde_json::Value::String(s)) => s,
        Some(_) => {
            return err(400, "Field 'stdin_encoding' must be text|base64.", None);
        }
    };
    let stdin_data: Vec<u8> = match stdin_enc.to_lowercase().as_str() {
        "text" => stdin_raw.as_bytes().to_vec(),
        "base64" => match crate::codec::b64_decode(stdin_raw) {
            Some(d) => d,
            None => return err(400, "Invalid base64 'stdin'.", None),
        },
        _ => {
            return err(400, "Field 'stdin_encoding' must be text|base64.", None);
        }
    };

    let mut child = match std::process::Command::new("bash")
        .arg("-c")
        .arg(&command)
        .current_dir(&cwd)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .pre_exec_sets_id()
        .spawn()
    {
        Ok(c) => c,
        Err(e) => return err(400, &format!("Cannot start shell: {e}"), None),
    };
    let out_acc = Arc::new(Mutex::new(Capped {
        buf: Vec::new(),
        cap: max_out,
        truncated: false,
    }));
    let err_acc = Arc::new(Mutex::new(Capped {
        buf: Vec::new(),
        cap: max_err,
        truncated: false,
    }));
    let out_h = drain(child.stdout.take().unwrap(), out_acc.clone());
    let err_h = drain(child.stderr.take().unwrap(), err_acc.clone());
    // Feeder thread: write all input, then close (EOF). A command that
    // never reads cannot block the timeout: killing the group faults
    // the write (or the close unblocks it) and the thread ends.
    let stdin_len = stdin_data.len();
    let mut stdin_pipe = child.stdin.take().unwrap();
    let feed_h = std::thread::spawn(move || {
        use std::io::Write;
        let _ = stdin_pipe.write_all(&stdin_data);
        drop(stdin_pipe);
    });

    let (timed_out, returncode) = wait_bounded(&mut child, timeout);
    join_bounded(out_h);
    join_bounded(err_h);
    join_bounded(feed_h);

    let out = out_acc.lock().unwrap();
    let errb = err_acc.lock().unwrap();
    let mut o = serde_json::Map::new();
    o.insert("command".to_string(), jstr(&command));
    o.insert("cwd".to_string(), jstr(&cwd));
    o.insert("returncode".to_string(), jstr(&format!("i:{returncode}")));
    o.insert("stdout".to_string(), jstr(&String::from_utf8_lossy(&out.buf)));
    o.insert("stderr".to_string(), jstr(&String::from_utf8_lossy(&errb.buf)));
    o.insert(
        "timed_out".to_string(),
        jstr(if timed_out { "b:true" } else { "b:false" }),
    );
    o.insert(
        "stdout_truncated".to_string(),
        jstr(if out.truncated { "b:true" } else { "b:false" }),
    );
    o.insert(
        "stderr_truncated".to_string(),
        jstr(if errb.truncated { "b:true" } else { "b:false" }),
    );
    o.insert("max_stdout".to_string(), jstr(&format!("i:{max_out}")));
    o.insert("max_stderr".to_string(), jstr(&format!("i:{max_err}")));
    o.insert(
        "stdin_bytes".to_string(),
        jstr(&format!("i:{}", stdin_len)),
    );
    send_json_obj(200, &o)
}

/// Wait up to `timeout` seconds; on expiry kill the whole process group.
fn wait_bounded(child: &mut std::process::Child, timeout: f64) -> (bool, i32) {
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs_f64(timeout.max(0.0));
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return (false, status.code().unwrap_or(0)),
            Ok(None) => {}
            Err(_) => return (false, 0),
        }
        if std::time::Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let end = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(_)) | Err(_) => break,
            Ok(None) => {}
        }
        if std::time::Instant::now() >= end {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    (true, 124)
}

/// Join a reader thread, bounded (a surviving background child may hold
/// the pipe open; mirrors the 5s drain join).
fn join_bounded(h: std::thread::JoinHandle<()>) {
    let end = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while !h.is_finished() {
        if std::time::Instant::now() >= end {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let _ = h.join();
}

trait PreExecSetsId {
    fn pre_exec_sets_id(&mut self) -> &mut Self;
}

impl PreExecSetsId for std::process::Command {
    fn pre_exec_sets_id(&mut self) -> &mut Self {
        use std::os::unix::process::CommandExt;
        // SAFETY: setsid() is async-signal-safe; no heap allocation here.
        unsafe {
            self.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        self
    }
}
