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

Why it is ambient rather than a parameter. The rule is to pass a per-run value
explicitly unless something structural prevents it
(``docs/guides/developing-finecode.md`` § "Ambient state"). Two things do here,
and both are checkable. First, *the consumer outlives the run*: ``UserPrompt``,
the service that has to name the run, is registered once per configuration with
``registry.register_instance`` in ``di/bootstrap.py``, and handler instances are
constructed with the services they asked for and then cached in
``RunnerContext.action_cache_by_name`` (``domain.ActionHandlerCache.instance``,
``used_services`` beside it) for reuse across runs — so a per-run ``UserPrompt``
cannot reach a handler that is already holding the one it was built with, and
making the container run-scoped would mean rebuilding or re-injecting handlers
per run, the cost the cache exists to avoid. Second, *the alternative is a
public API change that moves the responsibility outward*: ``IUserPrompt`` is
extension API, and a ``run_id`` parameter on ``ask_choice`` would make every
handler author name their own run, where a stale value misroutes a question to
another person silently. Both blockers stop holding if services are resolved per
run and handlers are no longer cached across runs; at that point ``UserPrompt``
could be constructed with its run id and this module would have no reason to
exist.

Not ``RunActionMeta.wal_run_id``, which is the same identifier under the name
handlers see it by. The action-runner impls do receive ``meta`` per call, so
reading the run from it would look like explicit passing, but ``meta`` there is
whatever the *handler* passed: the field defaults to ``""``, and a handler that
builds a fresh ``RunActionMeta`` rather than forwarding its own would attribute
the nested run to nobody, silently. The executing task always knows which run it
is; a value handed to it does not.

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
