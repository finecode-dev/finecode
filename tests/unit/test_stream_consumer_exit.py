"""A consumer that leaves a streamed run must not strand a producer's drain.

Only the final sink acknowledges a prefix fence (ADR-0135). When it stops
reading, a fence inserted by a later runner reply is never acknowledged, so the
run's shared outputs are aborted and the fence settles with that cause.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import typing
import uuid
from unittest import mock

from loguru import logger

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    runner_client,
    runner_manager,
)
from finecode.wm_server.runner.stream_completion import StreamChannel
from finecode.wm_server.services.run_service import proxy_utils
from finecode.wm_server.services.run_service.er_dispatch import _BridgeHandlers
from finecode.wm_server.services.run_service.project_executor import ProjectExecutor
from finecode.wm_server.utils.stream_completion import StreamState
from finecode_jsonrpc import client as jsonrpc_client

_CANONICAL_SOURCE = "test.actions.TestAction"


def _single_runner_workspace(
    tmp_path: pathlib.Path,
) -> tuple[typing.Any, typing.Any, wm_testing.FakeErClient]:
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return ws_context, runner, client


def _streamed_reply() -> _internal_client_types.ErRunActionResponse:
    return _internal_client_types.ErRunActionResponse(
        id=1,
        jsonrpc="2.0",
        result=_internal_client_types.ErRunActionResult(
            return_code=0, result_by_format={}, status="streamed"
        ),
    )


async def test_cancelled_consumer_does_not_strand_reply_after_cancel(
    tmp_path: pathlib.Path,
) -> None:
    """A reply that arrives after the consumer was cancelled settles its fence.

    The producer absorbs its cancel and waits for the runner's reply; that
    reply's hook inserts a fence that no consumer will acknowledge.
    """
    ws_context, runner, client = _single_runner_workspace(tmp_path)

    async def _reply_after_cancel(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: jsonrpc_client.ResponseCompletion | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout, await_reply_after_cancel)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert on_response is not None
            on_response.capture()
            raise jsonrpc_client.RequestCancelledError(1) from None
        raise AssertionError("unreachable")

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    async def _run() -> None:
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-cancel",
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
        mock.patch.object(client, "send_request", _reply_after_cancel),
    ):
        warnings: list[str] = []
        sink_id = logger.add(
            lambda message: warnings.append(message.record["message"]),
            level="WARNING",
        )
        try:
            task = asyncio.ensure_future(_run())
            await asyncio.sleep(0.05)
            _ = task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=2.0)
        finally:
            logger.remove(sink_id)
        if task not in done:
            _ = task.cancel()
            raise AssertionError("the run stalled after its consumer was cancelled")
        assert task.cancelled()

    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-cancel") == []
    # An interrupted prefix has no delivery guarantee: its unsettled fence is
    # expected on a cancel, not a warning.
    assert not [message for message in warnings if "Prefix fault" in message]
    assert runner.active_requests == 0


async def test_fully_consumed_run_ends_its_outputs(tmp_path: pathlib.Path) -> None:
    """A consumer that reads to the end leaves the outputs ended, not aborted."""
    ws_context, runner, client = _single_runner_workspace(tmp_path)

    async def _reply(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: jsonrpc_client.ResponseCompletion | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout, await_reply_after_cancel)
        runner_manager.admit_runner_progress(
            runner,
            _internal_client_types.ProgressParams(
                token="tok-full", value=json.dumps({"json": {"n": 1}})
            ),
        )
        runner_manager.admit_runner_progress(
            runner,
            _internal_client_types.ProgressParams(
                token="tok-full-progress", value=json.dumps({"type": "begin"})
            ),
        )
        assert on_response is not None
        on_response.capture()
        return _streamed_reply()

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return runner

    collected: list[typing.Any] = []
    progress: list[typing.Any] = []
    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_start,
        ),
        mock.patch.object(client, "send_request", _reply),
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok-full",
            progress_token="tok-full-progress",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
        ) as ctx:

            async def _read_progress() -> None:
                assert ctx.progress is not None
                progress.extend([value async for value in ctx.progress])

            progress_reader = asyncio.ensure_future(_read_progress())
            collected.extend([value async for value in ctx])
            await asyncio.wait_for(progress_reader, timeout=2.0)

    assert collected == [{"json": {"n": 1}}]
    assert progress == [{"type": "begin"}]
    assert ctx.partials.state is StreamState.ENDED
    assert ctx.progress is not None
    assert ctx.progress.state is StreamState.ENDED
    assert len(ctx.responses) == 1


async def test_failed_partial_pump_does_not_strand_delegated_run(
    tmp_path: pathlib.Path,
) -> None:
    """A partial-send failure fails the delegated call instead of stalling it.

    The progress pump keeps the handler inside the run context, so the stream
    the failed partial pump stopped reading must not hold the child's drain.
    """
    ws_context, child_runner, child_client = _single_runner_workspace(tmp_path)
    parent_client = wm_testing.FakeErClient()
    parent_runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="parent_env", client=parent_client
    )
    partial_send_failed = asyncio.Event()

    async def _parent_drain(method: str, params: typing.Any | None = None) -> None:
        _ = method
        if getattr(params, "token", None) == 7:
            partial_send_failed.set()
            raise RuntimeError("partial send failed")

    async def _child_reply_after_failure(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: jsonrpc_client.ResponseCompletion | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout, await_reply_after_cancel)
        runner_manager.admit_runner_progress(
            child_runner,
            _internal_client_types.ProgressParams(
                token="child-part", value=json.dumps({"json": {"n": 1}})
            ),
        )
        runner_manager.admit_runner_progress(
            child_runner,
            _internal_client_types.ProgressParams(
                token="child-prog", value=json.dumps({"type": "begin"})
            ),
        )
        await partial_send_failed.wait()
        assert on_response is not None
        on_response.capture()
        return _streamed_reply()

    async def _fake_start(**kwargs: typing.Any) -> typing.Any:
        return child_runner

    child_tokens = iter(["child-part", "child-prog"])
    real_uuid4 = uuid.uuid4

    def _uuid4() -> typing.Any:
        return next(child_tokens, None) or real_uuid4()

    params = _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=7,
        progress_token=8,
        run_id="run-1",
    )

    async def _run() -> typing.Any:
        with (
            mock.patch.object(
                proxy_utils.runner_start_service,
                "get_or_start_runner_with_auto_prepare",
                _fake_start,
            ),
            mock.patch.object(child_client, "send_request", _child_reply_after_failure),
            mock.patch.object(parent_client, "notify_and_drain", _parent_drain),
            mock.patch.object(
                elicitation_bridge,
                "originating_client_for_run",
                return_value=None,
            ),
            mock.patch.object(uuid, "uuid4", _uuid4),
        ):
            return await _BridgeHandlers()._run_action_in_project(
                parent_runner,
                params,
                ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    task = asyncio.ensure_future(_run())
    done, _pending = await asyncio.wait({task}, timeout=2.0)
    if task not in done:
        _ = task.cancel()
        raise AssertionError("the delegated run stalled after a partial-send failure")
    error = task.exception()
    assert isinstance(error, RuntimeError), repr(error)
    assert "partial send failed" in str(error)
    assert (
        child_runner.stream_registry.matching(StreamChannel.PARTIAL, "child-part") == []
    )
    assert child_runner.active_requests == 0
