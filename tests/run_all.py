#!/usr/bin/env python3
"""Run the full regression suite (stdlib only).

Starts the server on a free localhost port, waits until it answers,
runs unittest discovery over tests/test_*.py, then stops the server.

Usage:
    python3 tests/run_all.py [--server-cmd CMD] [--server-cmd-args ...]

Default server is go/gnumarus (the reference port). To run the suite
against another implementation with the same CLI/HTTP contract
(e.g. the Kotlin port):

    python3 tests/run_all.py \\
        --server-cmd kotlin/build/install/gnumarus/bin/gnumarus \\
        --server-cfg kotlin/build/install/gnumarus/config.cfg
"""

import argparse
import os
import secrets
import shlex
import socket
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.helpers import base_url  # noqa: E402


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(proc, timeout=15):
    import http.client
    from urllib.parse import urlparse
    parsed = urlparse(os.environ["GNATIVE_TEST_BASE"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited early with code %s" % proc.returncode)
        try:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=2)
            conn.request("GET", "/")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server did not become ready in time")


def probe_sqlite(base, token):
    """Return True when the server supports SQLite (else tests skip)."""
    import http.client
    import json
    import urllib.parse
    from urllib.parse import urlparse
    parsed = urlparse(base)
    probe = os.path.join(ROOT, "tmp", "sqlite_probe.db")
    try:
        os.remove(probe)
    except OSError:
        pass
    try:
        conn = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=10)
        conn.request(
            "POST", "/api/sqlite/create?path=" + urllib.parse.quote(probe),
            headers={"Authorization": "Bearer " + token})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status != 503
    except OSError:
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the regression suite.")
    ap.add_argument("--server-cmd", default=None,
                    help="Server launch command, e.g. "
                         "'kotlin/build/install/gnumarus/bin/gnumarus'. "
                         "Defaults to go/gnumarus (the reference port).")
    ap.add_argument("--server-cfg", default=None,
                    help="config.cfg path used by the server under test. "
                         "Required with --server-cmd.")
    opts = ap.parse_args(argv)
    if opts.server_cmd and not opts.server_cfg:
        ap.error("--server-cfg is required with --server-cmd")
    port = find_free_port()
    os.environ["GNATIVE_TEST_BASE"] = "http://127.0.0.1:%d" % port
    token = "test-token-%s" % secrets.token_urlsafe(16)
    os.environ["GNATIVE_TEST_TOKEN"] = token
    if opts.server_cmd:
        launch = shlex.split(opts.server_cmd)
        os.environ["GNATIVE_TEST_SERVER_CMD"] = opts.server_cmd
        os.environ["GNATIVE_TEST_CFG"] = (
            opts.server_cfg or os.path.join(
                "kotlin", "build", "install", "gnumarus", "config.cfg"))
    else:
        gobin = os.path.join(ROOT, "go", "gnumarus")
        if not (os.path.isfile(gobin) and os.access(gobin, os.X_OK)):
            ap.error("default server %s is missing: run ./go/build.sh "
                     "first (or pass --server-cmd/--server-cfg for "
                     "another port)" % gobin)
        launch = [gobin]
        os.environ["GNATIVE_TEST_SERVER_CMD"] = gobin
        os.environ["GNATIVE_TEST_CFG"] = os.path.join("go", "config.cfg")
    log_path = os.path.join(ROOT, "tmp", "test_server.log")
    os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            launch + ["--host", "127.0.0.1", "--port", str(port),
                      "--token", token],
            cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_ready(proc, timeout=30)
            print("server ready at %s" % base_url())
            sqlite = probe_sqlite(os.environ["GNATIVE_TEST_BASE"], token)
            os.environ["GNATIVE_TEST_SQLITE"] = "1" if sqlite else "0"
            print("sqlite support: %s" % ("yes" if sqlite else "no (skipping)"))
            loader = unittest.TestLoader()
            suite = loader.discover(
                start_dir=HERE, pattern="test_*.py", top_level_dir=ROOT)
            runner = unittest.TextTestRunner(verbosity=2)
            result = runner.run(suite)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
