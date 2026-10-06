"""Validation of the two endpoints a Python package registry exposes.

Reading the index and uploading are separate APIs, and on PyPI they live on
separate hosts. Pointing one role at the other host fails in ways that are hard
to read: an upload to ``pypi.org`` is rejected by the wrong service, and an
index lookup against ``upload.pypi.org`` returns 404 -- which is a *meaningful*
answer for an index ("this package has nothing published"), so a misroute there
is indistinguishable from a legitimately empty result and silently reports a
published version as missing.

Each function returns an error message describing the problem, or ``None`` if
the URL is usable. Returning rather than raising lets each handler surface the
problem in whichever way it already reports failures.
"""

import urllib.parse

# test.pypi.org is deliberately in neither set: it serves both roles, which is
# why the split has gone unnoticed -- it is the only registry exercised so far.
_UPLOAD_ONLY_HOSTS = {"upload.pypi.org"}
_INDEX_ONLY_HOSTS = {"pypi.org"}


def _common_problem(registry_name: str, field: str, url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        return (
            f"Registry {registry_name!r} has {field} {url!r}, which is not an"
            " http(s) URL."
        )
    if not parsed.hostname:
        return f"Registry {registry_name!r} has {field} {url!r}, which has no host."
    return None


def index_url_problem(registry_name: str, index_url: str) -> str | None:
    """Report why ``index_url`` cannot serve package index lookups, if it cannot."""
    problem = _common_problem(registry_name, "index_url", index_url)
    if problem is not None:
        return problem

    hostname = urllib.parse.urlsplit(index_url).hostname
    if hostname in _UPLOAD_ONLY_HOSTS:
        return (
            f"Registry {registry_name!r} has index_url {index_url!r}, but"
            f" {hostname} only accepts uploads and serves no package index."
            " Use https://pypi.org/simple/."
        )
    return None


def upload_url_problem(registry_name: str, upload_url: str) -> str | None:
    """Report why ``upload_url`` cannot accept uploads, if it cannot."""
    problem = _common_problem(registry_name, "upload_url", upload_url)
    if problem is not None:
        return problem

    hostname = urllib.parse.urlsplit(upload_url).hostname
    if hostname in _INDEX_ONLY_HOSTS:
        return (
            f"Registry {registry_name!r} has upload_url {upload_url!r}, but"
            f" {hostname} only serves the package index and does not accept"
            " uploads. Use https://upload.pypi.org/legacy/."
        )
    return None
