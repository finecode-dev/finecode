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

import collections.abc
import contextlib
import dataclasses
import typing

__all__ = [
    "ElicitationBridge",
    "RunDispatchOrigin",
    "bind_run",
    "handlers",
    "install",
    "originating_client_for_run",
    "reset",
    "reset_origins",
]


@dataclasses.dataclass(frozen=True, slots=True)
class RunDispatchOrigin:
    """Which client connection, if any, is asking for a run.

    Constructed at the request handler that still holds its caller's
    connection — or, for a nested ER→WM→ER dispatch, derived from the calling
    run's connection via :func:`originating_client_for_run` — and threaded
    explicitly down to :func:`bind_run`. ``connection=None`` is the honest
    answer for anything the WM started on its own behalf, or a dispatch that
    never had a client to begin with, and binds nothing.
    """

    connection: object | None


class ElicitationBridge(typing.Protocol):
    """What the runner needs from whoever owns client connections."""

    async def elicit(
        self,
        *,
        message: str,
        options: list[str],
        default: str | None,
        timeout_sec: float,
        run_writer_key: object | None,
    ) -> dict:
        """Put a multiple-choice question to one client and return the outcome.

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

# Run id → the connection that started it. Written for the life of the dispatch
# and read by an ER on a different connection entirely, which is why this is a
# registry rather than a value carried on the run's own call stack.
_runs: dict[str, object] = {}


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
    connection = origin.connection if origin is not None else None
    if connection is None:
        yield
        return
    _runs[run_id] = connection
    try:
        yield
    finally:
        _runs.pop(run_id, None)


def originating_client_for_run(run_id: str | None) -> object | None:
    """The connection that started *run_id*, or ``None``.

    ``None`` is an ordinary answer rather than a failure: a run dispatched
    through a path that never held a client, one whose client has since gone,
    and an ER too old to name its run all genuinely have nobody to ask, and each
    is told so at once instead of waiting out a deadline (ADR-0082 rule 3).
    """
    if run_id is None:
        return None
    return _runs.get(run_id)


def reset_origins() -> None:
    """Forget every recorded origin. Tests only."""
    _runs.clear()
