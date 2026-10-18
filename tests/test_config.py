"""Tests for multi-source configuration (CLI > env > stdin > file).

Spawns real servers and observes the flushed `config sources:` summary
plus HTTP behavior. A pre-existing config.cfg (GNATIVE_TEST_CFG, else
python3/config.cfg) is backed up and restored; GNUMARUS_* vars are
scrubbed from spawned environments.
"""

import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "python3", "server.py")
CFG_PATH = os.path.join(ROOT, "python3", "config.cfg")


def server_cmd():
    """Launch prefix for the server under test (override for ports)."""
    raw = os.environ.get("GNATIVE_TEST_SERVER_CMD")
    if raw:
        import shlex
        return shlex.split(raw)
    return [sys.executable, SERVER]


def cfg_path():
    return os.environ.get("GNATIVE_TEST_CFG") or CFG_PATH


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_get(port, target, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": "Bearer " + token} if token else {}
    conn.request("GET", target, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


class ConfigCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(cfg_path()):
            with open(cfg_path(), "rb") as fh:
                cls.backup = fh.read()
        else:
            cls.backup = None

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.backup is None:
                os.remove(cfg_path())
            else:
                with open(cfg_path(), "wb") as fh:
                    fh.write(cls.backup)
        except OSError:
            pass

    def setUp(self):
        self.procs = []
        self.stdin_files = []
        try:
            os.remove(cfg_path())
        except OSError:
            pass

    def tearDown(self):
        for proc in self.procs:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        for tmp in self.stdin_files:
            try:
                os.remove(tmp)
            except OSError:
                pass
        try:
            os.remove(cfg_path())
        except OSError:
            pass

    def write_cfg(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(cfg_path(), "wb") as fh:
            fh.write(data)

    def spawn(self, cli=(), env=None, stdin_data=None, probe_port=None):
        """Start a server; return (proc, summary_dict).

        probe_port: when given, wait until GET / answers 200 there
        (the summary is printed before listen(), so without this the
        first request races the bind).
        """
        cmd = server_cmd() + list(cli)
        child_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("GNUMARUS_")}
        child_env.update(env or {})
        if stdin_data is None:
            stdin_arg = subprocess.DEVNULL
        else:
            # Regular file, not a pipe: the write completes instantly and
            # the server observes EOF deterministically (no feeder thread,
            # no pipe-buffer races either way).
            if isinstance(stdin_data, str):
                stdin_data = stdin_data.encode("utf-8")
            tmpdir = os.path.join(ROOT, "tmp")
            os.makedirs(tmpdir, exist_ok=True)
            fd, tmppath = tempfile.mkstemp(dir=tmpdir, prefix="stdin_")
            with os.fdopen(fd, "wb") as fh:
                fh.write(stdin_data)
            self.stdin_files.append(tmppath)
            stdin_arg = open(tmppath, "rb")
        try:
            proc = subprocess.Popen(
                cmd, cwd=ROOT, stdin=stdin_arg,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=child_env)
        finally:
            if stdin_data is not None:
                stdin_arg.close()
        self.procs.append(proc)
        summary = self._read_summary(proc)
        self.assertIsNotNone(summary, "no `config sources:` summary printed")
        if probe_port is not None:
            deadline = time.time() + 15
            while True:
                if proc.poll() is not None:
                    raise AssertionError(
                        "server exited before listening")
                try:
                    if http_get(probe_port, "/")[0] == 200:
                        break
                except OSError:
                    pass
                if time.time() > deadline:
                    raise AssertionError("server never started listening")
                time.sleep(0.05)
        return proc, summary

    @staticmethod
    def _read_summary(proc, timeout=15):
        out = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                out += proc.stdout.read()
                break
            chunk = proc.stdout.readline()
            if not chunk:
                break
            out += chunk
            if b"config sources:" in chunk:
                break
        for line in out.decode("utf-8", errors="replace").splitlines():
            if line.startswith("config sources:"):
                pairs = line.split(":", 1)[1].strip().split()
                return dict(p.split("=", 1) for p in pairs)
        return None

    # -- tests ----------------------------------------------------------

    def test_defaults(self):
        port = free_port()
        _, summary = self.spawn(["--port", str(port)], probe_port=port)
        self.assertEqual(summary, {"host": "default", "port": "cli",
                                   "token": "generated",
                                   "no_auth": "default"})
        self.assertEqual(http_get(port, "/")[0], 200)

    def test_cfg_port_and_token(self):
        port = free_port()
        self.write_cfg("port = %d\ntoken = cfg-token\n" % port)
        _, summary = self.spawn(probe_port=port)
        self.assertEqual(summary["port"], "file")
        self.assertEqual(summary["token"], "file")
        self.assertEqual(http_get(port, "/")[0], 200)
        self.assertEqual(http_get(port, "/api/dir?path=/",
                                  token="cfg-token")[0], 200)
        self.assertEqual(http_get(port, "/api/dir?path=/")[0], 401)

    def test_env_beats_cfg(self):
        port = free_port()
        self.write_cfg("token = cfg-token\n")
        _, summary = self.spawn(["--port", str(port)],
                                env={"GNUMARUS_TOKEN": "env-token"},
                                probe_port=port)
        self.assertEqual(summary["token"], "env")
        self.assertEqual(http_get(port, "/api/dir?path=/",
                                  token="env-token")[0], 200)
        self.assertEqual(http_get(port, "/api/dir?path=/",
                                  token="cfg-token")[0], 401)

    def test_stdin_beats_cfg(self):
        port = free_port()
        self.write_cfg("token = cfg-token\n")
        _, summary = self.spawn(["--port", str(port)],
                                stdin_data="token = stdin-token\n",
                                probe_port=port)
        self.assertEqual(summary["token"], "stdin")
        self.assertEqual(http_get(port, "/api/dir?path=/",
                                  token="stdin-token")[0], 200)
        self.assertEqual(http_get(port, "/api/dir?path=/",
                                  token="cfg-token")[0], 401)

    def test_cli_beats_all(self):
        port = free_port()
        self.write_cfg("token = cfg-token\n")
        _, summary = self.spawn(
            ["--port", str(port), "--token", "cli-token"],
            env={"GNUMARUS_TOKEN": "env-token"},
            stdin_data="token = stdin-token\n", probe_port=port)
        self.assertEqual(summary["token"], "cli")
        self.assertEqual(http_get(port, "/api/dir?path=/",
                                  token="cli-token")[0], 200)
        for bad in ("env-token", "stdin-token", "cfg-token"):
            self.assertEqual(http_get(port, "/api/dir?path=/",
                                      token=bad)[0], 401)

    def test_port_env_beats_stdin_and_cfg(self):
        taken = free_port()  # stays free: proves it was NOT chosen
        wanted = free_port()
        self.write_cfg("port = %d\n" % taken)
        _, summary = self.spawn(env={"GNUMARUS_PORT": str(wanted)},
                                stdin_data="port = %d\n" % taken,
                                probe_port=wanted)
        self.assertEqual(summary["port"], "env")
        self.assertEqual(http_get(wanted, "/")[0], 200)
        with self.assertRaises(OSError):
            http_get(taken, "/")

    def test_no_auth_via_cfg(self):
        port = free_port()
        self.write_cfg("no_auth = true\n")
        _, summary = self.spawn(["--port", str(port)], probe_port=port)
        self.assertEqual(summary["no_auth"], "file")
        self.assertEqual(http_get(port, "/api/dir?path=/")[0], 200)

    def test_cli_auth_reenables(self):
        port = free_port()
        _, summary = self.spawn(["--port", str(port), "--auth"],
                                env={"GNUMARUS_NO_AUTH": "true"},
                                probe_port=port)
        self.assertEqual(summary["no_auth"], "cli")
        self.assertEqual(http_get(port, "/api/dir?path=/")[0], 401)

    def test_binary_cfg_ignored(self):
        port = free_port()
        self.write_cfg(b"\x00\x01binary\xff\xfetoken = hidden\n")
        _, summary = self.spawn(["--port", str(port)], probe_port=port)
        self.assertEqual(summary["token"], "generated")
        self.assertEqual(http_get(port, "/")[0], 200)

    def test_binary_stdin_ignored(self):
        port = free_port()
        _, summary = self.spawn(["--port", str(port)],
                                stdin_data=b"\x00binary\xff",
                                probe_port=port)
        self.assertEqual(summary["token"], "generated")

    def test_oversize_stdin_head_used_tail_ignored(self):
        port = free_port()
        _, summary = self.spawn(
            ["--port", str(port)],
            stdin_data="token = within-limit\n" + "#" * (2 * 1024 * 1024),
            probe_port=port)
        self.assertEqual(summary["token"], "stdin")
        self.assertEqual(http_get(port, "/api/dir?path=/",
                                  token="within-limit")[0], 200)

    def test_oversize_stdin_key_beyond_1mb_ignored(self):
        port = free_port()
        _, summary = self.spawn(
            ["--port", str(port)],
            stdin_data="#" * (1024 * 1024 + 64) + "\ntoken = beyond\n",
            probe_port=port)
        self.assertEqual(summary["token"], "generated")

    def test_invalid_port_env_exits_2(self):
        cmd = server_cmd()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GNUMARUS_")}
        env["GNUMARUS_PORT"] = "abc"
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env)
        try:
            out, _ = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"config error", out)

    def test_empty_token_env_means_absent(self):
        port = free_port()
        _, summary = self.spawn(["--port", str(port)],
                                env={"GNUMARUS_TOKEN": ""},
                                probe_port=port)
        self.assertEqual(summary["token"], "generated")
        self.assertEqual(http_get(port, "/api/dir?path=/")[0], 401)


if __name__ == "__main__":
    unittest.main()
