"""Resolving ruff's ``target-version`` from the project's declared support range."""

from __future__ import annotations

from fine_python_lang import support_range
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger

_OLDEST_EXPRESSIBLE = (3, 7)
_NEWEST_EXPRESSIBLE = (3, 15)
"""The ends of what ruff's ``target-version`` can name (ruff 0.15: py37 .. py315).

A declared range is not obliged to fall inside this: a project supporting 3.6 is
declaring something ruff has no spelling for. Ruff rejects an unknown value outright
(``invalid value 'py36'``) rather than clamping, and since the target version is passed
on every invocation, sending one would turn every lint and format run into a failure --
so a range ruff cannot express is not sent at all."""


def to_ruff_target_version(minor_series: str) -> str | None:
    """``"3.11"`` -> ``"py311"``, or None when ruff has no spelling for the series.

    None means say nothing rather than say something ruff rejects; see
    ``_OLDEST_EXPRESSIBLE``. Clamping to the nearest expressible level is deliberately
    not done for the low end: targeting py37 for a project that supports 3.6 makes ruff
    suggest syntax that breaks on the interpreter the project promised to run on, which
    is worse than leaving ruff to its own inference.
    """
    try:
        major, minor = (int(part) for part in minor_series.split("."))
    except ValueError:
        return None

    if not _OLDEST_EXPRESSIBLE <= (major, minor) <= _NEWEST_EXPRESSIBLE:
        return None

    return f"py{major}{minor}"


async def resolve_target_version(
    configured: str | None,
    resolver: support_range.PythonSupportRangeResolver,
    meta: code_action.RunActionMeta,
    logger: ilogger.ILogger,
) -> str | None:
    """The ``target-version`` to send ruff, or None to send none at all.

    The oldest supported version is the one that matters: it is the language level all
    the code has to stay valid at, so it decides which upgrade suggestions and syntax
    errors ruff reports.

    None is not a default -- it means nothing usable was configured or declared, so
    nothing is sent and ruff falls back to inferring the level from ``requires-python``
    itself, or to its own default. That is the only path on which ruff's inference is
    still in play; whenever FineCode has an answer ruff can express, ruff is told it, so
    the two can never quietly disagree.

    A *configured* value is passed through verbatim rather than validated: it is an
    explicit choice, and ruff naming the value it rejected is better feedback than this
    silently dropping it against a list of ruff's levels that ages.
    """
    if configured is not None:
        return configured

    declared = await resolver.get(meta)
    if declared.min_version is None:
        return None

    target_version = to_ruff_target_version(declared.min_version)
    if target_version is None:
        logger.warning(
            f"The declared support range starts at Python {declared.min_version}, which"
            " ruff's target-version cannot name (it spells"
            f" py{_OLDEST_EXPRESSIBLE[0]}{_OLDEST_EXPRESSIBLE[1]} ..."
            f" py{_NEWEST_EXPRESSIBLE[0]}{_NEWEST_EXPRESSIBLE[1]}). Ruff keeps its own"
            " inference of the language level."
        )
    return target_version
