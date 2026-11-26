"""FIFO completion streams and owned task teardown.

Observable contract: a drained runner prefix reaches its consumer through
values, fences, service mapping and writer handoff in FIFO order before the
received response is exposed. These unit tests pin the shared stream and
ownership primitives that every covered pipeline stage builds on.
"""

from __future__ import annotations

import asyncio
import typing

import pytest

from finecode.wm_server.utils import stream_completion as streams


async def _collect(stream: streams.CompletionStream[str]) -> list[str]:
    return [value async for value in stream]


async def test_fifo_values_fence_and_events_transfer_without_ack() -> None:
    """Values and fences transfer in order; intermediates never acknowledge."""
    stream: streams.CompletionStream[str] = streams.CompletionStream()
    stream.append("a")
    stream.append("b")
    fence = streams.PrefixFence(completion=asyncio.get_running_loop().create_future())
    stream.forward(fence)
    stream.append("c")
    stream.end()

    seen: list[object] = []
    async for event in stream.events():
        seen.append(event)
    assert seen[0] == streams.StreamValue("a")
    assert seen[1] == streams.StreamValue("b")
    assert seen[2] is fence
    assert seen[3] == streams.StreamValue("c")
    # Intermediate transfer did not acknowledge the fence.
    assert not fence.completion.done()


async def test_final_consumer_acks_fence_only_after_body_resumes() -> None:
    """The fence resolves once iteration resumes past the preceding body."""
    stream: streams.CompletionStream[str] = streams.CompletionStream()
    stream.append("a")
    fence = streams.PrefixFence(completion=asyncio.get_running_loop().create_future())
    stream.forward(fence)
    stream.append("b")
    stream.end()

    iterator = stream.__aiter__()
    assert await iterator.__anext__() == "a"
    # Still inside the first body: the fence must not be acknowledged yet.
    assert not fence.completion.done()
    assert await iterator.__anext__() == "b"
    # Resuming past the fenced prefix acknowledged it with no fault.
    assert fence.completion.done()
    assert fence.completion.result() is None
    with pytest.raises(StopAsyncIteration):
        await iterator.__anext__()


async def test_intermediate_forwards_fence_after_preceding_transforms() -> None:
    """A mapping stage enqueues transformed values before the same fence."""
    upstream: streams.CompletionStream[str] = streams.CompletionStream()
    downstream: streams.CompletionStream[str] = streams.CompletionStream()
    upstream.append("a")
    fence = streams.PrefixFence(completion=asyncio.get_running_loop().create_future())
    upstream.forward(fence)
    upstream.append("b")
    upstream.end()

    async for event in upstream.events():
        if isinstance(event, streams.StreamValue):
            downstream.append(event.value.upper())
        else:
            downstream.forward(event)
    downstream.end()

    assert await _collect(downstream) == ["A", "B"]
    # The final sink acknowledged the forwarded fence after its handoff.
    assert fence.completion.result() is None


async def test_end_drains_unread_events() -> None:
    """Values queued before the end are still delivered to a late consumer."""
    stream: streams.CompletionStream[str] = streams.CompletionStream()
    stream.append("a")
    stream.append("b")
    stream.end()
    stream.end()  # repeated end is idempotent
    assert await _collect(stream) == ["a", "b"]


async def test_empty_prefix_finishes_without_values() -> None:
    """An empty stream ends immediately; a fenceless prefix needs no marker."""
    stream: streams.CompletionStream[str] = streams.CompletionStream()
    stream.end()
    assert await _collect(stream) == []


async def test_abort_stops_forwarding_and_settles_pending_fences() -> None:
    """Abort drops the queued prefix and resolves fences with its cause."""
    stream: streams.CompletionStream[str] = streams.CompletionStream()
    stream.append("a")
    fence = streams.PrefixFence(completion=asyncio.get_running_loop().create_future())
    stream.forward(fence)
    cause = RuntimeError("writer down")
    stream.abort(cause)
    stream.abort(cause)  # repeated abort is idempotent
    stream.append("late")  # post-abort items cannot extend anything
    stream.end()  # end after abort is a no-op

    assert await _collect(stream) == []
    assert fence.completion.done()
    assert fence.completion.result() is cause


async def test_repeated_ack_is_idempotent() -> None:
    """Acknowledging an already-settled fence is a safe no-op."""
    stream: streams.CompletionStream[str] = streams.CompletionStream()
    fence = streams.PrefixFence(completion=asyncio.get_running_loop().create_future())
    stream.forward(fence)
    stream.end()
    assert await _collect(stream) == []
    assert fence.completion.result() is None
    # Second ack after completion must not raise.
    if not fence.completion.done():
        fence.completion.set_result(None)


