"""Slot through which whoever owns client connections *answers* the runner layer.

An ER whose handler needs a decision only a person can take sends
``finecode/elicit`` to the runner's JSON-RPC client. Answering it means putting
the question to the client that started the run and waiting for what comes back,
which is owned by ``wm_server.py`` — it holds the connections and the deadline —
and that sits *above* the runner in the WM's layer stack. See ADR-0072 for why
this is a slot the owner fills on import rather than an upward import.

Unlike ``wm_bridge``, an unfilled slot here is **not** droppable. Its one
operation has a caller waiting on a result, so there is no useful null
implementation: ``handlers()`` returns ``None`` and the call site answers the ER
with an error, exactly as ``run_dispatch_bridge`` and ``knowledge_bridge`` do.
The ER turns that error into the "nobody could be asked" outcome, so the handler
still gets an ordinary return value rather than an exception (ADR-0082 rule 3).

This module also owns the **addressing registry**: which client connection
started a given run. The registry lives here, at the bottom of the stack,
because both ends of the lookup are elsewhere — it is written on the way down by
the request handlers that hold their caller's connection, and read by the runner
layer when an ER asks. Neither of those two may import the other.

Addressing is by **run**, never by project. Two clients can be running the same
project at the same moment, and a run that fans out is answered by an ER serving
a project nobody registered anything for; keying by project could only guess
between them. The run identifier is the one the WM mints per dispatch and hands
to the ER, so the ER can name it back when it asks (ADR-0082 rule 1, whose
implementation notes park exactly this until such an identifier exists — it does
now, independently of the WAL, since ADR-0079 keys in-flight runs by it).

:class:`RunDispatchOrigin` carries the originating connection down to
:func:`bind_run`, the one choke point every dispatch passes through
(``in_flight_runs.track``): the request handler that still holds its caller's
connection constructs it, threads it explicitly through the dispatch, and
:func:`bind_run` records the run id the dispatch minted under it. Passed
explicitly rather than read from ambient state, per "Ambient state: when a
``ContextVar`` is allowed" in developing-finecode.md — a value many
intermediate signatures carry is a cost, not a structural blocker.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import dataclasses
import typing

from loguru import logger

from finecode.wm_server.runner.delegated_child_stops import DelegatedChildStops

__all__ = [
    "QUESTION_TEXT_MAX_CHARS",
    "ElicitAnswer",
    "ElicitationBridge",
    "QuestionKind",
    "RunDispatchOrigin",
    "ShapeAvailability",
    "WithdrawalReason",
    "bind_run",
    "handlers",
    "install",
    "mark_cancelled_by_wm",
    "originating_client_for_run",
    "reset",
    "reset_origins",
    "run_cancelled_by_wm",
]

WithdrawalReason = typing.Literal["expired", "runCancelled", "noLongerNeeded"]

QuestionKind = typing.Literal["choice", "text", "multiChoice"]

QUESTION_TEXT_MAX_CHARS: typing.Final = 65_536
"""Longest accepted text for a prefill or a text answer, in code points."""


class ElicitAnswer(typing.TypedDict):
    """What the connection owner returns for one elicitation."""

    outcome: typing.Literal["answered", "declined", "unavailable"]
    value: typing.NotRequired[str]
    values: typing.NotRequired[list[str]]


@dataclasses.dataclass(frozen=True, slots=True)
class ShapeAvailability:
    """Which question shapes the run's originating client declared."""

    choice: bool
    text: bool
    multi_choice: bool


@dataclasses.dataclass(frozen=True, slots=True)
class RunDispatchOrigin:
    """Which client connection, if any, is asking for a run.

    Constructed at the request handler that still holds its caller's
    connection — or, for a nested ER→WM→ER dispatch, derived from the calling
    run's connection via :func:`originating_client_for_run` — and threaded
    explicitly down to :func:`bind_run`. ``connection=None`` is the honest
    answer for anything the WM started on its own behalf, or a dispatch that
    never had a client to begin with, and binds nothing.

    ``request_task`` is the WM-owned task serving the client request the run
    started from, so the WM's own cancellation of that task can be told apart
    from every other reason a run ends (:func:`mark_cancelled_by_wm`). A nested
    dispatch leaves it ``None``: its run is not the one the WM cancels.
    """

    connection: asyncio.StreamWriter | None
    request_task: asyncio.Task[typing.Any] | None = None
    delegated_call: DelegatedChildStops | None = None


