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

import pathlib
import sys
from typing import NewType
from urllib.parse import unquote, urlparse

ResourceUri = NewType("ResourceUri", str)
"""A URI string identifying a resource.  Local files use the ``file://`` scheme."""


def path_to_resource_uri(path: pathlib.Path) -> ResourceUri:
    """Convert an absolute *path* to a ``file://`` :class:`ResourceUri`.

    The path **must** be absolute; call ``path.resolve()`` first if needed.

    >>> path_to_resource_uri(pathlib.Path("/home/user/foo.py"))
    'file:///home/user/foo.py'
    """
    return ResourceUri(path.as_uri())


def resource_uri_to_path(uri: ResourceUri) -> pathlib.Path:
    """Convert a ``file://`` :class:`ResourceUri` back to a local :class:`~pathlib.Path`.

    Raises :class:`ValueError` if the URI scheme is not ``file``.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"Cannot convert non-file URI to Path: {uri}")
    decoded_path = unquote(parsed.path)
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
