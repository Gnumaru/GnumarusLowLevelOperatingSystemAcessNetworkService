"""Multi-source configuration: CLI flag > env var > stdin > config.cfg > default."""

import os
import secrets
import sys
from hosts import bind_allowed


CONFIG_KEYS = ("host", "port", "token", "no_auth")
CONFIG_FILE_NAME = "config.cfg"
CONFIG_MAX_BYTES = 1024 * 1024
CONFIG_ENV_PREFIX = "GNUMARUS_"


class ConfigError(Exception):
    pass


def _parse_kv_bytes(data):
    """Parse key=value lines into a dict.

    Returns {} when the data looks binary (NUL byte anywhere) or is not
    valid UTF-8. Blank lines, #/; comments and [section] headers are
    skipped; unknown keys are ignored; last duplicate wins.
    """
    if b"\x00" in data:
        return {}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if (not line or line.startswith("#") or line.startswith(";")
                or (line.startswith("[") and line.endswith("]"))):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if (len(value) >= 2 and value[0] == value[-1]
                and value[0] in ("'", '"')):
            value = value[1:-1]
        if key in CONFIG_KEYS:
            cfg[key] = value
    return cfg


def load_file_config():
    """Read config.cfg next to this script (at most 1 MB, rest ignored)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        CONFIG_FILE_NAME)
    try:
        with open(path, "rb") as fh:
            data = fh.read(CONFIG_MAX_BYTES)
    except OSError:
        return {}
    return _parse_kv_bytes(data)


def load_stdin_config():
    """Read key=value lines from stdin (at most 1 MB, rest ignored).

    Skipped entirely when stdin is a terminal (no piped input) or
    unreadable, so interactive startups never block.
    """
    try:
        stdin = sys.stdin
        if stdin is None or stdin.isatty():
            return {}
        data = stdin.buffer.read(CONFIG_MAX_BYTES)
    except (OSError, ValueError, AttributeError):
        return {}
    if not data:
        return {}
    return _parse_kv_bytes(data)


def _parse_bool(value, source):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        "invalid boolean for no_auth from %s: %r (use true/false)"
        % (source, value))


def _parse_port(value, source):
    if isinstance(value, bool):
        raise ConfigError("invalid port from %s: %r (1-65535)"
                          % (source, value))
    try:
        port = value if isinstance(value, int) else int(str(value).strip(), 10)
    except (ValueError, TypeError):
        raise ConfigError("invalid port from %s: %r (1-65535)"
                          % (source, value))
    if not 1 <= port <= 65535:
        raise ConfigError("port from %s out of range: %d (1-65535)"
                          % (source, port))
    return port


def resolve_config(args, stdin_cfg, file_cfg):
    """Merge CLI > env > stdin > file > defaults. Raises ConfigError."""
    env_cfg = {}
    for key in CONFIG_KEYS:
        value = os.environ.get(CONFIG_ENV_PREFIX + key.upper())
        if value is not None and value != "":
            env_cfg[key] = value
    cli_cfg = {}
    if args.host is not None:
        cli_cfg["host"] = args.host
    if args.port is not None:
        cli_cfg["port"] = args.port
    if args.token is not None:
        cli_cfg["token"] = args.token
    if args.no_auth is not None:
        cli_cfg["no_auth"] = args.no_auth

    raw = {}
    sources = {}
    for key in CONFIG_KEYS:
        for source, layer in (("cli", cli_cfg), ("env", env_cfg),
                              ("stdin", stdin_cfg), ("file", file_cfg)):
            if key in layer:
                raw[key] = layer[key]
                sources[key] = source
                break
        else:
            sources[key] = "default"

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("host from %s must not be empty"
                          % sources["host"])
    if not bind_allowed(host):
        raise ConfigError("host from %s must be a loopback address "
                          "(127.0.0.1, ::1 or localhost), got '%s'"
                          % (sources["host"], host))
    port = (_parse_port(raw["port"], sources["port"]) if "port" in raw
            else 1423)
    no_auth = (_parse_bool(raw["no_auth"], sources["no_auth"])
               if "no_auth" in raw else False)
    token = None
    if no_auth:
        sources["token"] = "disabled"
    elif "token" in raw:
        token = (raw["token"] if isinstance(raw["token"], str)
                 else str(raw["token"]))
        if not token:
            raise ConfigError("token from %s must not be empty"
                              % sources["token"])
    else:
        token = secrets.token_urlsafe(32)
        sources["token"] = "generated"
    return {"host": host, "port": port, "token": token,
            "no_auth": no_auth, "sources": sources}
