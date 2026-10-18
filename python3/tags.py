"""Tagged wire scalars: JSON carries only string/array/object.
See /api/help for the wire convention."""

import math
import re


_INT_RE = re.compile(r"-?(0|[1-9][0-9]*)$")


def jenc(value):
    """Encode a value scalar as a tagged string.

    None -> "null", bool -> "b:true|false", int -> "i:<n>",
    float (finite) -> "f:<repr>". Plain strings are NOT encoded here:
    fixed vocabulary and free text travel raw; only dynamically typed
    positions (sqlite row cells, params values) use the "s:" prefix.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "b:true" if value else "b:false"
    if isinstance(value, int):
        return "i:%d" % value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float")
        return "f:%r" % value
    raise TypeError("jenc takes None/bool/int/float, not %r" % type(value))


def jtag_str(text):
    """Tag a string for fully-tagged positions (row cells, params)."""
    if not isinstance(text, str):
        raise TypeError("jtag_str takes str")
    return "s:" + text


def jdec(text):
    """Parse a tagged string back to a value. Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("expected a tagged string")
    if text == "null":
        return None
    if text.startswith("i:"):
        body = text[2:]
        if not _INT_RE.match(body):
            raise ValueError("bad int tag: %r" % text)
        return int(body)
    if text.startswith("f:"):
        try:
            value = float(text[2:])
        except ValueError:
            raise ValueError("bad float tag: %r" % text)
        if not math.isfinite(value):
            raise ValueError("bad float tag: %r" % text)
        return value
    if text == "b:true":
        return True
    if text == "b:false":
        return False
    if text.startswith("s:"):
        return text[2:]
    raise ValueError("untagged scalar: %r" % text)
