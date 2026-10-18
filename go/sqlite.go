package main

import (
	"bytes"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	_ "modernc.org/sqlite"
)

func sqliteAvailable() bool {
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		return false
	}
	defer db.Close()
	return db.Ping() == nil
}

func handleSqliteCreate(w http.ResponseWriter, r *http.Request, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if lexists(abs) {
		sendErr(w, 409, "Database file already exists.", abs)
		return
	}
	parent := filepath.Dir(abs)
	if st, err := os.Stat(parent); err != nil || !st.IsDir() {
		sendErr(w, 404, "Parent directory does not exist.", abs)
		return
	}
	db, err := sql.Open("sqlite", abs)
	if err != nil {
		sendErr(w, 400, fmt.Sprintf("Cannot create database: %v", err), abs)
		return
	}
	version := ""
	func() {
		defer db.Close()
		var v string
		if err := db.QueryRow("SELECT sqlite_version()").Scan(&v); err != nil {
			version = ""
			return
		}
		version = v
	}()
	if version == "" {
		os.Remove(abs)
		sendErr(w, 400, "Cannot create database", abs)
		return
	}
	var size int64
	if st, err := os.Stat(abs); err == nil {
		size = st.Size()
	}
	sendJSON(w, 201, map[string]any{
		"path": raw, "absolute_path": abs,
		"size": mustEnc(size), "sqlite_version": version,
	})
}

func handleSqliteQuery(w http.ResponseWriter, r *http.Request, q map[string][]string, opts Opts) {
	raw := firstQuery(q, "path")
	if raw == "" {
		sendErr(w, 400, "Missing required query parameter: path", "")
		return
	}
	abs := resolvePath(raw, opts.Cwd)
	if !lexists(abs) {
		sendErr(w, 404, "Database file does not exist", abs)
		return
	}
	if ct := r.Header.Get("Content-Type"); !strings.Contains(ct, "application/json") {
		sendErr(w, 400, "Content-Type must be application/json with body { sql, params?, timeout?, readonly? }.", "")
		return
	}
	body, err := readBody(r)
	if err != nil {
		sendErr(w, 400, "Cannot read body.", "")
		return
	}
	var obj map[string]any
	if len(bytes.TrimSpace(body)) == 0 {
		obj = map[string]any{}
	} else {
		dec := json.NewDecoder(bytes.NewReader(body))
		dec.UseNumber()
		if err := dec.Decode(&obj); err != nil {
			sendErr(w, 400, "Invalid JSON body.", "")
			return
		}
		if obj == nil {
			sendErr(w, 400, "JSON body must be an object.", "")
			return
		}
	}
	sqlText, _ := obj["sql"].(string)
	if strings.TrimSpace(sqlText) == "" {
		// Note: non-string sql also lands here (zero value "").
		if s, ok := obj["sql"].(string); !ok || strings.TrimSpace(s) == "" {
			sendErr(w, 400, "Missing or empty required field: sql.", "")
			return
		}
	}
	params, errMsg := parseParams(obj["params"])
	if errMsg != "" {
		sendErr(w, 400, errMsg, "")
		return
	}
	timeout := sqliteDefaultTimeout
	if v, ok := obj["timeout"]; ok && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		t, err := JDec(s)
		if err != nil {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		f, ok := toFloat(t)
		if !ok {
			sendErr(w, 400, "Field 'timeout' must be a tagged number (i:<int> or f:<float>).", "")
			return
		}
		if !(f > 0 && f <= sqliteMaxTimeout) {
			sendErr(w, 400, "Field 'timeout' must be within (0, 300].", "")
			return
		}
		timeout = f
	}
	readonly := false
	if v, ok := obj["readonly"]; ok && v != nil {
		s, isStr := v.(string)
		if !isStr {
			sendErr(w, 400, "Field 'readonly' must be b:true or b:false.", "")
			return
		}
		t, err := JDec(s)
		if err != nil {
			sendErr(w, 400, "Field 'readonly' must be b:true or b:false.", "")
			return
		}
		b, ok := t.(bool)
		if !ok {
			sendErr(w, 400, "Field 'readonly' must be b:true or b:false.", "")
			return
		}
		readonly = b
	}

	scan := ScanSql(sqlText)
	if len(scan.Statements) != 1 {
		sendErr(w, 400, "Send exactly one SQL statement per request.", abs)
		return
	}
	finalSql, args, errMsg := bindNative(scan.Statements[0], params)
	if errMsg != "" {
		sendErr(w, 400, errMsg, abs)
		return
	}
	rowsMode := rowFirstWords[scan.First] || scan.Returning

	dsn := abs
	if readonly {
		// NOTE: query parameters only work on file: URIs; without the
		// prefix "?mode=ro" would become part of the file name.
		dsn = "file:" + abs + "?mode=ro"
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		sendErr(w, 400, fmt.Sprintf("Cannot open database: %v", err), abs)
		return
	}
	defer db.Close()
	if _, err := db.Exec(fmt.Sprintf("PRAGMA busy_timeout=%d", int(timeout*1000))); err != nil {
		sendErr(w, 400, fmt.Sprintf("Cannot open database: %v", err), abs)
		return
	}
	payload := map[string]any{"path": raw, "absolute_path": abs}
	if rowsMode {
		rows, err := db.Query(finalSql, args...)
		if err != nil {
			sendErr(w, 400, fmt.Sprintf("SQLite error: %v", err), abs)
			return
		}
		defer rows.Close()
		cols, err := rows.Columns()
		if err != nil {
			sendErr(w, 400, fmt.Sprintf("SQLite error: %v", err), abs)
			return
		}
		var out [][]any
		for rows.Next() {
			vals := make([]any, len(cols))
			ptrs := make([]any, len(cols))
			for i := range vals {
				ptrs[i] = &vals[i]
			}
			if err := rows.Scan(ptrs...); err != nil {
				sendErr(w, 400, fmt.Sprintf("SQLite error: %v", err), abs)
				return
			}
			row := make([]any, len(cols))
			for i, v := range vals {
				row[i] = cellToTag(v)
			}
			out = append(out, row)
		}
		if err := rows.Err(); err != nil {
			sendErr(w, 400, fmt.Sprintf("SQLite error: %v", err), abs)
			return
		}
		if out == nil {
			out = [][]any{}
		}
		payload["columns"] = cols
		if payload["columns"] == nil {
			payload["columns"] = []any{}
		}
		payload["rows"] = out
		payload["rowcount"] = mustEnc(int64(len(out)))
	} else {
		res, err := db.Exec(finalSql, args...)
		if err != nil {
			sendErr(w, 400, fmt.Sprintf("SQLite error: %v", err), abs)
			return
		}
		affected, _ := res.RowsAffected()
		var lastId int64
		_ = db.QueryRow("SELECT last_insert_rowid()").Scan(&lastId)
		rc := affected
		if ddlFirstWords[scan.First] {
			rc = -1
		}
		payload["rowcount"] = mustEnc(rc)
		payload["lastrowid"] = mustEnc(lastId)
	}
	sendJSON(w, 200, payload)
}

