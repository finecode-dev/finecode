"""Early release of a delegated call (D8): forwarding stops, the child runs on.

Releasing a registered call replies ``{}`` after its forwarding pumps are
stopped: no further ``$/progress`` and no direct-final fallback reach the
parent, while the child run continues and its own output drains to a
discarding sink until it ends. Releasing anything unregistered is a no-op.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import pathlib
import typing
from unittest import mock

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, elicitation_bridge
from finecode.wm_server.services.run_service import delegated_progress, er_dispatch
from finecode.wm_server.utils import stream_completion as completion_streams

_CANONICAL_SOURCE = "test.actions.TestAction"
_LIMIT = delegated_progress.DELEGATED_PROGRESS_PENDING_LIMIT


class _FakeCtx:
    def __init__(self, return_code: int = 0) -> None:
        self.partials: completion_streams.CompletionStream[dict] = (
            completion_streams.CompletionStream()
        )
        self.progress: completion_streams.BoundedProgressStream[dict] = (
            completion_streams.BoundedProgressStream(
                limit=_LIMIT, protect=delegated_progress.is_owed_progress_value
            )
        )
        self.responses = [_response(return_code)]

    def __aiter__(self) -> typing.AsyncIterator[dict]:
        return self.partials.__aiter__()


def _response(return_code: int = 0) -> typing.Any:
    class _Resp:
        def __init__(self) -> None:
            self.status = "streamed"
            self.result_by_format: dict[str, dict] = {}
            self.return_code = return_code

    return _Resp()


def _params(
    partial_token: int | str, progress_token: int | str | None = "work-parent"
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


def _release_params(
    partial_token: int | str,
) -> _internal_client_types.ReleaseDelegatedCallParams:
    return _internal_client_types.ReleaseDelegatedCallParams(
        partial_result_token=partial_token
    )


def _make_runner_and_context(
    dir_path: pathlib.Path, client: wm_testing.FakeErClient | None = None
) -> tuple[typing.Any, typing.Any, typing.Any]:
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client = client or wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(
        working_dir_path=dir_path, env_name="test_env", client=client
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return runner, client, ws_context


@contextlib.asynccontextmanager
async def _patched_executor(
    ctx: _FakeCtx, gate: asyncio.Event | None = None
) -> collections.abc.AsyncGenerator[None, None]:
    def _fake(self: typing.Any, **kwargs: typing.Any) -> typing.Any:
        _ = (self, kwargs)
        return _cm()

    @contextlib.asynccontextmanager
    async def _cm() -> collections.abc.AsyncGenerator[_FakeCtx, None]:
        if gate is not None:
            await gate.wait()
        yield ctx

    with mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _fake,
    ):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            yield


def _origin() -> elicitation_bridge.RunDispatchOrigin:
    return elicitation_bridge.RunDispatchOrigin(connection=None)


def _parent_notifications(
    client: wm_testing.FakeErClient,
) -> list[tuple[typing.Any, dict]]:
    return [
        (notif.token, json.loads(notif.value))
        for _, notif in client.sent_requests
        if isinstance(notif, _internal_client_types.ProgressParams)
    ]


async def _wait_for_entry(
    runner: typing.Any, token: int | str, present: bool = True
) -> None:
    key = er_dispatch._delegated_call_key(runner, token)
    for _ in range(1000):
        if (key in er_dispatch._delegated_calls) == present:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"entry presence != {present}")


async def test_release_stops_forwarding_child_runs_on(tmp_path: pathlib.Path) -> None:
    """A release replies {} and the child still runs to its own return code."""
    runner, client, ws_context = _make_runner_and_context(tmp_path)
    control_runner, _, control_ws = _make_runner_and_context(tmp_path)
    ctx = _FakeCtx(return_code=0)
    control_ctx = _FakeCtx(return_code=0)
    params = _params(partial_token=41)
    handlers = er_dispatch._BridgeHandlers()

    async with _patched_executor(control_ctx):
        control_ctx.partials.append({"json": {"text": "c"}})
        control_ctx.partials.end()
        control_ctx.progress.end()
        control_result = await handlers._run_action_in_project(
            control_runner,
            params,
            er_dispatch.ProjectExecutor(control_ws),
            _origin(),
        )
    assert control_result.return_code == 0

    async with _patched_executor(ctx):
        ctx.partials.append({"json": {"text": "p1"}})
        ctx.progress.append({"type": "report", "message": "before"})
        task = asyncio.ensure_future(
            handlers._run_action_in_project(
                runner, params, er_dispatch.ProjectExecutor(ws_context), _origin()
            )
        )
        await _wait_for_entry(runner, 41, present=True)
        for _ in range(1000):
            if _parent_notifications(client):
                break
            await asyncio.sleep(0.005)
        sent_before = list(client.sent_requests)
        assert sent_before, "expected the pre-release notification to arrive (G4)"
        release = await handlers.release_delegated_call(
            runner, _release_params(41), ws_context
        )
        assert release == _internal_client_types.ReleaseDelegatedCallResult()
        await _wait_for_entry(runner, 41, present=False)
        # The child keeps producing after the release; none of it is forwarded.
        ctx.progress.append({"type": "report", "message": "after"})
        ctx.partials.append({"json": {"text": "p2"}})
        ctx.progress.end()
        ctx.partials.end()
        result = await asyncio.wait_for(task, timeout=10.0)

    assert result.return_code == control_result.return_code == 0
    assert sorted(
        json.dumps(value) for _, value in _parent_notifications(client)
    ) == sorted(
        [
            json.dumps({"text": "p1"}),
            json.dumps({"type": "report", "message": "before"}),
        ]
    )
    assert len(client.sent_requests) == len(sent_before)
    assert er_dispatch._delegated_calls == {}


async def test_release_of_unknown_or_finished_token_is_noop(
    tmp_path: pathlib.Path,
) -> None:
    """Releasing an unregistered token replies {} with no effect (G5/G6)."""
    runner, client, ws_context = _make_runner_and_context(tmp_path)
    other_client = wm_testing.FakeErClient()
    other_runner, _, _ = _make_runner_and_context(tmp_path, other_client)
    handlers = er_dispatch._BridgeHandlers()

    assert (
        await handlers.release_delegated_call(
            runner, _release_params("never-received"), ws_context
        )
        == _internal_client_types.ReleaseDelegatedCallResult()
    )
    assert client.sent_requests == []

    ctx = _FakeCtx()
    params = _params(partial_token=42)
    async with _patched_executor(ctx):
        ctx.partials.end()
        ctx.progress.end()
        result = await handlers._run_action_in_project(
            runner, params, er_dispatch.ProjectExecutor(ws_context), _origin()
        )
    assert result.return_code == 0
    # Finished: the entry is gone, so a late release is a no-op.
    assert (
        await handlers.release_delegated_call(runner, _release_params(42), ws_context)
        == _internal_client_types.ReleaseDelegatedCallResult()
    )
    # Another connection's value never matches this connection's entries.
    assert (
        await handlers.release_delegated_call(
            other_runner, _release_params(42), ws_context
        )
        == _internal_client_types.ReleaseDelegatedCallResult()
    )
    assert other_client.sent_requests == []
    assert er_dispatch._delegated_calls == {}


@contextlib.asynccontextmanager
async def _patched_executor_factory(
    created: list[_FakeCtx],
) -> collections.abc.AsyncGenerator[None, None]:
    """A fake executor minting a fresh child context per delegated call."""

    def _fake(self: typing.Any, **kwargs: typing.Any) -> typing.Any:
        _ = (self, kwargs)
        ctx = _FakeCtx()
        created.append(ctx)
        return _cm(ctx)

    @contextlib.asynccontextmanager
    async def _cm(
        ctx: _FakeCtx,
    ) -> collections.abc.AsyncGenerator[_FakeCtx, None]:
        yield ctx

    with mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _fake,
    ):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            yield


async def test_two_calls_one_released(tmp_path: pathlib.Path) -> None:
    """Two calls sharing a parent token value release independently (G7)."""
    runner_a, client_a, ws_context = _make_runner_and_context(tmp_path)
    runner_b, client_b, _ = _make_runner_and_context(tmp_path)
    handlers = er_dispatch._BridgeHandlers()
    created: list[_FakeCtx] = []

    async def _run(
        runner: typing.Any, ws: typing.Any
    ) -> _internal_client_types.RunActionInProjectResult:
        return await handlers._run_action_in_project(
            runner,
            _params(43),
            er_dispatch.ProjectExecutor(ws),
            _origin(),
        )

    async with _patched_executor_factory(created):
        task_a = asyncio.ensure_future(_run(runner_a, ws_context))
        for _ in range(1000):
            if len(created) >= 1:
                break
            await asyncio.sleep(0.005)
        ctx_a = created[0]
        task_b = asyncio.ensure_future(_run(runner_b, ws_context))
        for _ in range(1000):
            if len(created) >= 2:
                break
            await asyncio.sleep(0.005)
        ctx_b = created[1]
        ctx_a.progress.append({"type": "report", "message": "a1"})
        ctx_b.progress.append({"type": "report", "message": "b1"})
        for _ in range(1000):
            if _parent_notifications(client_a) and _parent_notifications(client_b):
                break
            await asyncio.sleep(0.005)
        assert (
            await handlers.release_delegated_call(
                runner_a, _release_params(43), ws_context
            )
            == _internal_client_types.ReleaseDelegatedCallResult()
        )
        ctx_a.progress.append({"type": "report", "message": "a2"})
        ctx_b.progress.append({"type": "report", "message": "b2"})
        ctx_a.progress.end()
        ctx_a.partials.end()
        ctx_b.progress.end()
        ctx_b.partials.end()
        result_a, result_b = await asyncio.wait_for(
            asyncio.gather(task_a, task_b), timeout=10.0
        )

    assert result_a.return_code == result_b.return_code == 0
    assert [value for _, value in _parent_notifications(client_a)] == [
        {"type": "report", "message": "a1"}
    ]
    assert [value for _, value in _parent_notifications(client_b)] == [
        {"type": "report", "message": "b1"},
        {"type": "report", "message": "b2"},
    ]
    assert er_dispatch._delegated_calls == {}


async def test_release_before_dispatch_never_forwards(tmp_path: pathlib.Path) -> None:
    """A release gated on dispatch still runs the child with no forwarding (G8)."""
    runner, client, ws_context = _make_runner_and_context(tmp_path)
    handlers = er_dispatch._BridgeHandlers()
    ctx = _FakeCtx()
    gate = asyncio.Event()
    params = _params(partial_token=44)

    async with _patched_executor(ctx, gate=gate):
        task = asyncio.ensure_future(
            handlers._run_action_in_project(
                runner, params, er_dispatch.ProjectExecutor(ws_context), _origin()
            )
        )
        await _wait_for_entry(runner, 44, present=True)
        assert (
            await handlers.release_delegated_call(
                runner, _release_params(44), ws_context
            )
            == _internal_client_types.ReleaseDelegatedCallResult()
        )
        gate.set()
        ctx.progress.append({"type": "report", "message": "late"})
        ctx.progress.end()
        ctx.partials.end()
        result = await asyncio.wait_for(task, timeout=10.0)

    assert result.return_code == 0
    assert client.sent_requests == []
    assert er_dispatch._delegated_calls == {}
