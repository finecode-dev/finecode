"""Streamed-prefix lifecycle gates (issue #916, tasks 3a-3d)."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import pathlib
import typing
from unittest import mock

from finecode.wm_server import domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    runner_client,
    runner_manager,
)
from finecode.wm_server.runner.stream_completion import StreamChannel
from finecode.wm_server.services.run_service.er_dispatch import _BridgeHandlers
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.project_executor import ProjectExecutor
from finecode.wm_server.utils.stream_completion import PipelineFailure

_CANONICAL_SOURCE = "test.actions.TestAction"


class _GatedClient(wm_testing.FakeErClient):
    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self._gate = gate
        self.drain_entries: list[str] = []

    async def notify_and_drain(
        self, method: str, params: typing.Any | None = None
    ) -> None:
        token = getattr(params, "token", None)
        self.drain_entries.append(str(token))
        await self._gate.wait()
        self.sent_requests.append((method, params))


class _FakeStreamingCtx:
    def __init__(
        self,
        partials: list[dict[str, typing.Any]],
        responses: list[typing.Any],
    ) -> None:
        self._partials = partials
        self.responses = responses
        self.progress: None = None

    def __aiter__(self) -> typing.AsyncIterator[dict[str, typing.Any]]:
        async def _gen() -> typing.AsyncIterator[dict[str, typing.Any]]:
            for item in self._partials:
                yield item

        return _gen()


def _make_params() -> _internal_client_types.RunActionInProjectParams:
    return _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=7,
        progress_token=None,
        run_id="run-1",
    )


async def test_gated_drain_blocks_bridge_completion(
    tmp_path: pathlib.Path,
) -> None:
    """A gated writer drain holds the delegated bridge until it is released."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    gate = asyncio.Event()
    client = _GatedClient(gate)
    runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="test_env", client=client
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    class _Resp:
        status = "streamed"
        result_by_format: typing.ClassVar[dict[str, typing.Any]] = {}
        return_code = 0

    @contextlib.asynccontextmanager
    async def _fake_ctx(
        *args: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_FakeStreamingCtx, None]:
        yield _FakeStreamingCtx([{"json": {"text": "p1"}}], [_Resp()])

    async def _run() -> typing.Any:
        with (
            mock.patch.object(
                ProjectExecutor,
                "run_action_with_partial_results",
                _fake_ctx,
            ),
            mock.patch.object(
                elicitation_bridge,
                "originating_client_for_run",
                return_value=None,
            ),
        ):
            return await _BridgeHandlers()._run_action_in_project(
                runner,
                _make_params(),
                ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    task = asyncio.ensure_future(_run())
    await asyncio.sleep(0.05)
    assert not task.done()
    assert client.drain_entries == ["7"]
    gate.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.return_code == 0
    assert [t for _, n in client.sent_requests for t in [n.token]] == [7]


async def test_gated_fallback_retains_drain_before_return(
    tmp_path: pathlib.Path,
) -> None:
    """The direct-final fallback also waits for writer drain before returning."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    gate = asyncio.Event()
    client = _GatedClient(gate)
    runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="test_env", client=client
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    class _Resp:
        status = "ok"
        result_by_format: typing.ClassVar[dict[str, typing.Any]] = {
            "json": {"text": "final"}
        }
        return_code = 0

    @contextlib.asynccontextmanager
    async def _fake_ctx(
        *args: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_FakeStreamingCtx, None]:
        yield _FakeStreamingCtx([], [_Resp()])

    async def _run() -> typing.Any:
        with (
            mock.patch.object(
                ProjectExecutor,
                "run_action_with_partial_results",
                _fake_ctx,
            ),
            mock.patch.object(
                elicitation_bridge,
                "originating_client_for_run",
                return_value=None,
            ),
        ):
            return await _BridgeHandlers()._run_action_in_project(
                runner,
                _make_params(),
                ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    task = asyncio.ensure_future(_run())
    await asyncio.sleep(0.05)
    assert not task.done()
    gate.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.return_code == 0
    assert len(client.sent_requests) == 1


def _ok_streamed_response() -> runner_client.RunActionResponse:
    return runner_client.RunActionResponse(
        result_by_format={}, return_code=0, status="streamed"
    )


def _two_env_project(
    tmp_path: pathlib.Path,
) -> tuple[typing.Any, dict[str, typing.Any], typing.Any]:
    project = wm_testing.make_multi_env_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        handler_envs=["env_a", "env_b"],
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runner_a = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="env_a"
    )
    runner_b = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="env_b"
    )
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=runner_a,
        env_name="env_a",
        extra_runners={"env_b": runner_b},
    )
    return ws_context, {"env_a": runner_a, "env_b": runner_b}, project


async def test_gated_later_runner_discovery_keeps_output_open(
    tmp_path: pathlib.Path,
) -> None:
    """A blocked later discovery holds the shared stream open for drained items."""
    from finecode.wm_server.services.run_service import proxy_utils

    ws_context, runners, _project = _two_env_project(tmp_path)
    consumer_seen_a = asyncio.Event()

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        if kwargs.get("env_name") == "env_b":
            await asyncio.wait_for(consumer_seen_a.wait(), timeout=5)
        return runners[typing.cast("str", kwargs.get("env_name"))]

    async def _fake_send(
        action_name: str,
        params: dict[str, typing.Any],
        runner: typing.Any,
        options: dict[str, typing.Any] | None = None,
        *,
        child_stops: typing.Any = None,
    ) -> runner_client.RunActionResponse:
        _ = (action_name, params, options)
        runner_manager.admit_runner_progress(
            runner,
            _internal_client_types.ProgressParams(
                token="tok-shared",
                value=json.dumps({"marker": runner.env_name}),
            ),
        )
        return _ok_streamed_response()

    collected: list[typing.Any] = []
    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(proxy_utils, "run_action_in_runner", _fake_send),
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-shared",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for item in ctx:
                collected.append(item)
                if item == {"marker": "env_a"}:
                    consumer_seen_a.set()

    assert {"marker": "env_a"} in collected
    assert {"marker": "env_b"} in collected
    assert len(ctx.responses) == 2


async def test_two_producers_share_output_until_both_finish(
    tmp_path: pathlib.Path,
) -> None:
    """Both producers contribute before the shared stream ends."""
    from finecode.wm_server.services.run_service import proxy_utils

    ws_context, runners, _project = _two_env_project(tmp_path)

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runners[typing.cast("str", kwargs.get("env_name"))]

    sent: list[str] = []

    async def _fake_send(
        action_name: str,
        params: dict[str, typing.Any],
        runner: typing.Any,
        options: dict[str, typing.Any] | None = None,
        *,
        child_stops: typing.Any = None,
    ) -> runner_client.RunActionResponse:
        _ = (action_name, params, options)
        sent.append(runner.env_name)
        runner_manager.admit_runner_progress(
            runner,
            _internal_client_types.ProgressParams(
                token="tok-two", value=json.dumps({"marker": runner.env_name})
            ),
        )
        return _ok_streamed_response()

    collected: list[typing.Any] = []
    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(proxy_utils, "run_action_in_runner", _fake_send),
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-two",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for item in ctx:
                collected.append(item)

    assert sorted(sent) == ["env_a", "env_b"]
    assert {"marker": "env_a"} in collected
    assert {"marker": "env_b"} in collected
    assert len(ctx.responses) == 2


async def test_empty_output_still_delivers_responses(
    tmp_path: pathlib.Path,
) -> None:
    """Producers that publish nothing still end the stream with responses."""
    from finecode.wm_server.services.run_service import proxy_utils

    ws_context, runners, _project = _two_env_project(tmp_path)

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runners[typing.cast("str", kwargs.get("env_name"))]

    async def _fake_send(
        action_name: str,
        params: dict[str, typing.Any],
        runner: typing.Any,
        options: dict[str, typing.Any] | None = None,
        *,
        child_stops: typing.Any = None,
    ) -> runner_client.RunActionResponse:
        _ = (action_name, params, runner, options)
        return _ok_streamed_response()

    collected: list[typing.Any] = []
    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(proxy_utils, "run_action_in_runner", _fake_send),
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-empty",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for item in ctx:
                collected.append(item)

    assert collected == []
    assert len(ctx.responses) == 2


async def test_duplicate_same_env_handlers_dispatch_once(
    tmp_path: pathlib.Path,
) -> None:
    """Two handlers in one env share one producer and one response."""
    from finecode.wm_server.services.run_service import proxy_utils

    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    project.actions[0].handlers.append(
        domain.ActionHandler(
            name="extra_handler",
            source="test.handlers.ExtraHandler",
            config={},
            env="test_env",
            dependencies=[],
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    send_count = 0

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    async def _fake_send(
        action_name: str,
        params: dict[str, typing.Any],
        runner: typing.Any,
        options: dict[str, typing.Any] | None = None,
        *,
        child_stops: typing.Any = None,
    ) -> runner_client.RunActionResponse:
        nonlocal send_count
        _ = (action_name, params, runner, options)
        send_count += 1
        return _ok_streamed_response()

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(proxy_utils, "run_action_in_runner", _fake_send),
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-dup",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for _ in ctx:
                pass

    assert send_count == 1
    assert len(ctx.responses) == 1


async def test_runner_failure_does_not_cancel_sibling(
    tmp_path: pathlib.Path,
) -> None:
    """One env failing still lets the sibling produce before the error surfaces."""
    from finecode.wm_server.services.run_service import proxy_utils
    from finecode.wm_server.services.run_service.exceptions import ActionRunFailed

    ws_context, runners, _project = _two_env_project(tmp_path)

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runners[typing.cast("str", kwargs.get("env_name"))]

    async def _fake_send(
        action_name: str,
        params: dict[str, typing.Any],
        runner: typing.Any,
        options: dict[str, typing.Any] | None = None,
        *,
        child_stops: typing.Any = None,
    ) -> runner_client.RunActionResponse:
        _ = (action_name, params, options)
        if runner.env_name == "env_a":
            raise ActionRunFailed("env_a blew up")
        runner_manager.admit_runner_progress(
            runner,
            _internal_client_types.ProgressParams(
                token="tok-mixed", value=json.dumps({"marker": "env_b"})
            ),
        )
        return _ok_streamed_response()

    collected: list[typing.Any] = []
    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(proxy_utils, "run_action_in_runner", _fake_send),
    ):
        try:
            async with proxy_utils.run_with_partial_results(
                action_name="test_action",
                params={},
                partial_result_token="tok-mixed",
                project_dir_path=tmp_path,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                ws_context=ws_context,
                origin=None,
            ) as ctx:
                async for item in ctx:
                    collected.append(item)
        except ActionRunFailed as exc:
            assert "env_a blew up" in exc.message
        else:
            raise AssertionError("expected ActionRunFailed")

    assert {"marker": "env_b"} in collected


async def test_fallback_drains_inside_owned_context(tmp_path: pathlib.Path) -> None:
    """The direct-final fallback keeps its in-flight registration until drain."""
    from finecode.wm_server.runner import elicitation_bridge
    from finecode.wm_server.services import in_flight_runs
    from finecode.wm_server.services.run_service import proxy_utils

    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    child_runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=child_runner)
    gate = asyncio.Event()
    entered = asyncio.Event()
    in_flight_during_drain: list[int] = []

    parent_client = wm_testing.FakeErClient()

    async def _gated_drain(method: str, params: typing.Any | None = None) -> None:
        in_flight_during_drain.append(
            len(in_flight_runs.runs_in_project(ws_context, tmp_path))
        )
        entered.set()
        await gate.wait()
        parent_client.sent_requests.append((method, params))

    parent_runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="parent_env", client=parent_client
    )

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return child_runner

    async def _fake_send(
        action_name: str,
        params: dict[str, typing.Any],
        runner: typing.Any,
        options: dict[str, typing.Any] | None = None,
        *,
        child_stops: typing.Any = None,
    ) -> runner_client.RunActionResponse:
        _ = (action_name, params, runner, options)
        return runner_client.RunActionResponse(
            result_by_format={"json": {"text": "final"}},
            return_code=0,
            status="ok",
        )

    params = _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=7,
        progress_token=None,
        run_id="run-1",
    )

    async def _run() -> typing.Any:
        with (
            mock.patch.object(
                proxy_utils.runner_start_service,
                "get_or_start_runner_with_auto_prepare",
                _fake_start,
            ),
            mock.patch.object(proxy_utils, "run_action_in_runner", _fake_send),
            mock.patch.object(parent_client, "notify_and_drain", _gated_drain),
            mock.patch.object(
                elicitation_bridge,
                "originating_client_for_run",
                return_value=None,
            ),
        ):
            return await _BridgeHandlers()._run_action_in_project(
                parent_runner,
                params,
                ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    task = asyncio.ensure_future(_run())
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert not task.done()
    assert in_flight_during_drain == [1]
    gate.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.return_code == 0
    assert len(parent_client.sent_requests) == 1
    _method, notif = parent_client.sent_requests[0]
    assert notif.token == 7
    assert json.loads(notif.value) == {"text": "final"}
    assert task.done()


class _FaultScriptClient(wm_testing.FakeErClient):
    """Admits values, optionally waits for consumption, then faults/answers."""

    def __init__(self) -> None:
        super().__init__()
        self.runner: typing.Any = None
        self.admissions: dict[str, list[_internal_client_types.ProgressParams]] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.aborts: dict[str, BaseException] = {}
        self.errors: dict[str, str] = {}

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (timeout, await_reply_after_cancel)
        assert self.runner is not None
        token = (params.options or {}).get("partialResultToken")
        for admission in self.admissions.get(token, []):
            runner_manager.admit_runner_progress(self.runner, admission)
        assert on_response is not None
        on_response.capture()
        gate = self.gates.get(token)
        if gate is not None:
            await gate.wait()
        fault = self.aborts.get(token)
        if fault is not None:
            await asyncio.sleep(0.02)
            subs = self.runner.stream_registry.matching(StreamChannel.PARTIAL, token)
            assert subs
            subs[0].stream.abort(fault)
        error = self.errors.get(token)
        if error is not None:
            return wm_testing.make_run_action_response(
                result_by_format={}, status="streamed", error=error
            )
        return wm_testing.make_run_action_response(
            result_by_format={}, status="streamed"
        )


def _chain_has_pipeline_failure(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, PipelineFailure):
            return True
        current = current.__cause__
    return False


async def test_stream_only_fault_keeps_pipeline_cause(
    tmp_path: pathlib.Path,
) -> None:
    """A writer fault after receipt fails the run without a runner error."""
    from finecode.wm_server.services.run_service import proxy_utils

    client = _FaultScriptClient()
    client.gates["tok-fault"] = asyncio.Event()
    client.admissions["tok-fault"] = [
        _internal_client_types.ProgressParams(
            token="tok-fault", value=json.dumps({"marker": "late"})
        )
    ]
    client.aborts["tok-fault"] = PipelineFailure(RuntimeError("writer down"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    client.runner = runner
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    collected: list[typing.Any] = []
    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        _fake_start,
    ):
        try:
            async with proxy_utils.run_with_partial_results(
                action_name="test_action",
                params={},
                partial_result_token="tok-fault",
                project_dir_path=tmp_path,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                ws_context=ws_context,
                origin=None,
            ) as ctx:
                async for item in ctx:
                    collected.append(item)
                    client.gates["tok-fault"].set()
                    await asyncio.sleep(0.2)
        except ActionRunFailed as exc:
            assert "Stream prefix" in exc.message
            assert _chain_has_pipeline_failure(exc)
        else:
            raise AssertionError("expected ActionRunFailed")

    assert collected == [{"marker": "late"}]
    assert ctx.responses == []


async def test_dual_fault_preserves_runner_error(tmp_path: pathlib.Path) -> None:
    """A drain fault cannot hide the received runner failure at the service."""
    from finecode.wm_server.services.run_service import proxy_utils

    client = _FaultScriptClient()
    client.gates["tok-dual"] = asyncio.Event()
    client.admissions["tok-dual"] = [
        _internal_client_types.ProgressParams(
            token="tok-dual", value=json.dumps({"marker": "late"})
        )
    ]
    client.aborts["tok-dual"] = RuntimeError("writer down")
    client.errors["tok-dual"] = "runner boom"
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    client.runner = runner
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    collected: list[typing.Any] = []
    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        _fake_start,
    ):
        try:
            async with proxy_utils.run_with_partial_results(
                action_name="test_action",
                params={},
                partial_result_token="tok-dual",
                project_dir_path=tmp_path,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                ws_context=ws_context,
                origin=None,
            ) as ctx:
                async for item in ctx:
                    collected.append(item)
                    client.gates["tok-dual"].set()
                    await asyncio.sleep(0.2)
        except ActionRunFailed as exc:
            assert "runner boom" in exc.message
            assert not _chain_has_pipeline_failure(exc)
        else:
            raise AssertionError("expected ActionRunFailed")

    assert collected == [{"marker": "late"}]
    assert ctx.responses == []


async def test_relay_fault_retires_owned_subscriptions(tmp_path: pathlib.Path) -> None:
    """A relay fault fails the run without stranding subscriptions or requests."""
    from finecode.wm_server.services.run_service import proxy_utils

    project = wm_testing.make_multi_env_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        handler_envs=["env_a", "env_b"],
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runners: dict[str, typing.Any] = {}
    for env_name in ("env_a", "env_b"):
        relay_client = _FaultScriptClient()
        relay_client.admissions["tok-relay"] = [
            _internal_client_types.ProgressParams(
                token="tok-relay", value=json.dumps({"marker": env_name})
            )
        ]
        env_runner = wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name=env_name, client=relay_client
        )
        relay_client.runner = env_runner
        runners[env_name] = env_runner
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=runners["env_a"],
        env_name="env_a",
        extra_runners={"env_b": runners["env_b"]},
    )
    real_relay = proxy_utils._relay_subscription
    failed_once = False
    seen_subscriptions: list[typing.Any] = []

    async def _failing_relay(subscription: typing.Any, output: typing.Any) -> None:
        nonlocal failed_once
        seen_subscriptions.append(subscription)
        if not failed_once:
            failed_once = True
            subscription.stream.abort(PipelineFailure(RuntimeError("relay down")))
            raise RuntimeError("relay down")
        await real_relay(subscription, output)

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runners[typing.cast("str", kwargs.get("env_name"))]

    async def _run() -> None:
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-relay",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for _ in ctx:
                pass

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(proxy_utils, "_relay_subscription", _failing_relay),
    ):
        try:
            await asyncio.wait_for(_run(), timeout=10)
        except ActionRunFailed as exc:
            assert "relay down" in exc.message
        else:
            raise AssertionError("expected ActionRunFailed")

    for runner in runners.values():
        assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-relay") == []
        assert runner.active_requests == 0
    faulted = [
        sub
        for sub in seen_subscriptions
        if sub.failure.done() and isinstance(sub.failure.result(), PipelineFailure)
    ]
    assert faulted


async def test_relay_failure_aborts_its_input_stream() -> None:
    """A relay fault settles its input so the invocation cannot hang on it."""
    from finecode.wm_server.runner.stream_completion import RunnerStreamRegistry
    from finecode.wm_server.services.run_service import proxy_utils
    from finecode.wm_server.utils.stream_completion import StreamState

    registry = RunnerStreamRegistry()
    with registry.subscribe_partial("p-relay") as subscription:
        subscription.stream.append({"n": 1})
        output = mock.Mock()
        output.append.side_effect = RuntimeError("sink down")
        try:
            await proxy_utils._relay_subscription(subscription, output)
        except RuntimeError as exc:
            assert "sink down" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
        assert subscription.stream.state is StreamState.ABORTED


async def test_independent_runs_stay_live_during_sibling_pipeline_fault(
    tmp_path: pathlib.Path,
) -> None:
    """A pipeline fault in one run does not disturb a concurrent healthy run."""
    from finecode.wm_server.services.run_service import proxy_utils

    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client = _FaultScriptClient()
    client.admissions["tok-bad"] = [
        _internal_client_types.ProgressParams(
            token="tok-bad", value=json.dumps({"marker": "bad"})
        )
    ]
    client.admissions["tok-good"] = [
        _internal_client_types.ProgressParams(
            token="tok-good", value=json.dumps({"marker": "good"})
        )
    ]
    client.aborts["tok-bad"] = PipelineFailure(RuntimeError("writer down"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    client.runner = runner
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    async def _collect(token: str, out: list[typing.Any]) -> None:
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token=token,
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for item in ctx:
                out.append(item)
                if token == "tok-bad":
                    await asyncio.sleep(0.2)

    bad_out: list[typing.Any] = []
    good_out: list[typing.Any] = []
    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        _fake_start,
    ):
        bad_task = asyncio.ensure_future(_collect("tok-bad", bad_out))
        good_task = asyncio.ensure_future(_collect("tok-good", good_out))
        try:
            await asyncio.wait_for(bad_task, timeout=10)
        except ActionRunFailed as exc:
            assert "Stream prefix" in exc.message
        else:
            raise AssertionError("expected ActionRunFailed")
        await asyncio.wait_for(good_task, timeout=10)

    assert bad_out == [{"marker": "bad"}]
    assert good_out == [{"marker": "good"}]
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-bad") == []
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-good") == []
    assert runner.active_requests == 0


async def test_repeated_cancellation_during_join_still_unwinds(
    tmp_path: pathlib.Path,
) -> None:
    """Two cancels during owned teardown leave no subscription or request behind."""
    from finecode.wm_server.services.run_service import proxy_utils

    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    async def _hanging_send(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout, on_response, await_reply_after_cancel)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    async def _run() -> None:
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-hang",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for _ in ctx:
                pass

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(client, "send_request", _hanging_send),
    ):
        task = asyncio.ensure_future(_run())
        await asyncio.sleep(0.05)
        _ = task.cancel()
        await asyncio.sleep(0.01)
        _ = task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError")

    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-hang") == []
    assert runner.active_requests == 0


async def test_cancelled_finalizer_ends_the_shielded_join(
    tmp_path: pathlib.Path,
) -> None:
    """A finalizer cancelled from outside ends the consumer's join.

    Event-loop teardown cancels every task, the finalizer included. The join
    must then stop shielding it and raise the interruption; re-shielding a
    cancelled task raises at once without yielding, so it would spin forever.
    """
    from finecode.wm_server.services.run_service import proxy_utils

    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    release = asyncio.Event()

    async def _cancel_resistant_send(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        # Keeps the producer, and so the finalizer, pending after the cancel.
        _ = (method, params, timeout, on_response, await_reply_after_cancel)
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()
        raise asyncio.CancelledError()

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    def _is_finalizer(candidate: object) -> bool:
        get_coro = getattr(candidate, "get_coro", None)
        coro = get_coro() if get_coro is not None else None
        return getattr(coro, "__qualname__", "").endswith("._finalize")

    real_shield = asyncio.shield
    finalizer_shields = 0

    class _Spinning(Exception):
        pass

    def _guarded_shield(arg: typing.Any) -> typing.Any:
        nonlocal finalizer_shields
        if _is_finalizer(arg):
            finalizer_shields += 1
            if finalizer_shields > 50:
                raise _Spinning("join keeps re-shielding a finished finalizer")
        return real_shield(arg)

    async def _run() -> None:
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-fin",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:
            async for _ in ctx:
                pass

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(client, "send_request", _cancel_resistant_send),
        mock.patch.object(proxy_utils.asyncio, "shield", _guarded_shield),
    ):
        task = asyncio.ensure_future(_run())
        try:
            await asyncio.sleep(0.05)
            _ = task.cancel()
            await asyncio.sleep(0.05)
            finalizers = [t for t in asyncio.all_tasks() if _is_finalizer(t)]
            assert len(finalizers) == 1, "the join is not waiting on a finalizer"
            _ = finalizers[0].cancel()
            # The cancelled finalizer finishes once the coordinator does.
            release.set()
            done, _pending = await asyncio.wait({task}, timeout=1.0)
            assert task in done, (
                "the join did not end after its finalizer was cancelled"
            )
            assert task.cancelled(), f"expected CancelledError, got {task!r}"
        finally:
            release.set()
            await asyncio.sleep(0.05)


async def test_broken_writer_fails_bridge_and_retires_producer(
    tmp_path: pathlib.Path,
) -> None:
    """A writer fault in a bridge pump surfaces without stranding the producer."""
    import uuid

    from finecode.wm_server.runner import elicitation_bridge
    from finecode.wm_server.services.run_service import proxy_utils

    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    child_runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=child_runner)
    parent_client = wm_testing.FakeErClient()
    parent_runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="parent_env", client=parent_client
    )

    async def _broken_drain(method: str, params: typing.Any | None = None) -> None:
        _ = (method, params)
        raise RuntimeError("writer down")

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return child_runner

    async def _fake_send(
        action_name: str,
        params: dict[str, typing.Any],
        runner: typing.Any,
        options: dict[str, typing.Any] | None = None,
        *,
        child_stops: typing.Any = None,
    ) -> runner_client.RunActionResponse:
        _ = (action_name, params, runner, options)
        return runner_client.RunActionResponse(
            result_by_format={"json": {"text": "final"}},
            return_code=0,
            status="ok",
        )

    params = _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=7,
        progress_token=None,
        run_id="run-1",
    )

    async def _run() -> typing.Any:
        with (
            mock.patch.object(
                proxy_utils.runner_start_service,
                "get_or_start_runner_with_auto_prepare",
                _fake_start,
            ),
            mock.patch.object(proxy_utils, "run_action_in_runner", _fake_send),
            mock.patch.object(parent_client, "notify_and_drain", _broken_drain),
            mock.patch.object(
                elicitation_bridge,
                "originating_client_for_run",
                return_value=None,
            ),
            mock.patch.object(uuid, "uuid4", return_value="child-tok"),
        ):
            return await _BridgeHandlers()._run_action_in_project(
                parent_runner,
                params,
                ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    try:
        await asyncio.wait_for(_run(), timeout=10)
    except RuntimeError as exc:
        assert "writer down" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert (
        child_runner.stream_registry.matching(StreamChannel.PARTIAL, "child-tok") == []
    )
    assert child_runner.active_requests == 0
