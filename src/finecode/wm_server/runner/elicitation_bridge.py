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

The two halves are separate on purpose: :func:`originating_client` marks *this
task* as belonging to a connection, and :func:`bind_run` records the run id the
dispatch minted under that connection. The first is a
:class:`contextvars.ContextVar`, so it reaches the dispatch through call chains
and into the tasks it spawns without every layer in between having to carry it;
the second is what turns it into a lookup an ER on another connection can
resolve.
"""

from __future__ import annotations

import collections.abc
import contextlib
import contextvars
import typing

__all__ = [
    "ElicitationBridge",
    "bind_run",
    "handlers",
    "install",
    "originating_client",
    "originating_client_for_run",
    "reset",
    "reset_origins",
]


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

# The connection whose request this task is executing. A ContextVar rather than
# a parameter because the dispatch that mints a run id sits many layers below
# the handler that knows the connection, and every layer in between would
# otherwise have to carry something it has no use for. Tasks copy the context
# they are created in, so a fan-out inherits it without being told.
_origin: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "finecode_elicitation_origin", default=None
)

# Run id → the connection that started it. Written for the life of the dispatch
# and read by an ER on a different connection entirely, which is why this is a
# registry and not just the ContextVar above.
_runs: dict[str, object] = {}


@contextlib.contextmanager
def originating_client(connection: object | None) -> collections.abc.Iterator[None]:
    """Mark this task, and what it starts, as running for *connection*.

    Entered by the request handlers that still hold their caller's connection,
    and again by nested dispatch on behalf of the run that asked for it. Passing
    ``None`` is meaningful: it states that the work in the block has no
    identifiable origin, which is the honest answer for anything the WM started
    on its own behalf.
    """
    token = _origin.set(connection)
    try:
        yield
    finally:
        _origin.reset(token)


@contextlib.contextmanager
def bind_run(run_id: str) -> collections.abc.Iterator[None]:
    """Record *run_id* as belonging to the connection this task is running for.

    Entered where the run identifier is minted, so the binding lasts exactly as
    long as the run does: a question can only be addressed while the run that
    asks it is in flight, and an entry outliving its run would address a later
    question to a client that has moved on. A dispatch with no originating
    connection records nothing rather than an empty entry.
    """
    connection = _origin.get()
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
