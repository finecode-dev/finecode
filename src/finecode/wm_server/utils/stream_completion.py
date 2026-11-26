"""Single-consumer completion streams for drained runner prefixes.

This module is dependency-neutral: it imports only the standard library, so it
can sit underneath the runner, service and API layers without pulling any of
them (or domain/errors) upward. All queues here are single-consumer and
unbounded. The existing ``AsyncList`` and its independent-reader semantics are
unchanged and are not narrowed by these streams.
"""

from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import enum
import typing

T = typing.TypeVar("T")


@dataclasses.dataclass
class StreamValue(typing.Generic[T]):
    """One delivered payload inside a completion stream."""

    value: T


@dataclasses.dataclass
class PrefixFence:
    """Boundary marker freezing the prefix that preceded it.

    The future carries ``None`` once the final sink has handed the whole
    prefix off locally, or the responsible fault when the prefix cannot
    complete. It is a plain completion value, not a new public result flag.
    """

    completion: asyncio.Future[Exception | None]


class StreamState(enum.Enum):
    OPEN = enum.auto()
    ENDED = enum.auto()
    ABORTED = enum.auto()


class StreamEnd(enum.Enum):
    END = enum.auto()


@dataclasses.dataclass
class RunSucceeded(typing.Generic[T]):
    """A received runner outcome carrying a successful response."""

    response: T


@dataclasses.dataclass
class RunFailed:
    """A received runner outcome carrying a runner-side error."""

    error: Exception


@dataclasses.dataclass
class RunInterrupted:
    """Caller/transport interruption, not a received runner cancellation.

    A received runner cancellation response still drains before the existing
    cancellation mapping runs; this outcome is only for interruptions that
    arrive without a runner response.
    """

    error: BaseException


@dataclasses.dataclass
class PipelineFailure(Exception):
    """Internal wrapper distinguishing pipeline faults from runner errors.

    Represents an interrupted receipt where an ``Exception``-valued completion
    is needed while preserving the original interruption. It is not a public
    exception schema or a replacement for ``ActionRunFailed`` mapping, which
    stays owned by the matrix/service adapters.
    """

    cause: BaseException


StreamEvent: typing.TypeAlias = StreamValue[T] | PrefixFence
"""One item transferred through a stream: a payload or a prefix boundary."""

TerminalOutcome: typing.TypeAlias = RunSucceeded[T] | RunFailed | RunInterrupted
"""A received runner outcome, kept separate from pipeline faults."""


@dataclasses.dataclass
class CompletionStream(typing.Generic[T]):
    """FIFO value/fence channel with explicit prefix completion.

    ``events()`` transfers events without acknowledging them: intermediate
    stages forward each fence only after all preceding transformations and
    enqueues, preserving FIFO. Iterating the stream directly (``async for``)
    is the final-sink behavior: values are yielded to the consumer body and
    each encountered fence is acknowledged with ``None`` when iteration
    resumes after that body, i.e. only after the local handoff. Faults resolve
    pending fences with their cause instead.
    """

    queue: asyncio.Queue[StreamEvent[T] | StreamEnd] = dataclasses.field(
        default_factory=asyncio.Queue
    )
    state: StreamState = StreamState.OPEN
    fences: list[PrefixFence] = dataclasses.field(default_factory=list)

    def append(self, value: T) -> None:
        """Enqueue one payload; dropped once the stream is no longer open."""
        if self.state is not StreamState.OPEN:
            return
        self.queue.put_nowait(StreamValue(value))

    def forward(self, fence: PrefixFence) -> None:
        """Pass a prefix boundary downstream, tracked for abort settlement.

        A fence arriving after the stream closed can never be delivered, so
        settle it immediately instead of stranding its drain waiter: an
        aborted stream resolves it with its cause, an ended stream with a
        pipeline fault. Idempotent with ``abort``/``end`` settlement.
        """
        if self.state is not StreamState.OPEN:
            if not fence.completion.done():
                fence.completion.set_result(
                    PipelineFailure(RuntimeError("stream closed before fence"))
                )
            return
        self.fences.append(fence)
        self.queue.put_nowait(fence)

    def end(self) -> None:
        """Close the stream after draining already-queued events."""
        if self.state is not StreamState.OPEN:
            return
        self.state = StreamState.ENDED
        self.queue.put_nowait(StreamEnd.END)

    def abort(self, error: Exception) -> None:
        """Stop forwarding and settle every pending fence with ``error``."""
        if self.state is StreamState.ABORTED:
            return
        for fence in self.fences:
            if not fence.completion.done():
                fence.completion.set_result(error)
        self.state = StreamState.ABORTED
        # Wake a consumer suspended in ``get()``; it observes the state and
        # stops without forwarding.
        self.queue.put_nowait(StreamEnd.END)

    async def events(self) -> collections.abc.AsyncIterator[StreamEvent[T]]:
        """Yield queued events without acknowledging fences."""
        while True:
            if self.state is StreamState.ABORTED:
                return
            item = await self.queue.get()
            if isinstance(item, StreamEnd):
                return
            if self.state is StreamState.ABORTED:
                return
            yield item

    async def __aiter__(self) -> collections.abc.AsyncIterator[T]:
        """Final-sink iteration: acknowledge each fence after the handoff."""
        pending: PrefixFence | None = None
        while True:
            if pending is not None:
                fence, pending = pending, None
                if not fence.completion.done():
                    fence.completion.set_result(None)
            if self.state is StreamState.ABORTED:
                return
            item = await self.queue.get()
            if isinstance(item, StreamEnd):
                return
            if self.state is StreamState.ABORTED:
                return
            if isinstance(item, PrefixFence):
                pending = item
            else:
                yield item.value