class ElicitationBridge(typing.Protocol):
    """What the runner needs from whoever owns client connections."""

    def can_elicit(self, *, run_writer_key: asyncio.StreamWriter | None) -> bool:
        """Whether a question could be put to a person right now."""
        ...

    def shape_availability(
        self, *, run_writer_key: asyncio.StreamWriter | None
    ) -> ShapeAvailability:
        """Which shapes the run's originating client declared. Sends nothing."""
        ...

    async def elicit(
        self,
        *,
        message: str,
        options: list[str],
        default: str | None,
        timeout_sec: float,
        run_writer_key: asyncio.StreamWriter | None,
        run_id: str | None,
        kind: str = "choice",
        multiline: bool = False,
        initial_value: str | None = None,
        placeholder: str | None = None,
        descriptions: collections.abc.Mapping[str, str] | None = None,
        preselected: list[str] | None = None,
    ) -> ElicitAnswer:
        """Put a multiple-choice question to one client and return the outcome.

        *run_id* is the run that asks, so the question can be withdrawn from the
        client when that run ends. ``None`` (an ER too old to name its run) is
        never swept, and only its deadline ends the question.

        *run_writer_key* identifies the connection that started the run, as
        obtained from :func:`originating_client_for_run`. ``None`` means the run has
        no identifiable origin — a non-streaming run, or one whose client has
        gone — and must be answered "nobody could be asked" immediately rather
        than being made to wait (ADR-0082 rule 1 with rule 3).

        Returns a dict with an ``outcome`` of ``"answered"``, ``"declined"`` or
        ``"unavailable"``, plus ``value`` (the chosen option) when answered.
        Every way of not getting an answer is one of those outcomes: this
        coroutine does not raise to report one.
        """

    def end_run(self, *, run_id: str) -> None:
        """Withdraw every question of *run_id* that is still waiting for an answer.

        Synchronous: it is called from the dispatch's ``finally``, before the
        run's response is written, and must not suspend.
        """
        ...

    def abandon(self, *, request_task: asyncio.Task[typing.Any]) -> None:
        """Withdraw the question being served by *request_task*, if it is waiting.

        Synchronous, for the same reason as :meth:`end_run`: the withdrawal has
        to be on the wire before anything the ER sends afterwards.
        """
        ...


_installed: ElicitationBridge | None = None


def install(implementation: ElicitationBridge) -> None:
    """Nominate *implementation* as the answer to elicitation requests from an ER."""
    global _installed
    _installed = implementation


def reset() -> None:
    """Forget the installed implementation. Tests only."""
    global _installed
    _installed = None


def handlers() -> ElicitationBridge | None:
    """The installed implementation, or ``None`` if nobody filled the slot.

    ``None`` is a real state rather than a defect: a runner exercised standalone
    has no client connections at all. Unlike ``wm_bridge`` there is no
    drop-everything default, because the caller is waiting on a result only the
    connection owner can produce.
    """
    return _installed


# ---------------------------------------------------------------------------
# Addressing: which connection started a given run
# ---------------------------------------------------------------------------

# Run id → the origin that started it. Written for the life of the dispatch
# and read by an ER on a different connection entirely, which is why this is a
# registry rather than a value carried on the run's own call stack.
_runs: dict[str, RunDispatchOrigin] = {}
# Runs whose client request task the WM itself cancelled. Lives as long as the
# run is bound.
_cancelled_by_wm: set[str] = set()


@contextlib.contextmanager
def bind_run(
    run_id: str, origin: RunDispatchOrigin | None
) -> collections.abc.Iterator[None]:
    """Record *run_id* as belonging to *origin*'s connection.

    Entered where the run identifier is minted, so the binding lasts exactly as
    long as the run does: a question can only be addressed while the run that
    asks it is in flight, and an entry outliving its run would address a later
    question to a client that has moved on. ``origin=None``, or an origin whose
    ``connection`` is ``None``, records nothing rather than an empty entry —
    that is the honest state for a dispatch with no identifiable origin.

    *origin* has no default, here and at every hop that forwards it. Passing an
    explicit ``None`` is cheap and states a real fact; a default would let the
    argument be dropped silently at one hop of a long chain, and the result —
    a connected client that is never asked — looks exactly like a run that
    genuinely had nobody to ask. That is the failure this parameter replaced a
    ``ContextVar`` to avoid, so it must not be reintroduced as a default.
    """
    if origin is None or origin.connection is None:
        yield
        return
    _runs[run_id] = origin
    try:
        yield
    finally:
        # Before the unbind: the sweep still needs the run's cause.
        installed = handlers()
        if installed is not None:
            try:
                installed.end_run(run_id=run_id)
            except Exception as error:
                logger.warning(
                    f"Withdrawing the questions of run {run_id} failed: {error}"
                )
        _runs.pop(run_id, None)
        _cancelled_by_wm.discard(run_id)


def originating_client_for_run(run_id: str | None) -> asyncio.StreamWriter | None:
    """The connection that started *run_id*, or ``None``.

    ``None`` is an ordinary answer rather than a failure: a run dispatched
    through a path that never held a client, one whose client has since gone,
    and an ER too old to name its run all genuinely have nobody to ask, and each
    is told so at once instead of waiting out a deadline (ADR-0082 rule 3).
    """
    if run_id is None:
        return None
    origin = _runs.get(run_id)
    return origin.connection if origin is not None else None


def mark_cancelled_by_wm(task: asyncio.Task[typing.Any]) -> None:
    """Record that the WM is about to cancel *task*, then withdraw its questions.

    ``asyncio`` cancellation carries no cause, so the WM states it here, before
    it cancels a client request task: the runs that started from *task* are
    marked, and their waiting questions are withdrawn with the cause the client
    should be told. Every WM-side site that cancels a client request task must
    call this first.
    """
    run_ids = [
        run_id for run_id, origin in _runs.items() if origin.request_task is task
    ]
    _cancelled_by_wm.update(run_ids)
    installed = handlers()
    if installed is None:
        return
    for run_id in run_ids:
        installed.end_run(run_id=run_id)


def run_cancelled_by_wm(run_id: str) -> bool:
    """Whether the WM marked *run_id* as cancelled by itself."""
    return run_id in _cancelled_by_wm


def reset_origins() -> None:
    """Forget every recorded origin and mark. Tests only."""
    _runs.clear()
    _cancelled_by_wm.clear()
