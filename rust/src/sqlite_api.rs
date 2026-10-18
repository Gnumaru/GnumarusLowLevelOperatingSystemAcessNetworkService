//! SQLite endpoints (rusqlite, one fresh connection per request).

use crate::api::{err, jstr, resolve_path, send_json_obj, State};
use crate::http::{Request, Response};
use crate::tags::{jdec, Tagged};
use rusqlite::Connection;

fn req_obj(req: &Request) -> Result<serde_json::Map<String, serde_json::Value>, Response> {
    let ct = req.headers.get("content-type").map(|s| s.as_str()).unwrap_or("");
    if !ct.contains("application/json") {
        return Err(err(
            400,
            "Content-Type must be application/json with body { sql, params?, timeout?, readonly? }.",
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

pub fn handle_create(req: &Request, state: &State) -> Response {
    let raw = match req.query.get("path") {
        Some(p) if !p.is_empty() => p.clone(),
        _ => return err(400, "Missing required query parameter: path", None),
    };
    let abs = resolve_path(&raw, &state.cwd);
    if crate::api::lexists(&abs) {
        return err(409, "Database file already exists.", Some(&abs));
    }
    let parent = std::path::Path::new(&abs)
        .parent()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    if !std::path::Path::new(&parent).is_dir() {
        return err(404, "Parent directory does not exist.", Some(&abs));
    }
    let version: String = match (|| -> Result<String, rusqlite::Error> {
        let conn = Connection::open(&abs)?;
        let v: String = conn.query_row("SELECT sqlite_version()", [], |r| r.get(0))?;
        conn.close().map_err(|(_, e)| e)?;
        Ok(v)
    })() {
        Ok(v) => v,
        Err(e) => {
            let _ = std::fs::remove_file(&abs);
            return err(400, &format!("Cannot create database: {e}"), Some(&abs));
        }
    };
    let size = std::fs::metadata(&abs).map(|m| m.len() as i64).unwrap_or(0);
    let mut o = serde_json::Map::new();
    o.insert("path".to_string(), jstr(&raw));
    o.insert("absolute_path".to_string(), jstr(&abs));
    o.insert("size".to_string(), jstr(&format!("i:{size}")));
    o.insert("sqlite_version".to_string(), jstr(&version));
    send_json_obj(201, &o)
}

fn cell_to_tag(v: rusqlite::types::Value) -> String {
    use rusqlite::types::Value;
    match v {
        Value::Null => "null".to_string(),
        Value::Integer(n) => format!("i:{n}"),
        Value::Real(f) => format!("f:{f:?}"),
        Value::Text(s) => format!("s:{s}"),
        Value::Blob(b) => format!("s:{}", crate::codec::b64_encode(&b)),
    }
}

pub fn handle_query(req: &Request, state: &State) -> Response {
    let raw = match req.query.get("path") {
        Some(p) if !p.is_empty() => p.clone(),
        _ => return err(400, "Missing required query parameter: path", None),
    };
    let abs = resolve_path(&raw, &state.cwd);
    if !crate::api::lexists(&abs) {
        return err(404, "Database file does not exist", Some(&abs));
    }
    let obj = match req_obj(req) {
        Ok(o) => o,
        Err(r) => return r,
    };
    let sql = match obj.get("sql") {
        Some(serde_json::Value::String(s)) if !s.trim().is_empty() => s.clone(),
        _ => return err(400, "Missing or empty required field: sql.", None),
    };

    // Params: fully-tagged values -> driver values.
    enum Params {
        None,
        Pos(Vec<rusqlite::types::Value>),
        Named(Vec<(String, rusqlite::types::Value)>),
    }
    let params = match obj.get("params") {
        None | Some(serde_json::Value::Null) => Params::None,
        Some(serde_json::Value::Array(items)) => {
            let mut out = Vec::with_capacity(items.len());
            for v in items {
                let serde_json::Value::String(s) = v else {
                    return err(
                        400,
                        "Array params must be tagged scalars (i:/f:/s:/b:/null).",
                        None,
                    );
                };
                match jdec(s) {
                    Ok(t) => out.push(match t {
                        Tagged::Null => rusqlite::types::Value::Null,
                        Tagged::Bool(b) => rusqlite::types::Value::Integer(b as i64),
                        Tagged::Int(n) => rusqlite::types::Value::Integer(n),
                        Tagged::Float(f) => rusqlite::types::Value::Real(f),
                        Tagged::Str(st) => rusqlite::types::Value::Text(st),
                    }),
                    Err(_) => {
                        return err(
                            400,
                            "Array params must be tagged scalars (i:/f:/s:/b:/null).",
                            None,
                        )
                    }
                }
            }
            Params::Pos(out)
        }
        Some(serde_json::Value::Object(map)) => {
            let mut out = Vec::with_capacity(map.len());
            for (k, v) in map {
                let serde_json::Value::String(s) = v else {
                    return err(
                        400,
                        "Object params must be tagged scalars (i:/f:/s:/b:/null).",
                        None,
                    );
                };
                match jdec(s) {
                    Ok(t) => out.push((
                        // rusqlite matches named params by their full
                        // placeholder text (":a", "@a", "$a").
                        if k.starts_with([':', '@', '$']) {
                            k.clone()
                        } else {
                            format!(":{k}")
                        },
                        match t {
                            Tagged::Null => rusqlite::types::Value::Null,
                            Tagged::Bool(b) => rusqlite::types::Value::Integer(b as i64),
                            Tagged::Int(n) => rusqlite::types::Value::Integer(n),
                            Tagged::Float(f) => rusqlite::types::Value::Real(f),
                            Tagged::Str(st) => rusqlite::types::Value::Text(st),
                        },
                    )),
                    Err(_) => {
                        return err(
                            400,
                            "Object params must be tagged scalars (i:/f:/s:/b:/null).",
                            None,
                        )
                    }
                }
            }
            Params::Named(out)
        }
        Some(_) => {
            return err(400, "Field 'params' must be an array or an object.", None);
        }
    };

    let timeout = match obj.get("timeout") {
        None | Some(serde_json::Value::Null) => 5.0,
        Some(serde_json::Value::String(s)) => match jdec(s) {
            Ok(Tagged::Int(n)) => n as f64,
            Ok(Tagged::Float(f)) => f,
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
    if !(timeout > 0.0 && timeout <= 300.0) {
        return err(400, "Field 'timeout' must be within (0, 300].", None);
    }

    let readonly = match obj.get("readonly") {
        None | Some(serde_json::Value::Null) => false,
        Some(serde_json::Value::String(s)) => match jdec(s) {
            Ok(Tagged::Bool(b)) => b,
            _ => {
                return err(400, "Field 'readonly' must be b:true or b:false.", None);
            }
        },
        Some(_) => {
            return err(400, "Field 'readonly' must be b:true or b:false.", None);
        }
    };

    // Parse + classify with sqlparser (single statement enforced).
    let dialect = sqlparser::dialect::SQLiteDialect {};
    let stmts = match sqlparser::parser::Parser::parse_sql(&dialect, &sql) {
        Ok(s) => s,
        Err(e) => {
            // PRAGMA and friends are outside the grammar: rows-mode by
            // first word, single statement enforced by re-parse failure
            // shape (best effort, documented).
            if first_word(&sql) == "PRAGMA" {
                return pragma_query(&abs, &raw, &sql, timeout, readonly);
            }
            return err(400, &format!("SQLite error: {e}"), Some(&abs));
        }
    };
    if stmts.len() != 1 {
        return err(400, "Send exactly one SQL statement per request.", Some(&abs));
    }
    let stmt = &stmts[0];
    let rows_mode = matches!(
        stmt,
        sqlparser::ast::Statement::Query(_)
            | sqlparser::ast::Statement::Explain { .. }
            | sqlparser::ast::Statement::ExplainTable { .. }
    );
    let is_dml = matches!(
        stmt,
        sqlparser::ast::Statement::Insert(_)
            | sqlparser::ast::Statement::Update { .. }
            | sqlparser::ast::Statement::Delete(_)
            | sqlparser::ast::Statement::Merge { .. }
    );

    let open = |ro: bool| -> Result<rusqlite::Connection, rusqlite::Error> {
        let flags = if ro {
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
        } else {
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                | rusqlite::OpenFlags::SQLITE_OPEN_CREATE
        };
        let conn = rusqlite::Connection::open_with_flags(&abs, flags)?;
        conn.busy_timeout(std::time::Duration::from_secs_f64(timeout))?;
        Ok(conn)
    };
    let conn = match open(readonly) {
        Ok(c) => c,
        Err(e) => return err(400, &format!("Cannot open database: {e}"), Some(&abs)),
    };
    // NOTE: connection closes on drop (end of scope) -> stateless.
    if rows_mode {
        let mut prepared = match conn.prepare(&sql) {
            Ok(s) => s,
            Err(e) => return err(400, &format!("SQLite error: {e}"), Some(&abs)),
        };
        let columns: Vec<String> = (0..prepared.column_count())
            .map(|i| prepared.column_name(i).unwrap_or("").to_string())
            .collect();
        // Bind params for the single statement.
        let rows = match &params {
            Params::None => prepared.query(rusqlite::params![]),
            Params::Pos(list) => {
                let refs: Vec<&dyn rusqlite::ToSql> =
                    list.iter().map(|v| v as &dyn rusqlite::ToSql).collect();
                prepared.query(refs.as_slice())
            }
            Params::Named(map) => {
                let refs: Vec<(&str, &dyn rusqlite::ToSql)> = map
                    .iter()
                    .map(|(k, v)| (k.as_str(), v as &dyn rusqlite::ToSql))
                    .collect();
                prepared.query(refs.as_slice())
            }
        };
        let mut rows = match rows {
            Ok(r) => r,
            Err(e) => return err(400, &format!("SQLite error: {e}"), Some(&abs)),
        };
        let mut out: Vec<serde_json::Value> = Vec::new();
        loop {
            match rows.next() {
                Ok(Some(row)) => {
                    let mut cells = Vec::new();
                    for i in 0..columns.len() {
                        let v: rusqlite::types::Value = row.get(i).unwrap_or(rusqlite::types::Value::Null);
                        cells.push(serde_json::Value::String(cell_to_tag(v)));
                    }
                    out.push(serde_json::Value::Array(cells));
                }
                Ok(None) => break,
                Err(e) => return err(400, &format!("SQLite error: {e}"), Some(&abs)),
            }
        }
        let mut o = serde_json::Map::new();
        o.insert(
            "columns".to_string(),
            serde_json::Value::Array(
                columns.into_iter().map(serde_json::Value::String).collect(),
            ),
        );
        o.insert("rows".to_string(), serde_json::Value::Array(out.clone()));
        o.insert("rowcount".to_string(), jstr(&format!("i:{}", out.len())));
        o.insert("path".to_string(), jstr(&raw));
        o.insert("absolute_path".to_string(), jstr(&abs));
        send_json_obj(200, &o)
    } else {
        let affected_raw = match &params {
            Params::None => conn.execute(&sql, rusqlite::params![]),
            Params::Pos(list) => {
                let refs: Vec<&dyn rusqlite::ToSql> =
                    list.iter().map(|v| v as &dyn rusqlite::ToSql).collect();
                conn.execute(&sql, refs.as_slice())
            }
            Params::Named(map) => {
                let refs: Vec<(&str, &dyn rusqlite::ToSql)> = map
                    .iter()
                    .map(|(k, v)| (k.as_str(), v as &dyn rusqlite::ToSql))
                    .collect();
                conn.execute(&sql, refs.as_slice())
            }
        };
        let affected: i64 = match affected_raw {
            Ok(n) => n as i64,
            Err(e) => return err(400, &format!("SQLite error: {e}"), Some(&abs)),
        };
        let last_id: i64 = conn.last_insert_rowid();
        let rc = if is_dml { affected } else { -1 };
        let mut o = serde_json::Map::new();
        o.insert("rowcount".to_string(), jstr(&format!("i:{rc}")));
        o.insert("lastrowid".to_string(), jstr(&format!("i:{last_id}")));
        o.insert("path".to_string(), jstr(&raw));
        o.insert("absolute_path".to_string(), jstr(&abs));
        send_json_obj(200, &o)
    }
}

/// PRAGMA fallback: sqlparser has no PRAGMA grammar. Rows-mode, single
/// statement required (a second statement makes prepare fail rowless).
fn pragma_query(abs: &str, raw: &str, sql: &str, timeout: f64, readonly: bool) -> crate::http::Response {
    // Reject anything that is not exactly one PRAGMA statement by
    // ensuring no top-level semicolon with trailing content.
    let mut chars = sql.chars().peekable();
    let mut in_str = false;
    let mut semis = 0;
    let mut after_last = String::new();
    while let Some(c) = chars.next() {
        if in_str {
            if c == '\'' {
                if chars.peek() == Some(&'\'') {
                    chars.next();
                } else {
                    in_str = false;
                }
            }
            continue;
        }
        match c {
            '\'' => in_str = true,
            ';' => {
                semis += 1;
                after_last.clear();
            }
            _ => after_last.push(c),
        }
    }
    if semis > 1 || (semis == 1 && !after_last.trim().is_empty()) {
        return err(400, "Send exactly one SQL statement per request.", Some(abs));
    }
    let open = || -> Result<rusqlite::Connection, rusqlite::Error> {
        let flags = if readonly {
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
        } else {
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                | rusqlite::OpenFlags::SQLITE_OPEN_CREATE
        };
        let conn = rusqlite::Connection::open_with_flags(abs, flags)?;
        conn.busy_timeout(std::time::Duration::from_secs_f64(timeout))?;
        Ok(conn)
    };
    let conn = match open() {
        Ok(c) => c,
        Err(e) => return err(400, &format!("Cannot open database: {e}"), Some(abs)),
    };
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(e) => return err(400, &format!("SQLite error: {e}"), Some(abs)),
    };
    let columns: Vec<String> = (0..stmt.column_count())
        .map(|i| stmt.column_name(i).unwrap_or("").to_string())
        .collect();
    let mut rows = match stmt.query(rusqlite::params![]) {
        Ok(r) => r,
        Err(e) => return err(400, &format!("SQLite error: {e}"), Some(abs)),
    };
    let mut out: Vec<serde_json::Value> = Vec::new();
    loop {
        match rows.next() {
            Ok(Some(row)) => {
                let mut cells = Vec::new();
                for i in 0..columns.len() {
                    let v: rusqlite::types::Value =
                        row.get(i).unwrap_or(rusqlite::types::Value::Null);
                    cells.push(serde_json::Value::String(cell_to_tag(v)));
                }
                out.push(serde_json::Value::Array(cells));
            }
            Ok(None) => break,
            Err(e) => return err(400, &format!("SQLite error: {e}"), Some(abs)),
        }
    }
    let mut o = serde_json::Map::new();
    o.insert(
        "columns".to_string(),
        serde_json::Value::Array(columns.into_iter().map(serde_json::Value::String).collect()),
    );
    o.insert("rows".to_string(), serde_json::Value::Array(out.clone()));
    o.insert("rowcount".to_string(), jstr(&format!("i:{}", out.len())));
    o.insert("path".to_string(), jstr(raw));
    o.insert("absolute_path".to_string(), jstr(abs));
    send_json_obj(200, &o)
}

fn first_word(sql: &str) -> String {
    let mut s = sql.trim_start();
    loop {
        if s.starts_with("--") {
            s = match s.find('\n') {
                Some(i) => s[i + 1..].trim_start(),
                None => return String::new(),
            };
        } else if s.starts_with("/*") {
            s = match s.find("*/") {
                Some(i) => s[i + 2..].trim_start(),
                None => return String::new(),
            };
        } else {
            break;
        }
    }
    s.split(|c: char| !c.is_alphanumeric() && c != '_')
        .next()
        .unwrap_or("")
        .to_uppercase()
}
