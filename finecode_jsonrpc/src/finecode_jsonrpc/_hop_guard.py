"""Transport-side guards for :class:`IHopObserver` calls.

Every observer call from transport/session code must go through these helpers.
An ordinary ``Exception`` from the observer is contained and reported via
``report_contained_failure()`` under its own guard; ``BaseException``
subclasses that are not ``Exception`` (cancellation included) propagate.
A truthy ``__exit__`` from a handler scope never suppresses the body
exception. A failed ``__enter__`` counts as not entered: no ``__exit__`` runs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator
from typing import TypeVar

from .tracing import HopToken, IHopObserver

__all__ = [
    "call_guarded",
    "guarded_handler_scope",
    "report_failure_guarded",
]

_T = TypeVar("_T")


def report_failure_guarded(observer: IHopObserver | None) -> None:
    """Call ``report_contained_failure`` containing ``Exception``.

    ``BaseException`` (cancellation included) propagates; an ``Exception``
    from the report itself is swallowed.
    """
    if observer is None:
        return
    try:
        observer.report_contained_failure()
    except Exception:  # noqa: BLE001 -- P3.1 guard: contain ordinary Exception
        return


def call_guarded(observer: IHopObserver | None, func: Callable[[], _T]) -> _T | None:
    """Call ``func`` containing an ordinary ``Exception``.

    Returns ``func()`` on success, ``None`` when the observer is absent or
    when ``func`` raised ``Exception`` (after reporting). ``BaseException``
    propagates unchanged.
    """
    if observer is None:
        return None
    try:
        return func()
    except Exception:  # noqa: BLE001 -- P3.1 guard: contain ordinary Exception
        report_failure_guarded(observer)
        return None


@contextlib.contextmanager
def guarded_handler_scope(
    observer: IHopObserver | None, token: HopToken
) -> Generator[None, None, None]:
    """Enter the observer's handler scope with all calls guarded.

    Yields normally (no-op) when there is no observer, when ``handler_scope``
    or ``__enter__`` raises ``Exception`` (after reporting), and always
    ignores a truthy ``__exit__`` so body exceptions are never suppressed.
    An ``Exception`` from ``__exit__`` is contained and reported.
    """
    if observer is None:
        yield
        return
    try:
        scope = observer.handler_scope(token)
    except Exception:  # noqa: BLE001 -- P3.1 guard: contain ordinary Exception
        report_failure_guarded(observer)
        yield
        return
    try:
        scope.__enter__()
    except Exception:  # noqa: BLE001 -- P3.1 guard: contain ordinary Exception
        report_failure_guarded(observer)
        yield
        return
    try:
        yield
    except BaseException as exc:
        try:
            suppress = scope.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:  # noqa: BLE001 -- P3.1 guard: contain ordinary Exception
            report_failure_guarded(observer)
        else:
            _ = suppress
        raise
    else:
        try:
            suppress = scope.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 -- P3.1 guard: contain ordinary Exception
            report_failure_guarded(observer)
        else:
            _ = suppress
