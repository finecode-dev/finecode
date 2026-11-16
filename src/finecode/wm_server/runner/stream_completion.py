"""Runner-owned stream registrations for drained run prefixes.

All mutable completion state belongs to the WM dispatch loop: a service
supplies its subscription and stream state downward through this registry; no
runner import of services/API is required. This placement matches the
live-handle lifetime of ``ExtensionRunnerInfo``, not domain purity.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import dataclasses
import enum
import typing

from finecode_jsonrpc.client import ResponseCompletion

from finecode.wm_server import domain
from finecode.wm_server.utils import stream_completion as streams

T = typing.TypeVar("T")


class StreamChannel(enum.Enum):
    PARTIAL = enum.auto()
    PROGRESS = enum.auto()


@dataclasses.dataclass
class StreamSubscription(typing.Generic[T]):
    """One service-owned subscription to a runner stream channel.

    The service owns registration/removal and its consumer. ``admitted`` counts
    successfully typed decoded publications; failures are latched in
    ``admission_errors`` and signal the owner immediately through ``failure``
    without running service callbacks synchronously from publication.
    """

    token: int | str
    channel: StreamChannel
    stream: streams.CompletionStream[T]
    admitted: int = 0
    admission_errors: list[Exception] = dataclasses.field(default_factory=list)
    closed: bool = False
    failure: asyncio.Future[Exception] = dataclasses.field(
        default_factory=streams._new_failure_future
    )

    def admit(self, value: T) -> None:
        """Publish one decoded item; latch and signal publication failures."""
        try:
            self.stream.append(value)
        except Exception as error:
            self.admission_errors.append(error)
            if not self.failure.done():
                self.failure.set_result(error)
            raise
        self.admitted += 1

    def consume_failure(self) -> Exception | None:
        """Take a latched failure and arm a fresh signal for later runs.

        Returns ``None`` when no failure was latched. Resetting here keeps a
        stale signal from failing an unrelated later invocation on the same
        subscription.
        """
        if not self.failure.done():
            return None
        error = self.failure.result()
        self.failure = asyncio.get_running_loop().create_future()
        return error


@dataclasses.dataclass
class ListenerPrefix:
    """One frozen invocation prefix inside a subscribed stream."""

    subscription: StreamSubscription[object]
    end_position: int
    errors: tuple[Exception, ...]
    fence: streams.PrefixFence


@dataclasses.dataclass
class InvocationBoundary:
    """Runner-owned registrations selected for one runner invocation.

    ``capture`` first sets ``received``, then freezes each admitted
    count/error tuple and appends a fence after that prefix. It never ends a
    shared stream. ``wait_prefixes()`` waits only those frozen fences;
    later errors/items cannot extend the frozen prefix, while active pipeline
    faults still trigger owner abort through the subscription signals.
    """

    subscriptions: tuple[StreamSubscription[object], ...]
    prefixes: tuple[ListenerPrefix, ...] = ()
    received: bool = False
    completion: ResponseCompletion = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.completion = ResponseCompletion(capture=self._capture)

    def _capture(self) -> None:
        loop = asyncio.get_running_loop()
        self.received = True
        frozen: list[ListenerPrefix] = []
        for subscription in self.subscriptions:
            fence = streams.PrefixFence(completion=loop.create_future())
            frozen.append(
                ListenerPrefix(
                    subscription=subscription,
                    end_position=subscription.admitted,
                    errors=tuple(subscription.admission_errors),
                    fence=fence,
                )
            )
            subscription.stream.forward(fence)
        self.prefixes = tuple(frozen)

    async def wait_prefixes(self) -> tuple[Exception, ...]:
        """Wait the frozen fences; never wait one that was never inserted."""
        if self.completion.error is not None:
            error = self.completion.error
            for subscription in self.subscriptions:
                subscription.stream.abort(error)
            return (error,)
        errors: list[Exception] = []
        for prefix in self.prefixes:
            outcome = await prefix.fence.completion
            if outcome is not None:
                errors.append(outcome)
        return tuple(errors)


class RunnerStreamRegistry:
    """Service-owned subscriptions, selected per invocation by token."""

    def __init__(self) -> None:
        self._partial: list[StreamSubscription[domain.PartialResultRawValue]] = []
        self._progress: list[StreamSubscription[domain.ProgressRawValue]] = []

    def matching(
        self, channel: StreamChannel, token: int | str
    ) -> list[StreamSubscription[typing.Any]]:
        """Snapshot of currently registered matching subscriptions."""
        if channel is StreamChannel.PARTIAL:
            return [
                subscription
                for subscription in self._partial
                if not subscription.closed and subscription.token == token
            ]
        return [
            subscription
            for subscription in self._progress
            if not subscription.closed and subscription.token == token
        ]

    @contextlib.contextmanager
    def subscribe_partial(
        self, token: int | str
    ) -> collections.abc.Generator[
        StreamSubscription[domain.PartialResultRawValue], None, None
    ]:
        """Own one partial subscription for ``token`` until context exit."""
        subscription: StreamSubscription[domain.PartialResultRawValue] = (
            StreamSubscription(
                token=token,
                channel=StreamChannel.PARTIAL,
                stream=streams.CompletionStream(),
            )
        )
        self._partial.append(subscription)
        try:
            yield subscription
        finally:
            subscription.closed = True
            with contextlib.suppress(ValueError):
                self._partial.remove(subscription)

    @contextlib.contextmanager
    def subscribe_progress(
        self, token: int | str
    ) -> collections.abc.Generator[
        StreamSubscription[domain.ProgressRawValue], None, None
    ]:
        """Own one progress subscription for ``token`` until context exit."""
        subscription: StreamSubscription[domain.ProgressRawValue] = StreamSubscription(
            token=token,
            channel=StreamChannel.PROGRESS,
            stream=streams.CompletionStream(),
        )
        self._progress.append(subscription)
        try:
            yield subscription
        finally:
            subscription.closed = True
            with contextlib.suppress(ValueError):
                self._progress.remove(subscription)

    def begin_invocation(
        self,
        partial_token: int | str | None,
        progress_token: int | str | None,
    ) -> InvocationBoundary:
        """Select currently registered subscriptions matching the options.

        Multiple existing token matches retain today's semantics; no new
        correlation promise is made. With no subscriptions, callers use the
        unchanged request path, without completion bookkeeping.
        """
        selected: list[StreamSubscription[object]] = []
        if partial_token is not None:
            selected.extend(self.matching(StreamChannel.PARTIAL, partial_token))
        if progress_token is not None:
            selected.extend(self.matching(StreamChannel.PROGRESS, progress_token))
        return InvocationBoundary(subscriptions=tuple(selected))
