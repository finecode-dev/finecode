"""Test support for Python registry handlers.

Complements ``finecode_extension_runner.testing`` for handlers that speak the
package-registry wire protocol instead of ``IHttpClient`` fakes. The
``LocalRegistry`` here is a throwaway stand-in for a real index: it reproduces
only the PyPI behavior the handlers rely on, persist its files in a directory
the test owns, and records every request so tests can assert on what the
handlers actually sent.
"""

from fine_python_package_info.testing.local_registry import (
    LocalRegistry,
    PublishedPackage,
    RequestRecord,
    normalize_name,
)

__all__ = [
    "LocalRegistry",
    "PublishedPackage",
    "RequestRecord",
    "normalize_name",
]