async def test_terminal_outcome_records() -> None:
    """Outcome records carry responses, errors and interruptions distinctly."""
    ok: streams.TerminalOutcome[str] = streams.RunSucceeded(response="done")
    assert isinstance(ok, streams.RunSucceeded) and ok.response == "done"
    failed: streams.TerminalOutcome[str] = streams.RunFailed(error=ValueError("x"))
    assert isinstance(failed, streams.RunFailed)
    err = asyncio.CancelledError()
    interrupted: streams.TerminalOutcome[str] = streams.RunInterrupted(error=err)
    assert isinstance(interrupted, streams.RunInterrupted) and interrupted.error is err
    wrapped = streams.PipelineFailure(err)
    assert isinstance(wrapped, Exception) and wrapped.cause is err


async def test_owned_tasks_signal_child_failure_immediately() -> None:
    """A non-cancellation child failure signals the owner without waiting."""
    owned = streams.OwnedRunTasks()

    async def _boom() -> None:
        raise ValueError("child down")

    _ = owned.start(_boom())
    error = await asyncio.wait_for(asyncio.ensure_future(owned.failure), timeout=5)
    assert isinstance(error, ValueError)
    await owned.aclose(lambda: None)


async def test_owned_tasks_wrap_non_exception_child_failure() -> None:
    """A BaseException child failure arrives wrapped, preserving its cause."""
    owned = streams.OwnedRunTasks()

    class _Stop(BaseException):
        """Test-only interruption that is not an ``Exception``."""

    async def _interrupt() -> None:
        raise _Stop("stop")

    task = owned.start(_interrupt())
    with pytest.raises(_Stop):
        await task
    error = owned.failure.result()
    assert isinstance(error, streams.PipelineFailure)
    assert isinstance(error.cause, _Stop)
    await owned.aclose(lambda: None)


async def test_owned_tasks_aclose_cancels_joins_and_runs_cleanup() -> None:
    """Teardown removes ownership, joins children and honors the finalizer."""
    owned = streams.OwnedRunTasks()
    cleaned: list[str] = []
    started = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def _sleeper() -> None:
        started.set()
        await asyncio.sleep(3600)

    async def _finalizer() -> None:
        await release_finalizer.wait()

    sleeper = owned.start(_sleeper())
    owned.finalizer = asyncio.ensure_future(_finalizer())
    await asyncio.wait_for(started.wait(), timeout=5)
    closing = asyncio.ensure_future(owned.aclose(lambda: cleaned.append("cleanup")))
    for _ in range(100):
        if sleeper.done():
            break
        await asyncio.sleep(0)
    assert cleaned == ["cleanup"]
    assert sleeper.done()
    # Repeated outer cancellation while joining still completes teardown.
    _ = closing.cancel()
    await asyncio.sleep(0)
    _ = closing.cancel()
    await asyncio.sleep(0)
    release_finalizer.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert sleeper.done()
    assert owned.finalizer is not None and owned.finalizer.done()


async def test_owned_tasks_aclose_ends_when_finalizer_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalizer cancelled from outside ends the shielded join.

    Event-loop teardown cancels every task, the finalizer included. ``aclose``
    must then stop shielding it and raise the interruption; re-shielding a
    cancelled task raises at once without yielding, so it would spin forever.
    """
    owned = streams.OwnedRunTasks()
    finalizer_started = asyncio.Event()

    async def _finalizer() -> None:
        finalizer_started.set()
        await asyncio.sleep(3600)

    owned.finalizer = asyncio.ensure_future(_finalizer())
    real_shield = asyncio.shield
    finalizer_shields = 0

    class _Spinning(Exception):
        pass

    def _guarded_shield(arg: typing.Any) -> typing.Any:
        nonlocal finalizer_shields
        if arg is owned.finalizer:
            finalizer_shields += 1
            if finalizer_shields > 50:
                raise _Spinning("aclose keeps re-shielding a finished finalizer")
        return real_shield(arg)

    monkeypatch.setattr(streams.asyncio, "shield", _guarded_shield)
    await asyncio.wait_for(finalizer_started.wait(), timeout=5)
    closing = asyncio.ensure_future(owned.aclose(lambda: None))
    await asyncio.sleep(0)
    _ = owned.finalizer.cancel()
    done, _pending = await asyncio.wait({closing}, timeout=1.0)
    assert closing in done, "aclose did not end after its finalizer was cancelled"
    assert closing.cancelled(), f"expected CancelledError, got {closing!r}"


async def test_owned_tasks_aclose_without_finalizer_still_joins() -> None:
    """Teardown with no finalizer joins cancelled children and returns."""
    owned = streams.OwnedRunTasks()
    done = asyncio.Event()

    async def _quick() -> None:
        done.set()

    _ = owned.start(_quick())
    await asyncio.wait_for(done.wait(), timeout=5)
    await owned.aclose(lambda: None)
    assert not owned.tasks


def test_stream_state_enum_values() -> None:
    """Stream lifecycle states are exactly open, ended and aborted."""
    assert [state.name for state in streams.StreamState] == [
        "OPEN",
        "ENDED",
        "ABORTED",
    ]
