"""Shell execution handler (mixed into Handler)."""

import base64
import binascii
import json
import os
import signal
import subprocess
import threading
from tags import jdec, jenc
from fsutil import resolve_path


SHELL_DEFAULT_TIMEOUT = 30
SHELL_MAX_TIMEOUT = 300
SHELL_DEFAULT_MAX_OUTPUT = 1024 * 1024
SHELL_MAX_OUTPUT = 100 * 1024 * 1024
def _drain_pipe(pipe, acc, limit, truncated):
    """Read a subprocess pipe to EOF, keeping at most `limit` bytes.
    Extra bytes are discarded (the command still runs to completion).
    truncated[0] is set True when more than `limit` bytes arrived.
    """
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            room = limit - len(acc)
            if room > 0:
                acc += chunk[:room]
            if len(chunk) > max(room, 0):
                truncated[0] = True
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


def _feed_pipe(pipe, data):
    """Write input bytes to a subprocess stdin pipe, then close it.

    Runs in its own thread so a command that never reads cannot block
    the timeout logic; the pipe close (or process death) unblocks it.
    """
    try:
        pipe.write(data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


class HandlerShellMixin:
    """Shell execution handler (mixed into Handler)."""

    def _handle_exec_shell(self):
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
                     "with body { command, timeout?, cwd?, "
                     "max_stdout?, max_stderr?, stdin?, stdin_encoding? }.")
            return
        try:
            obj = json.loads(raw.decode("utf-8") if raw else "{}")
        except (UnicodeDecodeError, ValueError):
            self._send_error_json(400, "Invalid JSON body.")
            return
        if not isinstance(obj, dict):
            self._send_error_json(400, "JSON body must be an object.")
            return

        command = obj.get("command", "")
        if not isinstance(command, str) or not command.strip():
            self._send_error_json(
                400, "Missing or empty required field: command.")
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
            timeout = SHELL_DEFAULT_TIMEOUT
        timeout = max(1, min(float(timeout), SHELL_MAX_TIMEOUT))

        cwd = obj.get("cwd") or os.getcwd()
        if not isinstance(cwd, str):
            self._send_error_json(400, "Field 'cwd' must be a string.")
            return
        cwd = resolve_path(cwd)
        if not os.path.isdir(cwd):
            self._send_error_json(
                400, "Working directory does not exist.", path=cwd)
            return

        def _limit(label, default):
            if label not in obj or obj[label] is None:
                return default
            try:
                value = jdec(obj[label])
            except ValueError:
                self._send_error_json(
                    400, "Field '%s' must be an integer >= 0 "
                         "(i:<int>)." % label)
                return None
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                self._send_error_json(
                    400, "Field '%s' must be an integer >= 0 "
                         "(i:<int>)." % label)
                return None
            if value > SHELL_MAX_OUTPUT:
                self._send_error_json(
                    400, "Field '%s' exceeds the %d byte cap."
                    % (label, SHELL_MAX_OUTPUT))
                return None
            return value

        max_stdout = _limit("max_stdout", SHELL_DEFAULT_MAX_OUTPUT)
        if max_stdout is None:
            return
        max_stderr = _limit("max_stderr", SHELL_DEFAULT_MAX_OUTPUT)
        if max_stderr is None:
            return

        stdin_raw = obj.get("stdin", "")
        if stdin_raw is None:
            stdin_raw = ""
        if not isinstance(stdin_raw, str):
            self._send_error_json(
                400, "Field 'stdin' must be a string.")
            return
        stdin_encoding = obj.get("stdin_encoding", "text")
        if stdin_encoding is None:
            stdin_encoding = "text"
        if not isinstance(stdin_encoding, str):
            self._send_error_json(
                400, "Field 'stdin_encoding' must be text|base64.")
            return
        stdin_encoding = stdin_encoding.lower()
        if stdin_encoding == "text":
            stdin_data = stdin_raw.encode("utf-8")
        elif stdin_encoding == "base64":
            try:
                stdin_data = base64.b64decode(stdin_raw, validate=True)
            except (binascii.Error, ValueError):
                self._send_error_json(
                    400, "Invalid base64 'stdin'.")
                return
        else:
            self._send_error_json(
                400, "Field 'stdin_encoding' must be text|base64.")
            return

        try:
            proc = subprocess.Popen(
                ["bash", "-c", command],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE, cwd=cwd,
                start_new_session=True)
        except OSError as exc:
            self._send_error_json(400, "Cannot start shell: %s" % exc)
            return
        out_acc, err_acc = bytearray(), bytearray()
        out_cut, err_cut = [False], [False]
        drainers = [
            threading.Thread(target=_drain_pipe,
                             args=(proc.stdout, out_acc, max_stdout, out_cut),
                             daemon=True),
            threading.Thread(target=_drain_pipe,
                             args=(proc.stderr, err_acc, max_stderr, err_cut),
                             daemon=True),
            threading.Thread(target=_feed_pipe,
                             args=(proc.stdin, stdin_data),
                             daemon=True),
        ]
        for thread in drainers:
            thread.start()
        try:
            proc.wait(timeout=timeout)
            timed_out = False
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.stdin.close()  # unblock the feeder thread
            except (OSError, ValueError):
                pass
            proc.wait()
            timed_out = True
            returncode = 124
        for thread in drainers:
            thread.join(timeout=5)
        self._send_json({
            "command": command,
            "cwd": cwd,
            "returncode": jenc(returncode),
            "stdout": bytes(out_acc).decode("utf-8", errors="replace"),
            "stderr": bytes(err_acc).decode("utf-8", errors="replace"),
            "timed_out": jenc(timed_out),
            "stdout_truncated": jenc(out_cut[0]),
            "stderr_truncated": jenc(err_cut[0]),
            "max_stdout": jenc(max_stdout),
            "max_stderr": jenc(max_stderr),
            "stdin_bytes": jenc(len(stdin_data)),
        })
