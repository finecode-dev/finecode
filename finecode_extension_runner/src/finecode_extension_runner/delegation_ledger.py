"""One delegation record per action run, and its stop verdict.

``DelegationLedger`` records, for the action run executing on this task, every
delegated call the run made and whether each one ended stopped. The run loop
(``_services/run_action.run_action``) binds one record per action run; a
same-env child run binds its own record, counted in its caller's record until
it closes. The public ``RunActionContext.delegated_calls_outcome()`` read
answers from the handler's own run record.

Why it is ambient rather than a parameter. The same two structural facts that
``run_context`` names hold here. First, *the consumer outlives the run*:
``ProjectActionRunnerImpl`` and ``WorkspaceActionRunnerImpl`` are registered
once per configuration (``di/bootstrap.py``) and reused across runs, so a
per-run record cannot reach the runner through the instance it holds. Second,
*a call belongs to the run current in the task making it*:
the read must cover calls from any task of the run, including same-env child
runs at any depth, so threading a record object through every ``run_action``
call site would still miss tasks the run spawns. Tasks inherit the context
they are created in, so every task of the run shares this task's record
object. The record is read at the edge (the outcome read, the reply verdict)
and never stored on the shared impl instances.
"""

from __future__ import annotations

import collections.abc
import contextlib
import contextvars
import typing

from finecode_extension_api import code_action

__all__ = [
    "DelegatedCall",
    "DelegatedCallsOutcome",
    "DelegationLedger",
    "SubtreeStopData",
    "bind_run",
    "current",
    "subtree_stopped_of",
]

DelegatedCallsOutcome = code_action.DelegatedCallsOutcome


class SubtreeStopData(typing.TypedDict):
    subtreeStopped: bool


def subtree_stopped_of(value: object) -> bool:
    """Report whether *value* carries ``subtreeStopped: True``.

    Anything else — a missing key, a non-mapping, or an explicit ``False`` —
    reads as unconfirmed, so an older peer that sends no such field (or no
    reply at all) can never be mistaken for stopped.
    """
    return isinstance(value, dict) and value.get("subtreeStopped") is True


class DelegatedCall:
    """One delegated call's verdict slot. The first ``end`` wins."""

    def __init__(self) -> None:
        self._ended = False
        self._stopped = False

    def end(self, *, stopped: bool) -> None:
        """Record how the call ended. Later calls are ignored."""
        if self._ended:
            return
        self._ended = True
        self._stopped = stopped


class DelegationLedger:
    """Every delegated call opened by one action run, and their verdicts."""

    def __init__(self) -> None:
        self._calls: list[DelegatedCall] = []
        self._closed = False
        self._frozen: code_action.DelegatedCallsOutcome | None = None

    def open_call(self) -> DelegatedCall:
        """Open a verdict slot for a delegated call about to be sent."""
        call = DelegatedCall()
        if not self._closed:
            self._calls.append(call)
        return call

    def outcome(self) -> code_action.DelegatedCallsOutcome:
        """``STOPPED`` iff every opened call ended with ``stopped=True``.

        A run with no delegated call reads ``STOPPED``; a call still pending,
        or one that ended unconfirmed, reads ``UNCONFIRMED``. Once closed, the
        value it had at close is kept: a call still pending at close reads
        ``UNCONFIRMED`` for good, and a later call is counted in no ledger.
        """
        if self._frozen is not None:
            return self._frozen
        if all(call._ended and call._stopped for call in self._calls):
            return code_action.DelegatedCallsOutcome.STOPPED
        return code_action.DelegatedCallsOutcome.UNCONFIRMED

    def _close(self) -> None:
        """Freeze the outcome; later calls are counted nowhere."""
        if not self._closed:
            self._frozen = self.outcome()
            self._closed = True


_current_ledger: contextvars.ContextVar[DelegationLedger | None] = (
    contextvars.ContextVar("finecode_delegation_ledger", default=None)
)


def current() -> DelegationLedger | None:
    """The record of the action run this task executes under, or ``None`` outside one."""
    return _current_ledger.get()


@contextlib.contextmanager
def bind_run() -> collections.abc.Generator[DelegationLedger, None, None]:
    """Bind one delegation record for the block, counted in the outer run.

    On entry a fresh record is made current in this task (and so in every
    task created while the block lasts). When a record was current on entry
    and is still open, the new record is counted in it as one entry, pending
    until this block exits. On exit the new record closes, the outer entry
    ends stopped iff the new record's outcome at its close is ``STOPPED``,
    and the outer binding is restored. Exit runs on every path, exception
    and cancellation included.
    """
    outer = _current_ledger.get()
    outer_call: DelegatedCall | None = None
    if outer is not None and not outer._closed:
        outer_call = outer.open_call()
    record = DelegationLedger()
    token = _current_ledger.set(record)
    try:
        yield record
    finally:
        record._close()
        _current_ledger.reset(token)
        if outer_call is not None:
            outer_call.end(
                stopped=record.outcome() is code_action.DelegatedCallsOutcome.STOPPED
            )
