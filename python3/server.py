#!/usr/bin/env python3
"""Gnumarus Python Native Operations HTTP Service.

Thin entry point: CLI parsing and server boot.
Configuration lives in config.py, routes in routes.py, request handling
in handler.py (endpoint implementations in the handler_* mixins).
"""

import argparse
import sys
from http.server import ThreadingHTTPServer
from config import (ConfigError, load_file_config,
                    load_stdin_config, resolve_config)
from handler import Handler
from version import VERSION


def build_parser():
    parser = argparse.ArgumentParser(
        description="Gnumarus native-operations HTTP service (v%s, "
                    "stdlib only)." % VERSION,
        epilog="Every setting can also come from a GNUMARUS_* environment "
               "variable (GNUMARUS_HOST/PORT/TOKEN/NO_AUTH), from stdin "
               "key=value lines, or from config.cfg next to server.py. "
               "Precedence (highest first): CLI flag > env var > stdin > "
               "config.cfg > default.")
    parser.add_argument("--host", default=None,
                        help="Interface to bind (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None,
                        help="TCP port to listen on (default: 1423).")
    parser.add_argument("--token", default=None,
                        help="Fixed API token. When unset everywhere, a "
                             "random token is minted at startup and printed.")
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument("--no-auth", dest="no_auth",
                            action="store_true", default=None,
                            help="Disable the API token requirement "
                                 "entirely. Anyone able to reach the server "
                                 "gets full access (shell included). Use "
                                 "only in trusted environments.")
    auth_group.add_argument("--auth", dest="no_auth",
                            action="store_false", default=None,
                            help="Enforce the API token requirement even "
                                 "when env/stdin/file try to disable it.")
    return parser


def main():
    args = build_parser().parse_args()
    stdin_cfg = load_stdin_config()
    file_cfg = load_file_config()
    try:
        cfg = resolve_config(args, stdin_cfg, file_cfg)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    sources = cfg["sources"]
    print("config sources: host=%s port=%s token=%s no_auth=%s"
          % (sources["host"], sources["port"],
             sources["token"], sources["no_auth"]), flush=True)
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    server.auth_required = not cfg["no_auth"]
    server.auth_token = cfg["token"] or ""
    print("GnumarusNativeOps v%s serving on http://%s:%d (Ctrl+C to stop)"
          % (VERSION, cfg["host"], cfg["port"]), flush=True)
    if not cfg["no_auth"]:
        print("API token: %s" % cfg["token"], flush=True)
        print("Send it as 'Authorization: Bearer <token>' "
              "(or ?token=). /api/help stays open.", flush=True)
    else:
        print("WARNING: authentication DISABLED (no_auth from %s). Anyone "
              "able to reach this server has full access, including shell "
              "execution." % sources["no_auth"], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
