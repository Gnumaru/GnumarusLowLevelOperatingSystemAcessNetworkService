"""Shared test helpers (stdlib only).

Every test module imports from here::

    from tests.helpers import request, json_request, workspace, quote

The target server is read from GNATIVE_TEST_BASE
(default http://127.0.0.1:1423). The API token is read from
GNATIVE_TEST_TOKEN and attached automatically (pass auth=False to
opt out). tests/run_all.py starts a server automatically (go/gnumarus
by default); the modules also work against a manually started server::

    ./go/gnumarus --token secret --port 1423
    GNATIVE_TEST_TOKEN=secret python3 -m unittest discover -s tests
"""

import http.client
import json
import math
import os
import re
import shutil
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.join(ROOT, "tmp", "test_workspace")

_INT_RE = re.compile(r"-?(0|[1-9][0-9]*)$")


def tv(tagged):
    """Parse a tagged wire string back to a python value.

    "null" -> None, "i:5" -> 5, "f:1.5" -> 1.5, "b:true" -> True,
    "s:ada" -> "ada". Anything else (incl. raw numbers/bools) fails,
    which is exactly what the wire contract forbids.
    """
    assert isinstance(tagged, str), "not a string: %r" % (tagged,)
    if tagged == "null":
        return None
    if tagged.startswith("i:"):
        body = tagged[2:]
        assert _INT_RE.match(body), "bad int tag: %r" % tagged
        return int(body)
    if tagged.startswith("f:"):
        value = float(tagged[2:])
        assert math.isfinite(value), "bad float tag: %r" % tagged
        return value
    if tagged in ("b:true", "b:false"):
        return tagged == "b:true"
    if tagged.startswith("s:"):
        return tagged[2:]
    raise AssertionError("untagged scalar on the wire: %r" % tagged)


def ti(n):
    """Build an int tag for request bodies."""
    assert isinstance(n, int) and not isinstance(n, bool)
    return "i:%d" % n


def tf(n):
    """Build a float tag for request bodies."""
    assert isinstance(n, float) and math.isfinite(n)
    return "f:%r" % n


def assert_json_scalars(obj):
    """Wire invariant: parsed JSON holds only str/list/dict (recursively).

    Any int/float/bool/None instance anywhere is a contract violation.
    """
    if isinstance(obj, dict):
        for value in obj.values():
            assert_json_scalars(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            assert_json_scalars(value)
    elif isinstance(obj, str):
        pass
    else:
        raise AssertionError("bare scalar on the wire: %r" % (obj,))


def base_url():
    return os.environ.get("GNATIVE_TEST_BASE", "http://127.0.0.1:1423")


def _split_base():
    parsed = urllib.parse.urlparse(base_url())
    return parsed.hostname or "127.0.0.1", parsed.port or 80


def request(method, target, body=None, headers=None, auth=True):
    """Do an HTTP request. Return (status, headers, body_bytes).

    headers: dict with lower-cased keys. When auth is True (default) and
    GNATIVE_TEST_TOKEN is set, an Authorization: Bearer header is added
    unless one is already present.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    hdrs = dict(headers or {})
    if auth and os.environ.get("GNATIVE_TEST_TOKEN"):
        if "authorization" not in {k.lower() for k in hdrs}:
            hdrs["Authorization"] = "Bearer " + os.environ["GNATIVE_TEST_TOKEN"]
    if body is not None:
        hdrs.setdefault("Content-Length", str(len(body)))
    host, port = _split_base()
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request(method, target, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    out_headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, out_headers, data


def json_request(method, target, obj=None, auth=True):
    """Do a request and parse the response as JSON.

    obj=None sends no body. Returns (status, headers, parsed_json_or_bytes)."""
    if obj is None:
        status, headers, data = request(method, target, auth=auth)
    else:
        body = json.dumps(obj).encode("utf-8")
        status, headers, data = request(
            method, target, body=body,
            headers={"Content-Type": "application/json"}, auth=auth)
    try:
        parsed = json.loads(data.decode("utf-8"))
    except ValueError:
        return status, headers, data
    assert_json_scalars(parsed)
    return status, headers, parsed


def get_json(target):
    status, headers, data = request("GET", target)
    parsed = json.loads(data.decode("utf-8"))
    assert_json_scalars(parsed)
    return status, headers, parsed


def workspace(name):
    """Return a clean, existing workspace dir for a test module."""
    path = os.path.join(WORKSPACE, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def quote(path):
    return urllib.parse.quote(path, safe="")
