from __future__ import annotations

import pathlib
import sys
import tempfile


def nonexistent_abs_path(*parts: str) -> pathlib.Path:
    """An absolute path that is never created, for fixtures that need one.

    The returned path may not exist: nothing creates it, and no caller may use
    it for I/O. It is an in-memory identity token only -- something to derive a
    resource URI from or to compare against a verbatim-passthrough argv token.
    A test that reads, writes, or otherwise touches the filesystem must use
    pytest's `tmp_path` instead.

    Usable at module scope, where fixtures are unavailable. POSIX gets the
    `/tmp`-flavored spelling (`pathlib.Path("/tmp", *parts)`); Windows gets
    the real temp dir (`tempfile.gettempdir()`), where a leading slash names
    no drive and `as_uri()` would raise.
    """
    if sys.platform == "win32":
        return pathlib.Path(tempfile.gettempdir(), *parts)
    return pathlib.Path("/tmp", *parts)
