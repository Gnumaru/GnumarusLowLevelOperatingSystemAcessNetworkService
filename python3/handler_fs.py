"""Filesystem endpoint handlers (mixed into Handler)."""

import base64
import binascii
import json
import os
import stat
from fsutil import entry_type, guess_mime, resolve_path
from tags import jenc
from times import _birthtime



class HandlerFsMixin:
    """Filesystem endpoint handlers (mixed into Handler)."""

    def _handle_read_dir(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if not os.path.lexists(abs_path):
            self._send_error_json(404, "Path does not exist", path=abs_path)
            return
        if not os.path.isdir(abs_path):
            self._send_error_json(400, "Path is not a directory", path=abs_path)
            return
        try:
            names = sorted(os.listdir(abs_path))
        except PermissionError:
            self._send_error_json(403, "Permission denied", path=abs_path)
            return
        except OSError as exc:
            self._send_error_json(403, "Cannot list directory: %s" % exc,
                                  path=abs_path)
            return

        entries = []
        for name in names:
            full = os.path.join(abs_path, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            entries.append({
                "name": name,
                "path": full,
                "type": entry_type(full, st),
                "size": jenc(st.st_size),
                "atime": jenc(st.st_atime),
                "mtime": jenc(st.st_mtime),
                "ctime": jenc(st.st_ctime),
                "crtime": jenc(_birthtime(full, st)),
                "mode": oct(stat.S_IMODE(st.st_mode)),
            })
        self._send_json({
            "path": raw_paths[0],
            "absolute_path": abs_path,
            "count": jenc(len(entries)),
            "entries": entries,
        })

    def _handle_read_file(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if not os.path.lexists(abs_path):
            self._send_error_json(404, "Path does not exist", path=abs_path)
            return
        if os.path.isdir(abs_path) and not os.path.islink(abs_path):
            self._send_error_json(400, "Path is a directory, not a file",
                                  path=abs_path)
            return

        encoding = (query.get("encoding", ["auto"])[0] or "auto").lower()
        if encoding not in ("auto", "text", "base64"):
            self._send_error_json(
                400, "Invalid encoding param: %r (use auto|text|base64)" % encoding)
            return
        raw_mode = query.get("raw", ["0"])[0] == "1"

        try:
            with open(abs_path, "rb") as fh:
                data = fh.read()
        except PermissionError:
            self._send_error_json(403, "Permission denied", path=abs_path)
            return
        except IsADirectoryError:
            self._send_error_json(400, "Path is a directory, not a file",
                                  path=abs_path)
            return
        except OSError as exc:
            self._send_error_json(403, "Cannot read file: %s" % exc,
                                  path=abs_path)
            return

        try:
            st = os.lstat(abs_path)
            mtime = st.st_mtime
        except OSError:
            mtime = None

        if raw_mode:
            self.send_response(200)
            self.send_header("Content-Type", guess_mime(abs_path))
            self.send_header("Content-Length", str(len(data)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(data)
            return

        mime_type = guess_mime(abs_path)
        if encoding == "text":
            self._send_json({
                "path": raw_paths[0],
                "absolute_path": abs_path,
                "size": jenc(len(data)),
                "mtime": jenc(mtime),
                "mime_type": mime_type,
                "is_binary": jenc(False),
                "encoding": "utf-8",
                "content": data.decode("utf-8", errors="replace"),
            })
        elif encoding == "base64":
            self._send_json({
                "path": raw_paths[0],
                "absolute_path": abs_path,
                "size": jenc(len(data)),
                "mtime": jenc(mtime),
                "mime_type": mime_type,
                "is_binary": jenc(True),
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
            })
        else:  # auto
            try:
                text = data.decode("utf-8")
                # Heuristic: reject control chars typical of binaries
                # (except \t \n \r) so binaries are base64-encoded.
                if any(ord(ch) < 32 and ch not in ("\t", "\n", "\r")
                       for ch in text):
                    raise ValueError("binary content")
                self._send_json({
                    "path": raw_paths[0],
                    "absolute_path": abs_path,
                    "size": jenc(len(data)),
                    "mtime": jenc(mtime),
                    "mime_type": mime_type,
                    "is_binary": jenc(False),
                    "encoding": "utf-8",
                    "content": text,
                })
            except (UnicodeDecodeError, ValueError):
                self._send_json({
                    "path": raw_paths[0],
                    "absolute_path": abs_path,
                    "size": jenc(len(data)),
                    "mtime": jenc(mtime),
                    "mime_type": mime_type,
                    "is_binary": jenc(True),
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode("ascii"),
                })

    def _parse_write_body(self):
        """Parse a POST/PUT body into raw bytes.

        JSON bodies (Content-Type: application/json) accept
        { content, encoding } with encoding text|base64 (default text).
        Any other Content-Type is stored as raw bytes.
        Empty/absent body means zero bytes.
        Returns bytes, or None after sending a 400 error.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            return body
        try:
            obj = json.loads(body.decode("utf-8") if body else "{}")
        except (UnicodeDecodeError, ValueError):
            self._send_error_json(400, "Invalid JSON body.")
            return None
        if not isinstance(obj, dict):
            self._send_error_json(400, "JSON body must be an object.")
            return None
        content = obj.get("content", "")
        encoding = str(obj.get("encoding", "text")).lower()
        if encoding == "text":
            if not isinstance(content, str):
                self._send_error_json(
                    400, "'content' must be a string for text encoding.")
                return None
            return content.encode("utf-8")
        if encoding == "base64":
            if not isinstance(content, str):
                self._send_error_json(
                    400, "'content' must be a base64 string.")
                return None
            try:
                return base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                self._send_error_json(400, "Invalid base64 'content'.")
                return None
        self._send_error_json(
            400, "Invalid encoding: %r (use text|base64)." % encoding)
        return None

    def _handle_create_file(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if os.path.lexists(abs_path):
            self._send_error_json(
                409, "Path already exists (use PUT to overwrite).",
                path=abs_path)
            return
        parent = os.path.dirname(abs_path)
        if not os.path.isdir(parent):
            self._send_error_json(
                404, "Parent directory does not exist.", path=abs_path)
            return
        data = self._parse_write_body()
        if data is None:
            return
        try:
            with open(abs_path, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            self._send_error_json(
                409, "Path already exists (use PUT to overwrite).",
                path=abs_path)
            return
        except FileNotFoundError:
            self._send_error_json(
                404, "Parent directory does not exist.", path=abs_path)
            return
        except PermissionError:
            self._send_error_json(403, "Permission denied", path=abs_path)
            return
        except OSError as exc:
            self._send_error_json(400, "Cannot create file: %s" % exc,
                                  path=abs_path)
            return
        self._send_json({
            "path": raw_paths[0],
            "absolute_path": abs_path,
            "size": jenc(len(data)),
            "mime_type": guess_mime(abs_path),
        }, status=201)

    def _handle_overwrite_file(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if not os.path.lexists(abs_path):
            self._send_error_json(404, "Path does not exist", path=abs_path)
            return
        if os.path.isdir(abs_path) and not os.path.islink(abs_path):
            self._send_error_json(400, "Path is a directory, not a file",
                                  path=abs_path)
            return
        data = self._parse_write_body()
        if data is None:
            return
        try:
            with open(abs_path, "wb") as fh:
                fh.write(data)
        except PermissionError:
            self._send_error_json(403, "Permission denied", path=abs_path)
            return
        except IsADirectoryError:
            self._send_error_json(400, "Path is a directory, not a file",
                                  path=abs_path)
            return
        except OSError as exc:
            self._send_error_json(400, "Cannot write file: %s" % exc,
                                  path=abs_path)
            return
        self._send_json({
            "path": raw_paths[0],
            "absolute_path": abs_path,
            "size": jenc(len(data)),
            "mime_type": guess_mime(abs_path),
        })

    def _handle_delete_file(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if not os.path.lexists(abs_path):
            self._send_error_json(404, "Path does not exist", path=abs_path)
            return
        if os.path.isdir(abs_path) and not os.path.islink(abs_path):
            self._send_error_json(
                400, "Path is a directory, not a file "
                     "(use DELETE /api/dir).", path=abs_path)
            return
        try:
            os.remove(abs_path)
        except FileNotFoundError:
            self._send_error_json(404, "Path does not exist", path=abs_path)
            return
        except PermissionError:
            self._send_error_json(403, "Permission denied", path=abs_path)
            return
        except OSError as exc:
            self._send_error_json(400, "Cannot delete file: %s" % exc,
                                  path=abs_path)
            return
        self._send_json({
            "path": raw_paths[0],
            "absolute_path": abs_path,
            "deleted": jenc(True),
        })

    def _handle_delete_dir(self, query):
        raw_paths = query.get("path")
        if not raw_paths or not raw_paths[0]:
            self._send_error_json(400, "Missing required query parameter: path")
            return
        abs_path = resolve_path(raw_paths[0])
        if not os.path.lexists(abs_path):
            self._send_error_json(404, "Path does not exist", path=abs_path)
            return
        if os.path.islink(abs_path) or not os.path.isdir(abs_path):
            self._send_error_json(400, "Path is not a directory",
                                  path=abs_path)
            return
        try:
            os.rmdir(abs_path)
        except FileNotFoundError:
            self._send_error_json(404, "Path does not exist", path=abs_path)
            return
        except PermissionError:
            self._send_error_json(403, "Permission denied", path=abs_path)
            return
        except OSError as exc:
            self._send_error_json(400, "Cannot delete directory: %s" % exc,
                                  path=abs_path)
            return
        self._send_json({
            "path": raw_paths[0],
            "absolute_path": abs_path,
            "deleted": jenc(True),
        })