// parseParams decodes tagged params values (fully-tagged position).
func parseParams(v any) (any, string) {
	if v == nil {
		return nil, ""
	}
	switch t := v.(type) {
	case []any:
		out := make([]any, 0, len(t))
		for _, e := range t {
			s, ok := e.(string)
			if !ok {
				return nil, "Array params must be tagged scalars (i:/f:/s:/b:/null)."
			}
			d, err := JDec(s)
			if err != nil {
				return nil, "Array params must be tagged scalars (i:/f:/s:/b:/null)."
			}
			out = append(out, bindValue(d))
		}
		return out, ""
	case map[string]any:
		out := map[string]any{}
		for k, e := range t {
			s, ok := e.(string)
			if !ok {
				return nil, "Object params must be tagged scalars (i:/f:/s:/b:/null)."
			}
			d, err := JDec(s)
			if err != nil {
				return nil, "Object params must be tagged scalars (i:/f:/s:/b:/null)."
			}
			out[k] = bindValue(d)
		}
		return out, ""
	default:
		return nil, "Field 'params' must be an array or an object."
	}
}

// bindValue maps a decoded tag to a driver value (bool -> 0/1).
func bindValue(v any) any {
	if b, ok := v.(bool); ok {
		if b {
			return int64(1)
		}
		return int64(0)
	}
	return v
}

func cellToTag(v any) string {
	switch t := v.(type) {
	case nil:
		return "null"
	case bool:
		s, _ := JEnc(t)
		return s
	case int64:
		s, _ := JEnc(t)
		return s
	case float64:
		s, _ := JEnc(t)
		return s
	case string:
		return JTagStr(t)
	case []byte:
		return JTagStr(base64.StdEncoding.EncodeToString(t))
	default:
		return JTagStr(asString(v))
	}
}

func asString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
