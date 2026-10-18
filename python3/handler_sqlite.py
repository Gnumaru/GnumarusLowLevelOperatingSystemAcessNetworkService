"""SQLite endpoint handlers (mixed into Handler)."""

import base64
import json
import os
import sqlite3
from urllib.parse import quote
from fsutil import guess_mime, resolve_path
from tags import jdec, jenc, jtag_str


def _sqlite_cell(value):
    """Map a SQLite cell to its wire form (rows are fully tagged)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return jenc(value)
    if isinstance(value, int):
        return jenc(value)
    if isinstance(value, float):
        return jenc(value)
    if isinstance(value, bytes):
        return jtag_str(base64.b64encode(value).decode("ascii"))
    if isinstance(value, str):
        return jtag_str(value)
    return jtag_str(str(value))


class HandlerSqliteMixin:
    """SQLite endpoint handlers (mixed into Handler)."""

    def _handle_sqlite_create(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if os.path.lexists(abs_path):
            self._send_error_json(409, "Database file already exists.",
                                  path=abs_path)
            return
        parent = os.path.dirname(abs_path)
        if not os.path.isdir(parent):
            self._send_error_json(
                404, "Parent directory does not exist.", path=abs_path)
            return
        try:
            conn = sqlite3.connect(abs_path, timeout=5)
            try:
                version = conn.execute(
                    "SELECT sqlite_version()").fetchone()[0]
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            try:
                os.remove(abs_path)
            except OSError:
                pass
            self._send_error_json(
                400, "Cannot create database: %s" % exc, path=abs_path)
            return
        except OSError as exc:
            self._send_error_json(403, "Cannot create database: %s" % exc,
                                  path=abs_path)
            return
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0
        self._send_json({
            "path": raw_paths[0],
            "absolute_path": abs_path,
            "size": jenc(size),
            "sqlite_version": version,
        }, status=201)

    def _handle_sqlite_query(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if not os.path.lexists(abs_path):
            self._send_error_json(404, "Database file does not exist",
                                  path=abs_path)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            self._send_error_json(
                400, "Content-Type must be application/json "
                     "with body { sql, params?, timeout?, readonly? }.")
            return
        try:
            obj = json.loads(raw.decode("utf-8") if raw else "{}")
        except (UnicodeDecodeError, ValueError):
            self._send_error_json(400, "Invalid JSON body.")
            return
        if not isinstance(obj, dict):
            self._send_error_json(400, "JSON body must be an object.")
            return

        sql = obj.get("sql", "")
        if not isinstance(sql, str) or not sql.strip():
            self._send_error_json(
                400, "Missing or empty required field: sql.")
            return

        params = obj.get("params", [])
        if params is None:
            params = []
        if isinstance(params, list):
            try:
                params = [jdec(v) for v in params]
            except ValueError:
                self._send_error_json(
                    400, "Array params must be tagged scalars "
                         "(i:/f:/s:/b:/null).")
                return
        elif isinstance(params, dict):
            try:
                params = {k: jdec(v) for k, v in params.items()}
            except ValueError:
                self._send_error_json(
                    400, "Object params must be tagged scalars "
                         "(i:/f:/s:/b:/null).")
                return
        else:
            self._send_error_json(
                400, "Field 'params' must be an array or an object.")
            return

        if "timeout" in obj and obj["timeout"] is not None:
            try:
                timeout = jdec(obj["timeout"])
            except ValueError:
                self._send_error_json(
                    400, "Field 'timeout' must be a tagged number "
                         "(i:<int> or f:<float>).")
                return
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                self._send_error_json(
                    400, "Field 'timeout' must be a tagged number "
                         "(i:<int> or f:<float>).")
                return
        else:
            timeout = 5
        timeout = float(timeout)
        if not 0 < timeout <= 300:
            self._send_error_json(
                400, "Field 'timeout' must be within (0, 300].")
            return

        if "readonly" in obj and obj["readonly"] is not None:
            try:
                readonly = jdec(obj["readonly"])
            except ValueError:
                self._send_error_json(
                    400, "Field 'readonly' must be b:true or b:false.")
                return
            if not isinstance(readonly, bool):
                self._send_error_json(
                    400, "Field 'readonly' must be b:true or b:false.")
                return
        else:
            readonly = False

        try:
            if readonly:
                from urllib.parse import quote as _quote
                conn = sqlite3.connect(
                    "file:%s?mode=ro" % _quote(abs_path), uri=True,
                    timeout=timeout)
            else:
                conn = sqlite3.connect(abs_path, timeout=timeout)
        except sqlite3.Error as exc:
            self._send_error_json(
                400, "Cannot open database: %s" % exc, path=abs_path)
            return
        try:
            try:
                cursor = conn.execute(sql, params)
            except sqlite3.Warning:
                self._send_error_json(
                    400, "Send exactly one SQL statement per request.",
                    path=abs_path)
                return
            except sqlite3.Error as exc:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                self._send_error_json(400, "SQLite error: %s" % exc,
                                      path=abs_path)
                return
            if cursor.description is None:
                conn.commit()
                payload = {
                    "rowcount": jenc(cursor.rowcount),
                    "lastrowid": jenc(cursor.lastrowid),
                }
            else:
                columns = [info[0] for info in cursor.description]
                rows = [[_sqlite_cell(value) for value in row]
                        for row in cursor.fetchall()]
                payload = {
                    "columns": columns,
                    "rows": rows,
                    "rowcount": jenc(len(rows)),
                }
        finally:
            conn.close()
        payload["path"] = raw_paths[0]
        payload["absolute_path"] = abs_path
        self._send_json(payload)
