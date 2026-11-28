"""
ResourceUri — a semantic type for resource locations in action payloads and results.

See ADR-0005 for the full rationale.  In short: action boundary DTOs must not
use ``pathlib.Path``; they carry ``ResourceUri`` values instead.  Local files
use ``file://`` URIs (RFC 8089).  Future non-local resources may use other
schemes.

Typical usage in a handler::

    from finecode_extension_api.resource_uri import (
        ResourceUri,
        path_to_resource_uri,
        resource_uri_to_path,
    )

    # Path → ResourceUri  (when populating a payload or result field)
    uri = path_to_resource_uri(some_absolute_path)

    # ResourceUri → Path  (when you need a local filesystem path)
    local_path = resource_uri_to_path(uri)
"""

from __future__ import annotations

import os.path
import pathlib
import sys
from typing import NewType
from urllib.parse import ParseResult, unquote, urlparse

ResourceUri = NewType("ResourceUri", str)
"""A URI string identifying a resource.  Local files use the ``file://`` scheme."""


def path_to_resource_uri(path: pathlib.Path) -> ResourceUri:
    """Convert an absolute *path* to a ``file://`` :class:`ResourceUri`.

    The path **must** be absolute; call ``path.resolve()`` first if needed.

    >>> path_to_resource_uri(pathlib.Path("/home/user/foo.py"))
    'file:///home/user/foo.py'
    """
    return ResourceUri(path.as_uri())


def _parse_file_uri_path(parsed: ParseResult) -> pathlib.Path:
    """The path a parsed ``file://`` URI names, still relative if the URI was.

    When ``urlparse`` sees two slashes it treats the first path segment as the
    netloc (hostname), so ``file://relative/path`` arrives split in two; this
    reconstructs it as ``netloc + path``.
    """
    decoded_path = unquote(parsed.path)
    if parsed.netloc:
        return pathlib.Path(parsed.netloc + decoded_path)
    # On Windows, file:///C:/foo is parsed as path="/C:/foo" — strip the
    # leading slash so pathlib recognises the drive letter.
    if (
        sys.platform == "win32"
        and len(decoded_path) >= 3
        and decoded_path[0] == "/"
        and decoded_path[2] == ":"
    ):
        decoded_path = decoded_path[1:]
    return pathlib.Path(decoded_path)


def resource_uri_to_path(uri: ResourceUri) -> pathlib.Path:
    """Convert a ``file://`` :class:`ResourceUri` back to a local :class:`~pathlib.Path`.

    Supports relative ``file://`` URIs: ``file://relative/path`` is resolved
    against the current working directory.

    NOTE: resolving against the CWD is only correct in a process whose CWD is
    the user's terminal directory.  An ER runs in its own project directory, so
    the *same* relative URI resolves to a different path in every ER it reaches.
    Relative URIs must therefore be expanded with :func:`absolutize_resource_uri`
    at the boundary that still knows the user's directory — the CLI does this to
    every payload it sends — and never travel over the wire.

    Raises :class:`ValueError` if the URI scheme is not ``file``.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"Cannot convert non-file URI to Path: {uri}")
    path = _parse_file_uri_path(parsed)
    if not path.is_absolute():
        return pathlib.Path.cwd() / path
    return path


def absolutize_resource_uri(uri: ResourceUri, base_dir: pathlib.Path) -> ResourceUri:
    """Expand a relative ``file://`` *uri* against *base_dir*.

    Returns *uri* unchanged when it is already absolute, or when it is not a
    ``file://`` URI at all (other schemes carry no local path to expand).

    ``.`` and ``..`` segments are collapsed lexically rather than through
    :meth:`~pathlib.Path.resolve`, so *base_dir* reaches the other side spelled
    exactly as the caller spelled it — resolving symlinks here would hand the WM
    a path that no longer matches the project paths it keys its state by.

    >>> absolutize_resource_uri(ResourceUri("file://./pkg"), pathlib.Path("/ws"))
    'file:///ws/pkg'
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    path = _parse_file_uri_path(parsed)
    if path.is_absolute():
        return uri
    return path_to_resource_uri(pathlib.Path(os.path.normpath(base_dir / path)))


def _has_uri_scheme(location: str) -> bool:
    """Whether *location* is spelled as a URI rather than a plain path.

    A Windows drive letter parses as a one-character scheme (``C:/ws`` gives
    scheme ``c``), so a single character never counts as one — no registered
    URI scheme is that short.
    """
    return len(urlparse(location).scheme) > 1


def resource_location_to_uri(location: str, base_dir: pathlib.Path) -> ResourceUri:
    """Read *location* as a resource and return it addressed absolutely.

    Accepts the three spellings a caller may reasonably use for the same local
    file — an absolute ``file://`` URI, a relative one, or a plain filesystem
    path — and returns an absolute :class:`ResourceUri` for all of them.  A URI
    in some other scheme names no local path and is returned untouched.

    Only call this where the field is *known* to hold a resource, from a payload
    schema or an equivalent declaration.  Applied to an arbitrary string it would
    read prose as a filename.

    >>> resource_location_to_uri("./pkg", pathlib.Path("/ws"))
    'file:///ws/pkg'
    >>> resource_location_to_uri("file://./pkg", pathlib.Path("/ws"))
    'file:///ws/pkg'
    """
    if _has_uri_scheme(location):
        return absolutize_resource_uri(ResourceUri(location), base_dir)
    path = pathlib.Path(location)
    if not path.is_absolute():
        path = pathlib.Path(os.path.normpath(base_dir / path))
    return path_to_resource_uri(path)


def is_relative_file_uri(location: str) -> bool:
    """Whether *location* is a ``file://`` URI that names no absolute path.

    Such a URI resolves against whatever directory the process reading it
    happens to be in, so it means different files in different processes.  This
    identifies the ones that must not be sent anywhere — see
    :func:`resource_uri_to_path` for why.
    """
    parsed = urlparse(location)
    if parsed.scheme != "file":
        return False
    return not _parse_file_uri_path(parsed).is_absolute()
