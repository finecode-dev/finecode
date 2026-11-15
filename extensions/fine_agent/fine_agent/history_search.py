"""Find native session files by a marker string they contain.

Backends store history in their own layouts; what they share is that a session
file records the tool calls the agent made, output included. A marker a
command printed inside the session therefore identifies the session file
without the agent ever knowing its own id.
"""

from __future__ import annotations

import pathlib
import typing

__all__ = ["file_contains", "files_containing"]

_CHUNK_SIZE = 1 << 20


def file_contains(path: pathlib.Path, needle: str) -> bool | None:
    """Whether *path* contains *needle*; `None` when the file cannot be read.

    Reads in chunks, so a large session file is never loaded whole.
    """
    encoded = needle.encode("utf-8")
    if not encoded:
        return True
    overlap = len(encoded) - 1
    tail = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    return False
                window = tail + chunk
                if encoded in window:
                    return True
                tail = window[-overlap:] if overlap else b""
    except OSError:
        return None


def files_containing(
    paths: typing.Iterable[pathlib.Path], needle: str
) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Split *paths* into those containing *needle* and those unreadable."""
    found: list[pathlib.Path] = []
    unreadable: list[pathlib.Path] = []
    for path in paths:
        contains = file_contains(path, needle)
        if contains is None:
            unreadable.append(path)
        elif contains:
            found.append(path)
    return found, unreadable
