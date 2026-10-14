"""The single reader of ``project.requires-python``.

Two facts are derived from that one declaration: the interpreter axis an env is
materialized with (``sync_python_interpreters``) and the toolchain support range the
tools target (``get_src_artifact_toolchain_range``). They MUST agree on what a
``<major>.<minor>`` series means when matched against a patch-level bound, or a project
declaring ``>=3.11.4`` gets an axis starting at 3.11 while its linter targets 3.12 --
a disagreement between two derivations of the same fact, which is the failure mode
having a derivation at all is meant to prevent. Both go through this module so there
is only one answer to give.
"""

from __future__ import annotations

from finecode_extension_api import code_action
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

LATEST_PATCH_SENTINEL = 999
"""Stands in for "the newest patch release of this minor" when matching a specifier.

A minor series (``cpython@3.11``) is what an axis row and a target version both name,
but ``requires-python`` is matched against full versions, and what ``create_env``
provisions for that row is the *newest* patch of the series the provisioner can obtain.
So the question a series has to answer is not "does the string 3.11 satisfy the
specifier" -- it never does for a patch-level bound -- but "does the interpreter this
series stands for satisfy it". Matching a high sentinel patch models that: ``>=3.11.4``
keeps 3.11 (the provisioner installs 3.11.15), and ``<3.11.5`` drops it (the provisioner
would install 3.11.15, which violates the bound and yields an env the project cannot
use)."""

_SCAN_MAJORS = (2, 3, 4)
_SCAN_MINOR_LIMIT = 99
"""How far a minor series is looked for when locating the ends of a support range.

The range is found by asking the specifier about concrete series rather than by taking
the specifier's bounds apart, because the operators that need taking apart are the ones
that get it wrong: ``~=3.11`` carries no explicit upper bound yet has one, ``==3.12.*``
carries neither, and ``>3.10`` names a version it excludes. Asking is the same question
the axis derivation asks, with the same answer."""


def _scan_space() -> list[Version]:
    """Every minor series the scan considers, oldest first."""
    return [
        Version(f"{major}.{minor}")
        for major in _SCAN_MAJORS
        for minor in range(_SCAN_MINOR_LIMIT + 1)
    ]


_SCAN_FLOOR = Version(f"{_SCAN_MAJORS[0]}.0")
"""The bottom of the scan: admitting it means the declaration has no lower bound."""


def parse_requires_python(requires_python: str) -> SpecifierSet:
    """Parse a ``requires-python`` declaration, failing with an actionable message.

    Raises:
        ActionFailedException: requires_python is not a valid PEP 440 specifier.
    """
    try:
        return SpecifierSet(requires_python)
    except InvalidSpecifier as error:
        raise code_action.ActionFailedException(
            "project.requires-python is not a valid PEP 440 specifier:"
            f" '{requires_python}'"
        ) from error


def newest_patch_of(minor_series: Version) -> Version:
    """The version a ``<major>.<minor>`` series stands for, for specifier matching."""
    major, minor, *_ = minor_series.release
    return Version(f"{major}.{minor}.{LATEST_PATCH_SENTINEL}")


def supports_minor(specifier: SpecifierSet, minor_series: Version) -> bool:
    """Whether *specifier* admits the interpreter *minor_series* stands for."""
    return specifier.contains(newest_patch_of(minor_series))


def support_range(requires_python: str) -> tuple[str | None, str | None]:
    """Return the ``(oldest, newest)`` minor series a ``requires-python`` declares.

    Either end is ``None`` when the declaration does not close it, and that is an
    answer rather than a missing one -- what an unclosed end means is that the project
    promised nothing there, so a consumer needing a concrete version has to get it from
    something that knows which versions exist, such as the interpreter axis.

    ``newest`` is ``None`` when the upper end is open within the major line the range
    ends in: ``>=3.11`` and ``~=3.11`` both leave every later 3.x admitted, and that is
    the correct form for a published package (see the no-upper-bound convention in
    developing-finecode.md).

    ``oldest`` is ``None`` when nothing is ruled out below -- ``<3.14`` and ``==3.*``
    state a ceiling and no floor. Reporting the bottom of the scan as the floor instead
    would be inventing a promise the project never made, and a tool configured from it
    targets a language level decades below anything the project runs on.

    Raises:
        ActionFailedException: requires_python is not a valid PEP 440 specifier, or
            admits no Python minor series.
    """
    specifier = parse_requires_python(requires_python)

    supported = [
        series for series in _scan_space() if supports_minor(specifier, series)
    ]

    if not supported:
        # every minor series is matched as its newest patch (LATEST_PATCH_SENTINEL), so
        # a specifier admitting only an exact patch (`==3.11.2`) lands here. That is the
        # same answer the axis derivation gives it -- no series can be provisioned that
        # satisfies the declaration -- rather than a second, laxer rule for one caller.
        raise code_action.ActionFailedException(
            f"project.requires-python '{requires_python}' admits no Python minor series"
            " (each series is matched as its newest patch release)"
        )

    oldest = supported[0]
    newest = supported[-1]

    # Both ends are read off the whole scan rather than off one major line, so a range
    # spanning majors (`>=2.7,<3.14`) reports the ceiling it actually has. Asking only
    # within the oldest end's major answers `2.99` for that declaration and calls the
    # upper end open, which is the opposite of what it says.
    return (
        None if oldest == _SCAN_FLOOR else _format(oldest),
        None if newest.release[1] == _SCAN_MINOR_LIMIT else _format(newest),
    )


def _format(minor_series: Version) -> str:
    major, minor, *_ = minor_series.release
    return f"{major}.{minor}"
