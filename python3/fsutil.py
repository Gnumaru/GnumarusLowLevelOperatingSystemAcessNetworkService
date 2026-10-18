"""Filesystem path helpers."""

import mimetypes
import os
import stat


def resolve_path(raw_path):
    """Resolve user-supplied path to an absolute normalized path."""
    expanded = os.path.expanduser(raw_path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    return os.path.normpath(expanded)


def entry_type(full_path, st):
    if os.path.islink(full_path):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


def guess_mime(path):
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"
