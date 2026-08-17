"""Which run this task is executing, for anything that has to name it back to the WM.

The ER services several runs at once on one connection, so "the current run" can
only be a property of the task doing the work, never of the process or of the
runner. A :class:`contextvars.ContextVar` is exactly that: set where the WM's
request is accepted, inherited by every task the run spawns, and invisible to
the runs executing beside it.

It exists because some back-channel calls have to say *which* run is speaking.
A question put to a person (``finecode/elicit``) is answered by the client that
started the run, and the WM can only resolve that from a run identifier — the
project the asking runner serves is not enough, since two clients can stream the
same project at once. Nested dispatch carries it for the same reason: a run the
WM starts on behalf of another run belongs to the same originating client.

Not a substitute for the run's own context object. This is only the identifier,
for the handful of places that talk to the WM about the run rather than about
its work.
"""

from __future__ import annotations

import collections.abc
import contextlib
import contextvars

__all__ = ["current_run_id", "run"]


_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "finecode_current_run_id", default=None
)


@contextlib.contextmanager
def run(run_id: str) -> collections.abc.Iterator[None]:
    """Mark this task, and everything it starts, as executing *run_id*."""
    token = _current_run_id.set(run_id)
    try:
        yield
    finally:
        _current_run_id.reset(token)


def current_run_id() -> str | None:
    """The run this task is executing, or ``None`` outside of one.

    ``None`` is an ordinary answer rather than a defect: an ER exercised
    standalone, or code running outside any dispatched run, genuinely has no run
    to name. Callers report it as-is and let the WM decide what it means — for
    an ask, that a person cannot be reached (ADR-0082 rule 3).
    """
    return _current_run_id.get()