def _never_protect(value: T) -> bool:
    _ = value
    return False


@dataclasses.dataclass
class BoundedProgressStream(CompletionStream[T]):
    """A completion stream holding at most ``limit`` values (fences unbounded).

    Delegated progress shares one of these per call across every child runner
    of the call, so a flooding child cannot grow the WM's backlog without
    bound. When a new value arrives at a full buffer, the oldest
    pending value is removed — except the newest pending value the
    ``protect`` predicate keeps, which is never removed to make room (it
    only leaves through delivery, or when a newer owed value arrives and the
    old one becomes an ordinary value). Fences are never removed and keep
    their position. ``forward``/``end``/``abort`` and final-sink iteration
    behave exactly like ``CompletionStream``'s. Admission is synchronous: it
    never suspends and never raises because of the bound (ADR-0134 rule 1).
    """

    limit: int = 100
    protect: collections.abc.Callable[[T], bool] = _never_protect

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"BoundedProgressStream limit must be >= 1")

    def append(self, value: T) -> None:
        """Enqueue one payload, discarding the oldest unprotected one when full."""
        if self.state is not StreamState.OPEN:
            return
        if self.queue.qsize() >= self.limit and not self._evict_for_newcomer():
            # The only pending value is the protected newest owed one: drop
            # the newcomer rather than the report the reply owes the parent.
            return
        self.queue.put_nowait(StreamValue(value))

    def _evict_for_newcomer(self) -> bool:
        """Remove the oldest pending unprotected value; True if room was made.

        Drains and restores the queue synchronously, so no consumer can
        observe the intermediate state; order is preserved. Fences ride along
        untouched.
        """
        items: list[StreamEvent[T] | StreamEnd] = []
        try:
            while True:
                items.append(self.queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        values = [item for item in items if isinstance(item, StreamValue)]
        if len(values) < self.limit:
            for item in items:
                self.queue.put_nowait(item)
            return True
        newest_owed = -1
        for index, wrapper in enumerate(values):
            if self.protect(wrapper.value):
                newest_owed = index
        victim: StreamValue[T] | None = None
        for index, wrapper in enumerate(values):
            if index != newest_owed:
                victim = wrapper
                break
        if victim is None:
            for item in items:
                self.queue.put_nowait(item)
            return False
        for item in items:
            if item is not victim:
                self.queue.put_nowait(item)
        return True


def _new_failure_future() -> asyncio.Future[Exception]:
    return asyncio.get_running_loop().create_future()


@dataclasses.dataclass
class OwnedRunTasks:
    """Tracked child tasks with immediate failure signaling and owned teardown.

    ``start()`` tracks children; a non-cancellation child failure signals
    ``failure`` immediately. ``aclose()`` runs the owner-supplied cleanup
    (subscription/hook removal), cancels and joins children and pending waits,
    then joins the strongly held finalizer task — shielding and retrying that
    same finalizer across repeated outer cancellations and re-raising the
    first interruption only after cleanup. There is no detached finalizer,
    ``uncancel()`` state reset, runtime timeout, or wait for remote
    acknowledgment.
    """

    tasks: set[asyncio.Task[object]] = dataclasses.field(default_factory=set)
    failure: asyncio.Future[Exception] = dataclasses.field(
        default_factory=_new_failure_future
    )
    finalizer: asyncio.Task[None] | None = None

    def start(
        self, awaitable: collections.abc.Coroutine[typing.Any, typing.Any, T]
    ) -> asyncio.Task[T]:
        """Track one owned child; signal ``failure`` if it fails."""
        task: asyncio.Task[T] = asyncio.ensure_future(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self._child_done)
        return task

    def _child_done(self, task: asyncio.Task[object]) -> None:
        self.tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None or self.failure.done():
            return
        if isinstance(error, Exception):
            self.failure.set_result(error)
        else:
            self.failure.set_result(PipelineFailure(error))

    async def aclose(self, cleanup: collections.abc.Callable[[], None]) -> None:
        """Remove ownership, join children and the finalizer, then re-raise."""
        cleanup()
        for task in list(self.tasks):
            if task.done():
                self.tasks.discard(task)
        pending: asyncio.CancelledError | None = None
        for child in list(self.tasks):
            if not child.done():
                _ = child.cancel()
        while True:
            remaining = [task for task in self.tasks if not task.done()]
            if not remaining:
                break
            try:
                await asyncio.gather(*remaining, return_exceptions=True)
            except asyncio.CancelledError as exc:
                if pending is None:
                    pending = exc
                for child in remaining:
                    if not child.done():
                        _ = child.cancel()
        if self.finalizer is not None:
            # Loop on the finalizer's state, not `while True`: when the
            # finalizer itself is cancelled, shielding it again raises at
            # once without yielding.
            while not self.finalizer.done():
                try:
                    await asyncio.shield(self.finalizer)
                except asyncio.CancelledError as exc:
                    if pending is None:
                        pending = exc
        if pending is not None:
            raise pending
