"""Bounded delegated-progress buffer: one shared bound per call (P2, D4.volume).

The shared progress output of a delegated call holds at most 100 pending
values across every child runner of the call; overflow discards the oldest
pending value, never the newest owed one, and never a fence. Admission never
suspends or raises. Bridge tests pin the bound end to end: with the parent ER
stalled, a 1000-notification flood from one or two runners forwards at most
101 values (100 buffered + 1 in hand) and the newest owed value still reaches
the parent before the reply.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import pathlib
import typing
from unittest import mock

from finecode.wm_server import domain, testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, elicitation_bridge
from finecode.wm_server.services.run_service import delegated_progress, er_dispatch
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.utils import stream_completion as completion_streams

_CANONICAL_SOURCE = "test.actions.TestAction"
_LIMIT = delegated_progress.DELEGATED_PROGRESS_PENDING_LIMIT


def _report(message: str | None) -> dict:
    return {"type": "report", "message": message}


def _begin(title: str, message: str | None = None) -> dict:
    return {"type": "begin", "title": title, "message": message}


def _end(message: str | None = None) -> dict:
    return {"type": "end", "message": message}


async def _drain_values(
    stream: completion_streams.CompletionStream[dict],
) -> list[dict]:
    return [value async for value in stream]


async def test_evicts_oldest_first() -> None:
    """A full buffer discards the oldest value for each newcomer."""
    stream: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(limit=3, protect=lambda _: False)
    )
    for index in range(5):
        stream.append(_report(f"m{index}"))
    stream.end()
    assert await _drain_values(stream) == [_report("m2"), _report("m3"), _report("m4")]


async def test_newest_owed_is_never_evicted() -> None:
    """Overflow passes over the newest owed value to the next-oldest one."""
    owed = _report("keep")
    filler = _report(None)
    stream: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(
            limit=3, protect=delegated_progress.is_owed_progress_value
        )
    )
    stream.append(owed)
    stream.append(dict(filler))
    stream.append(dict(filler))
    stream.append(dict(filler))
    stream.append(dict(filler))
    stream.end()
    assert await _drain_values(stream) == [owed, filler, filler]


async def test_newer_owed_releases_old_protection() -> None:
    """A newer owed arrival makes the previous owed value evictable again."""
    first_owed = _report("first")
    second_owed = _report("second")
    filler = _report(None)
    stream: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(
            limit=2, protect=delegated_progress.is_owed_progress_value
        )
    )
    stream.append(first_owed)
    stream.append(second_owed)
    stream.append(dict(filler))
    stream.end()
    assert await _drain_values(stream) == [second_owed, filler]


async def test_message_less_suffix_keeps_trailing_end() -> None:
    """A flood of message-less reports still delivers the trailing end (G24)."""
    stream: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(
            limit=3, protect=delegated_progress.is_owed_progress_value
        )
    )
    for index in range(10):
        stream.append(_report(None))
    stream.append(_end("done"))
    stream.end()
    drained = await _drain_values(stream)
    assert drained[-1] == _end("done")
    assert len(drained) == 3


async def test_fences_survive_eviction_and_keep_position() -> None:
    """Fences are never removed; values around them still turn over."""
    stream: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(limit=1, protect=lambda _: False)
    )
    fence = completion_streams.PrefixFence(
        completion=asyncio.get_running_loop().create_future()
    )
    stream.append(_report("old"))
    stream.forward(fence)
    stream.append(_report("new"))
    stream.end()
    seen: list[dict] = []
    async for value in stream:
        seen.append(value)
    assert seen == [_report("new")]
    assert fence.completion.done() and fence.completion.result() is None


async def test_end_and_abort_match_completion_stream() -> None:
    """End drains what is buffered; abort settles fences and stops delivery."""
    stream: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(limit=10, protect=lambda _: False)
    )
    stream.append(_report("a"))
    stream.end()
    assert await _drain_values(stream) == [_report("a")]

    failing: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(limit=10, protect=lambda _: False)
    )
    fence = completion_streams.PrefixFence(
        completion=asyncio.get_running_loop().create_future()
    )
    failing.append(_report("a"))
    failing.forward(fence)
    error = RuntimeError("child failed")
    failing.abort(error)
    assert await _drain_values(failing) == []
    assert fence.completion.done() and fence.completion.result() is error


async def test_admission_never_suspends_or_raises() -> None:
    """A flood far past the bound is absorbed by synchronous admission alone."""
    stream: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(
            limit=_LIMIT, protect=delegated_progress.is_owed_progress_value
        )
    )
    for index in range(10 * _LIMIT):
        stream.append(_report(f"m{index}"))
    stream.end()
    assert len(await _drain_values(stream)) == _LIMIT


async def test_limit_below_one_rejected() -> None:
    """A bound of zero would admit nothing, so construction refuses it."""
    try:
        completion_streams.BoundedProgressStream(limit=0, protect=lambda _: False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


class _BlockingClient(wm_testing.FakeErClient):
    """A parent ER stalled on progress delivery until the test releases it."""

    def __init__(self, progress_token: int | str) -> None:
        super().__init__()
        self._progress_token = progress_token
        self.gate = asyncio.Event()
        self.first_progress_seen = asyncio.Event()

    async def notify_and_drain(
        self, method: str, params: typing.Any | None = None
    ) -> None:
        if (
            isinstance(params, _internal_client_types.ProgressParams)
            and params.token == self._progress_token
        ):
            self.first_progress_seen.set()
            await self.gate.wait()
        self.sent_requests.append((method, params))


class _FakeBridgeCtx:
    def __init__(
        self,
        partials: completion_streams.CompletionStream[dict],
        progress: completion_streams.BoundedProgressStream[dict],
        responses: list[typing.Any],
    ) -> None:
        self._partials = partials
        self.progress = progress
        self.responses = responses

    def __aiter__(self) -> typing.AsyncIterator[dict]:
        return self._partials.__aiter__()


def _make_runner_and_context(
    dir_path: pathlib.Path, client: wm_testing.FakeErClient
) -> tuple[typing.Any, typing.Any]:
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runner = wm_testing.make_running_runner(
        working_dir_path=dir_path, env_name="test_env", client=client
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return runner, ws_context


def _response(return_code: int = 0) -> typing.Any:
    class _Resp:
        def __init__(self) -> None:
            self.status = "streamed"
            self.result_by_format: dict[str, dict] = {}
            self.return_code = return_code

    return _Resp()


def _params(
    partial_token: int | str, progress_token: int | str | None
) -> _internal_client_types.RunActionInProjectParams:
    return _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=partial_token,
        progress_token=progress_token,
        run_id="run-1",
    )


@contextlib.asynccontextmanager
async def _bounded_delegated_call(
    runner: typing.Any,
    params: _internal_client_types.RunActionInProjectParams,
    runner_feeds: list[list[dict]],
    responses: list[typing.Any],
    partials: list[dict] | None = None,
) -> collections.abc.AsyncGenerator[_FakeBridgeCtx, None]:
    """A delegated child run feeding real bounded streams from N runners."""
    progress_out: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(
            limit=_LIMIT, protect=delegated_progress.is_owed_progress_value
        )
    )
    partials_out: completion_streams.CompletionStream[dict] = (
        completion_streams.CompletionStream()
    )

    async def _flood(values: list[dict]) -> None:
        for index, value in enumerate(values):
            progress_out.append(value)
            if index % 25 == 0:
                await asyncio.sleep(0)

    feeders = [asyncio.ensure_future(_flood(values)) for values in runner_feeds]

    async def _finish() -> None:
        await asyncio.gather(*feeders)
        for partial in partials or []:
            partials_out.append({"json": partial})
        progress_out.end()
        partials_out.end()

    finisher = asyncio.ensure_future(_finish())
    ctx = _FakeBridgeCtx(partials_out, progress_out, responses)

    def _fake_run_action_with_partial_results(
        self: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        _ = (self, kwargs)
        return _bridge_cm(ctx, finisher)

    @contextlib.asynccontextmanager
    async def _bridge_cm(
        bridge_ctx: _FakeBridgeCtx, done: asyncio.Task[None]
    ) -> collections.abc.AsyncGenerator[_FakeBridgeCtx, None]:
        try:
            yield bridge_ctx
        finally:
            if not done.done():
                _ = done.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await done

    with mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _fake_run_action_with_partial_results,
    ):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            yield ctx


def _progress_values(client: wm_testing.FakeErClient, token: int | str) -> list[dict]:
    return [
        json.loads(notif.value)
        for _, notif in client.sent_requests
        if isinstance(notif, _internal_client_types.ProgressParams)
        and notif.token == token
    ]


async def _run_until_progress_stalled(
    runner: typing.Any,
    ws_context: typing.Any,
    params: _internal_client_types.RunActionInProjectParams,
    client: _BlockingClient,
) -> asyncio.Task[typing.Any]:
    task = asyncio.ensure_future(
        er_dispatch._BridgeHandlers()._run_action_in_project(
            runner,
            params,
            er_dispatch.ProjectExecutor(ws_context),
            elicitation_bridge.RunDispatchOrigin(connection=None),
        )
    )
    await asyncio.wait_for(client.first_progress_seen.wait(), timeout=10.0)
    # The pump holds the first value; let the flood accumulate behind it.
    await asyncio.sleep(0.2)
    return task


async def test_flood_from_one_runner_stays_bounded(tmp_path: pathlib.Path) -> None:
    """1000 notifications with a stalled parent forward at most 101 values."""
    parent_token = "work-parent"
    client = _BlockingClient(parent_token)
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    values = [_report(f"m{i}") for i in range(1000)] + [_end("done")]
    params = _params(partial_token=31, progress_token=parent_token)
    async with _bounded_delegated_call(
        runner, params, [values], [_response()], [{"text": "p"}]
    ):
        task = await _run_until_progress_stalled(runner, ws_context, params, client)
        client.gate.set()
        result = await asyncio.wait_for(task, timeout=10.0)
    assert result.return_code == 0
    forwarded = _progress_values(client, parent_token)
    assert len(forwarded) <= _LIMIT + 1
    assert forwarded[-1] == _end("done")


async def test_flood_from_two_runners_shares_one_bound(
    tmp_path: pathlib.Path,
) -> None:
    """Two runners flooding one call still forward at most 101 values total."""
    parent_token = "work-parent-2"
    client = _BlockingClient(parent_token)
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    feed_a = [_report(f"a{i}") for i in range(1000)]
    feed_b = [_report(f"b{i}") for i in range(1000)] + [_end("done")]
    params = _params(partial_token=32, progress_token=parent_token)
    async with _bounded_delegated_call(
        runner, params, [feed_a, feed_b], [_response()]
    ):
        task = await _run_until_progress_stalled(runner, ws_context, params, client)
        client.gate.set()
        result = await asyncio.wait_for(task, timeout=10.0)
    assert result.return_code == 0
    forwarded = _progress_values(client, parent_token)
    assert len(forwarded) <= _LIMIT + 1
    assert forwarded[-1] == _end("done")


async def test_flood_with_error_reply_stays_bounded(tmp_path: pathlib.Path) -> None:
    """A child error reply still delivers the bounded tail incl. the end (G4)."""
    parent_token = "work-parent-3"
    client = _BlockingClient(parent_token)
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    values = [_report(f"m{i}") for i in range(1000)] + [_end("failed")]
    params = _params(partial_token=33, progress_token=parent_token)
    async with _bounded_delegated_call(
        runner, params, [values], [_response(return_code=1)]
    ):
        task = await _run_until_progress_stalled(runner, ws_context, params, client)
        client.gate.set()
        result = await asyncio.wait_for(task, timeout=10.0)
    assert result.return_code == 1
    forwarded = _progress_values(client, parent_token)
    assert len(forwarded) <= _LIMIT + 1
    assert forwarded[-1] == _end("failed")


async def test_message_less_flood_without_end_forwards_tail(
    tmp_path: pathlib.Path,
) -> None:
    """Without a trailing end, the tail of the flood is what gets through (G24)."""
    parent_token = "work-parent-4"
    client = _BlockingClient(parent_token)
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    values = [_report(None) for _ in range(1000)]
    params = _params(partial_token=34, progress_token=parent_token)
    async with _bounded_delegated_call(
        runner, params, [values], [_response()]
    ):
        task = await _run_until_progress_stalled(runner, ws_context, params, client)
        client.gate.set()
        result = await asyncio.wait_for(task, timeout=10.0)
    assert result.return_code == 0
    forwarded = _progress_values(client, parent_token)
    assert len(forwarded) <= _LIMIT + 1
    assert all(value == _report(None) for value in forwarded)


async def test_partials_and_final_fallback_unaffected_by_bound(
    tmp_path: pathlib.Path,
) -> None:
    """Bounding progress changes nothing about partials or the final fallback."""
    parent_token = "work-parent-5"
    client = _BlockingClient(parent_token)
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    values = [_report(f"m{i}") for i in range(300)] + [_end("done")]
    params = _params(partial_token=35, progress_token=parent_token)
    async with _bounded_delegated_call(
        runner, params, [values], [_response()], [{"text": "p1"}, {"text": "p2"}]
    ):
        task = await _run_until_progress_stalled(runner, ws_context, params, client)
        client.gate.set()
        result = await asyncio.wait_for(task, timeout=10.0)
    assert result.return_code == 0
    by_token: dict[typing.Any, list[typing.Any]] = {}
    for _, notif in client.sent_requests:
        assert isinstance(notif, _internal_client_types.ProgressParams)
        by_token.setdefault(notif.token, []).append(json.loads(notif.value))
    assert by_token[35] == [{"text": "p1"}, {"text": "p2"}]
    assert by_token[parent_token][-1] == _end("done")


async def test_child_exception_still_fails_the_call(tmp_path: pathlib.Path) -> None:
    """An exception from the child run propagates instead of replying success."""

    @contextlib.asynccontextmanager
    async def _failing(
        *args: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[typing.NoReturn, None]:
        raise ActionRunFailed("child failed")
        yield  # pragma: no cover

    parent_token = "work-parent-6"
    client = wm_testing.FakeErClient()
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    params = _params(partial_token=36, progress_token=parent_token)
    with mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _failing,
    ):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            try:
                await er_dispatch._BridgeHandlers()._run_action_in_project(
                    runner,
                    params,
                    er_dispatch.ProjectExecutor(ws_context),
                    elicitation_bridge.RunDispatchOrigin(connection=None),
                )
            except ActionRunFailed as exc:
                assert "child failed" in exc.message
            else:
                raise AssertionError("expected ActionRunFailed")


class _FailingSendClient(wm_testing.FakeErClient):
    """Fails sends for one token while recording everything attempted."""

    def __init__(self, fail_token: int | str, error: Exception) -> None:
        super().__init__()
        self._fail_token = fail_token
        self._error = error

    async def notify_and_drain(
        self, method: str, params: typing.Any | None = None
    ) -> None:
        if (
            isinstance(params, _internal_client_types.ProgressParams)
            and params.token == self._fail_token
        ):
            raise self._error
        self.sent_requests.append((method, params))


@contextlib.asynccontextmanager
async def _live_delegated_call(
    responses: list[typing.Any],
) -> collections.abc.AsyncGenerator[_FakeBridgeCtx, None]:
    """A delegated child run with live (unbounded-cap) real streams."""
    progress_out: completion_streams.BoundedProgressStream[dict] = (
        completion_streams.BoundedProgressStream(
            limit=_LIMIT, protect=delegated_progress.is_owed_progress_value
        )
    )
    partials_out: completion_streams.CompletionStream[dict] = (
        completion_streams.CompletionStream()
    )
    yield _FakeBridgeCtx(partials_out, progress_out, responses)


async def test_progress_send_failure_stops_progress_only(
    tmp_path: pathlib.Path,
) -> None:
    """A progress send failure still returns the reply and every partial."""
    parent_token = "work-progress-down"
    client = _FailingSendClient(parent_token, RuntimeError("progress down"))
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    params = _params(partial_token=37, progress_token=parent_token)
    fence = completion_streams.PrefixFence(
        completion=asyncio.get_running_loop().create_future()
    )

    async with _live_delegated_call([_response()]) as ctx:
        assert ctx.progress is not None
        ctx.progress.append(_report("first"))
        ctx.progress.forward(fence)
        ctx.progress.append(_report("second"))
        ctx.progress.append(_end("done"))
        ctx.progress.end()
        ctx._partials.end()
        async with _live_patched_executor(ctx):
            result = await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                params,
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    assert result.return_code == 0
    # Nothing was delivered on the broken channel, but the stream was still
    # consumed to its end: the fence completed instead of stranding the drain.
    assert _progress_values(client, parent_token) == []
    assert fence.completion.done() and fence.completion.result() is None


@contextlib.asynccontextmanager
async def _live_patched_executor(
    ctx: _FakeBridgeCtx,
) -> collections.abc.AsyncGenerator[None, None]:
    def _fake_run_action_with_partial_results(
        self: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        _ = (self, kwargs)
        return _yield_ctx(ctx)

    @contextlib.asynccontextmanager
    async def _yield_ctx(
        bridge_ctx: _FakeBridgeCtx,
    ) -> collections.abc.AsyncGenerator[_FakeBridgeCtx, None]:
        yield bridge_ctx

    with mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _fake_run_action_with_partial_results,
    ):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            yield


async def test_partial_send_failure_still_fails(tmp_path: pathlib.Path) -> None:
    """A partial send failure fails the call, after progress still drains."""
    parent_token = "work-partial-down"
    client = _FailingSendClient(38, RuntimeError("partial down"))
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    params = _params(partial_token=38, progress_token=parent_token)

    async with _live_delegated_call([_response()]) as ctx:
        assert ctx.progress is not None
        ctx.progress.append(_report("kept"))
        ctx.progress.append(_end("done"))
        ctx.progress.end()
        ctx._partials.append({"json": {"text": "lost"}})
        ctx._partials.end()
        async with _live_patched_executor(ctx):
            try:
                await er_dispatch._BridgeHandlers()._run_action_in_project(
                    runner,
                    params,
                    er_dispatch.ProjectExecutor(ws_context),
                    elicitation_bridge.RunDispatchOrigin(connection=None),
                )
            except RuntimeError as exc:
                assert "partial down" in str(exc)
            else:
                raise AssertionError("expected RuntimeError")
    # Progress kept forwarding while partials failed: the reply's failure
    # does not cancel the sibling pump.
    assert _progress_values(client, parent_token) == [
        _report("kept"),
        _end("done"),
    ]


async def test_cancelled_pump_propagates(tmp_path: pathlib.Path) -> None:
    """Cancelling the call propagates cancellation instead of a reply."""
    parent_token = "work-cancelled"
    client = wm_testing.FakeErClient()
    runner, ws_context = _make_runner_and_context(tmp_path, client)
    params = _params(partial_token=39, progress_token=parent_token)

    async with _live_delegated_call([_response()]) as ctx:
        async with _live_patched_executor(ctx):
            task = asyncio.ensure_future(
                er_dispatch._BridgeHandlers()._run_action_in_project(
                    runner,
                    params,
                    er_dispatch.ProjectExecutor(ws_context),
                    elicitation_bridge.RunDispatchOrigin(connection=None),
                )
            )
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("expected CancelledError")
