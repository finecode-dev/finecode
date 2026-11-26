"""WM-side cancellation of delegated calls: verdict, retention, wire shape.

REQUIREMENT: when a handler's await of a delegated call ends early, the child
run is cancelled; the parent's await then waits for the child's acknowledgement
and reports whether the whole subtree stopped.

Covers the WM-half acceptance rows owned by Phase 3: the cancel/race verdict
(A4, WM side), the stop verdict across fan-outs (A12), the in-flight retention
of a cancelled child (A16), and the never-client-facing rule (A19).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import typing
from unittest import mock

from finecode_jsonrpc.client import ResponseError
from loguru import logger

import finecode_jsonrpc
from finecode.wm_server import testing as wm_testing
from finecode.wm_server._api_handlers import _helpers
from finecode.wm_server.runner import _internal_client_types, runner_client
from finecode.wm_server.services import in_flight_runs
from finecode.wm_server.services.run_service import er_dispatch
from finecode_jsonrpc import JsonRpcHandlerError, error_codes
from finecode_jsonrpc import client as jsonrpc_client

_CANONICAL_SOURCE = "test.actions.TestAction"


def _make_params(
    partial_token: int | str | None = None,
) -> _internal_client_types.RunActionInProjectParams:
    return _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=partial_token,
        run_id="run-1",
    )


def _make_context(
    dir_path: pathlib.Path,
) -> tuple[typing.Any, typing.Any, typing.Any]:
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(
        working_dir_path=dir_path, env_name="test_env", client=client
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return runner, client, ws_context


def _child_response(
    subtree_stopped: bool | None,
) -> _internal_client_types.ErRunActionResponse:
    return _internal_client_types.ErRunActionResponse(
        id=1,
        jsonrpc="2.0",
        result=_internal_client_types.ErRunActionResult(
            result_by_format={"json": {}},
            return_code=0,
            status="success",
            subtree_stopped=subtree_stopped,
        ),
    )


def _run_delegated(
    runner: typing.Any,
    params: _internal_client_types.RunActionInProjectParams,
    ws_context: typing.Any,
) -> typing.Awaitable[_internal_client_types.RunActionInProjectResult]:
    return er_dispatch._BridgeHandlers().run_action_in_project(
        runner=runner, params=params, ws_context=ws_context
    )


async def test_verdict_true_when_child_reports_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """A delegated call whose child stopped reports ``subtreeStopped: true``."""
    runner, client, ws_context = _make_context(tmp_path)
    client.configure_response(_child_response(True))

    result = await _run_delegated(runner, _make_params(), ws_context)

    assert result.subtree_stopped is True


async def test_verdict_false_when_child_reports_unconfirmed(
    tmp_path: pathlib.Path,
) -> None:
    """An explicit ``false`` from the child makes the call unconfirmed."""
    runner, client, ws_context = _make_context(tmp_path)
    client.configure_response(_child_response(False))

    result = await _run_delegated(runner, _make_params(), ws_context)

    assert result.subtree_stopped is False


async def test_verdict_false_when_child_reply_has_no_key(
    tmp_path: pathlib.Path,
) -> None:
    """A missing ``subtreeStopped`` (older child ER) reads as unconfirmed."""
    runner, client, ws_context = _make_context(tmp_path)
    client.configure_response(_child_response(None))

    result = await _run_delegated(runner, _make_params(), ws_context)

    assert result.subtree_stopped is False


def _handlers_response(
    subtree_stopped: bool | None,
) -> _internal_client_types.ErRunHandlersResponse:
    return _internal_client_types.ErRunHandlersResponse(
        id=1,
        jsonrpc="2.0",
        result=_internal_client_types.ErRunHandlersResult(
            result={},
            result_by_format={"json": {}},
            return_code=0,
            status="success",
            subtree_stopped=subtree_stopped,
        ),
    )


async def test_verdict_across_concurrent_envs_needs_every_child_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """A multi-env concurrent call is stopped only when every child stopped."""
    project = wm_testing.make_multi_env_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        handler_envs=["env_a", "env_b"],
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client_a = wm_testing.FakeErClient(readable_id="fake-er-a")
    client_b = wm_testing.FakeErClient(readable_id="fake-er-b")
    runner_a = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="env_a", client=client_a
    )
    runner_b = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="env_b", client=client_b
    )
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=runner_a,
        env_name="env_a",
        extra_runners={"env_b": runner_b},
    )
    parent = wm_testing.make_running_runner(working_dir_path=tmp_path)
    client_a.configure_response(_handlers_response(True))
    client_b.configure_response(_handlers_response(False))

    result = await _run_delegated(parent, _make_params(), ws_context)

    assert result.subtree_stopped is False


async def test_workspace_verdict_needs_every_project_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """A two-project delegated call is stopped only when both projects stopped."""
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    project_a = wm_testing.make_single_action_project(
        dir_path=dir_a, action_name="test_action", action_source=_CANONICAL_SOURCE
    )
    project_a.actions[0].canonical_source = _CANONICAL_SOURCE
    project_b = wm_testing.make_single_action_project(
        dir_path=dir_b, action_name="test_action", action_source=_CANONICAL_SOURCE
    )
    project_b.actions[0].canonical_source = _CANONICAL_SOURCE
    client_a = wm_testing.FakeErClient(readable_id="fake-er-a")
    client_b = wm_testing.FakeErClient(readable_id="fake-er-b")
    runner_a = wm_testing.make_running_runner(
        working_dir_path=dir_a, env_name="test_env", client=client_a
    )
    runner_b = wm_testing.make_running_runner(
        working_dir_path=dir_b, env_name="test_env", client=client_b
    )
    ws_context = wm_testing.make_workspace_context(
        project=project_a, runner=runner_a, extra_runners={}
    )
    ws_context.ws_projects[dir_b] = project_b
    ws_context.ws_projects_extension_runners[dir_b] = {"test_env": runner_b}
    client_a.configure_response(_child_response(True))
    client_b.configure_response(_child_response(False))
    params = _internal_client_types.RunActionInWorkspaceParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        project_paths=[str(dir_a), str(dir_b)],
        run_id="run-1",
    )

    result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=runner_a, params=params, ws_context=ws_context
    )

    assert result.subtree_stopped is False


async def test_error_reply_keeps_code_and_message_with_merged_data(
    tmp_path: pathlib.Path,
) -> None:
    """A failing dispatch answers with its code/message and the stop verdict."""
    runner, client, ws_context = _make_context(tmp_path)
    client.configure_error(wm_testing.make_error_on_request(-32001, "child blew up"))

    try:
        await _run_delegated(runner, _make_params(), ws_context)
    except JsonRpcHandlerError as error:
        assert error.code == error_codes.INTERNAL_ERROR
        assert "child blew up" in error.message
        assert error.data == {"subtreeStopped": False}
    else:
        raise AssertionError("expected the delegated call to fail")


async def test_data_carrying_error_keeps_its_data_with_merged_verdict(
    tmp_path: pathlib.Path,
) -> None:
    """An error that already carries ``data`` keeps it under the verdict."""
    runner, _client, ws_context = _make_context(tmp_path)

    async def _boom(self: typing.Any, **kwargs: typing.Any) -> typing.Any:
        _ = (self, kwargs)
        raise JsonRpcHandlerError(1234, "boom", {"detail": "kept"})

    with mock.patch.object(er_dispatch.ProjectExecutor, "run_action", _boom):
        try:
            await _run_delegated(runner, _make_params(), ws_context)
        except JsonRpcHandlerError as error:
            assert error.code == 1234
            assert error.message == "boom"
            assert error.data == {"detail": "kept", "subtreeStopped": True}
        else:
            raise AssertionError("expected the delegated call to fail")


async def test_cancel_racing_reply_ends_with_reply_verdict(
    tmp_path: pathlib.Path,
) -> None:
    """A cancel that lands as the reply resolves still ends with its verdict.

    The late reply is dropped quietly (DEBUG at most), the serving layer answers
    the call cancelled with the reply's verdict, and no forwarding state, no
    in-flight entry and no handler task is left behind.
    """
    runner, client, ws_context = _make_context(tmp_path)
    response = _child_response(True)
    sent: list[tuple[str, typing.Any]] = []

    async def _send_race(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (timeout, on_response)
        sent.append((method, params))
        if await_reply_after_cancel is None:
            return response
        # The reply has already resolved; the cancellation lands before it is
        # consumed. Mirror the real client: no $/cancelRequest is written and
        # the wait ends with what the reply carried.
        task = asyncio.current_task()
        assert task is not None
        _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0)
        raise jsonrpc_client.RequestCancelledError(
            request_id="race-1",
            reply=jsonrpc_client.ReplyAfterCancel(
                kind=jsonrpc_client.ReplyAfterCancelKind.RESULT,
                result=response,
            ),
        )

    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        with mock.patch.object(client, "send_request", _send_race):
            task = asyncio.ensure_future(
                _run_delegated(runner, _make_params(), ws_context)
            )
            try:
                await task
            except JsonRpcHandlerError as error:
                assert error.code == finecode_jsonrpc.REQUEST_CANCELLED
                assert error.data == {"subtreeStopped": True}
            else:
                raise AssertionError("expected the delegated call to be cancelled")
    finally:
        logger.remove(handler_id)
    assert not any("ERROR" in entry for entry in seen)
    assert [method for method, _ in sent] == [_internal_client_types.ER_RUN_ACTION]
    assert er_dispatch._delegated_calls == {}
    assert in_flight_runs.runs_in_project(ws_context, tmp_path) == []
    assert task.done()


class _DelayedChildClient(wm_testing.FakeErClient):
    """A child-ER client whose reply arrives only when the test releases it.

    On cancellation it mirrors the real client: the cancel is forwarded and
    the wait ends with the reply, raising ``RequestCancelledError``.
    """

    def __init__(self, response: typing.Any) -> None:
        super().__init__()
        self._response = response
        self.reply_ready = asyncio.Event()
        self.request_started = asyncio.Event()

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (timeout, on_response)
        self.sent_requests.append((method, params))
        self.request_started.set()
        if await_reply_after_cancel is None:
            return self._response
        with contextlib.suppress(asyncio.CancelledError):
            await self.reply_ready.wait()
        self.sent_requests.append(
            (
                _internal_client_types.CANCEL_REQUEST,
                _internal_client_types.CancelParams(id="child-1"),
            )
        )
        await self.reply_ready.wait()
        raise jsonrpc_client.RequestCancelledError(
            request_id="child-1",
            reply=jsonrpc_client.ReplyAfterCancel(
                kind=jsonrpc_client.ReplyAfterCancelKind.RESULT,
                result=self._response,
            ),
        )


async def test_cancelled_child_stays_in_flight_until_it_replies(
    tmp_path: pathlib.Path,
) -> None:
    """A cancelled delegated child stays tracked until the child replies.

    While the child has not replied the run is still in-flight (so a recovery
    of its project is refused naming it); once it replies the entry is gone
    and the call ends cancelled with the reply's verdict.
    """
    runner, _client, ws_context = _make_context(tmp_path)
    child = _DelayedChildClient(_child_response(True))
    ws_context.ws_projects_extension_runners[tmp_path] = {
        "test_env": wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="test_env", client=child
        )
    }

    task = asyncio.ensure_future(_run_delegated(runner, _make_params(), ws_context))
    await asyncio.wait_for(child.request_started.wait(), timeout=5.0)
    _ = task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    try:
        runs = in_flight_runs.runs_in_project(ws_context, tmp_path)
        assert [run.action_name for run in runs] == ["test_action"]
        assert "test_action" in in_flight_runs.describe(runs)
        assert any(
            method == _internal_client_types.CANCEL_REQUEST
            for method, _ in child.sent_requests
        )
    finally:
        child.reply_ready.set()
    try:
        await task
    except JsonRpcHandlerError as error:
        assert error.code == finecode_jsonrpc.REQUEST_CANCELLED
        assert error.data == {"subtreeStopped": True}
    else:
        raise AssertionError("expected the delegated call to be cancelled")

    assert in_flight_runs.runs_in_project(ws_context, tmp_path) == []
    assert er_dispatch._delegated_calls == {}
    assert task.done()


async def test_client_facing_results_never_carry_subtree_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """Client results drop ``subtreeStopped`` even when the ER reply had it."""
    runner, client, _ws_context = _make_context(tmp_path)
    client.configure_response(_child_response(True))

    run_response = await runner_client.run_action(
        runner=runner,
        action_name="test_action",
        params={},
        child_stops=None,
    )
    assert "subtreeStopped" not in vars(run_response)

    client.configure_response(_handlers_response(True))
    handlers_response = await runner_client.run_handlers(
        runner=runner,
        action_name="test_action",
        handler_names=["test_handler"],
        child_stops=None,
    )
    assert "subtreeStopped" not in vars(handlers_response)

    batch, _return_code = _helpers._build_batch_result(
        {tmp_path: {"test_action": run_response}},
        {"test_action": "test.actions.TestAction"},
    )
    assert batch == {
        str(tmp_path): {
            "test.actions.TestAction": {
                "resultByFormat": {"json": {}},
                "returnCode": 0,
            }
        }
    }
    assert "subtreeStopped" not in repr(batch)


async def test_child_error_with_data_counts_as_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """A child error reply carrying ``true`` still counts the call stopped."""
    runner, client, ws_context = _make_context(tmp_path)
    client.configure_error(
        finecode_jsonrpc.ErrorOnRequest(
            error=ResponseError(
                code=-32001,
                message="child failed",
                data={"subtreeStopped": True},
            )
        )
    )

    try:
        await _run_delegated(runner, _make_params(), ws_context)
    except JsonRpcHandlerError as error:
        assert error.code == error_codes.INTERNAL_ERROR
        assert "child failed" in error.message
        assert error.data == {"subtreeStopped": True}
    else:
        raise AssertionError("expected the delegated call to fail")


def _serving_client(
    impl: typing.Callable[..., typing.Awaitable[typing.Any]],
    hooked: list[asyncio.Task[typing.Any]],
) -> tuple[jsonrpc_client.JsonRpcClient, list[dict[str, typing.Any]]]:
    """A WM-side client serving the delegated method with production options.

    The registration mirrors ``runner_manager`` (hook, not-started data, the
    connection-loss flag); the serving and cancel machinery under test is the
    real ``JsonRpcClient``.
    """
    client = jsonrpc_client.JsonRpcClient(
        message_types=_internal_client_types.METHOD_TO_TYPES,
        readable_id="wm-delegated-cancel",
    )
    sent: list[dict[str, typing.Any]] = []
    client._send_data = lambda data: sent.append(json.loads(data))  # type: ignore[method-assign]
    client.feature(
        _internal_client_types.RUN_ACTION_IN_PROJECT,
        impl,
        on_peer_cancel=hooked.append,
        not_started_cancel_data={"subtreeStopped": True},
        cancel_on_connection_loss=True,
    )
    return client, sent


def _wire_params() -> dict[str, typing.Any]:
    return {
        "actionSource": _CANONICAL_SOURCE,
        "payload": {},
        "meta": {"trigger": "user", "devEnv": "cli", "orchestrationDepth": 0},
        "runId": "run-1",
    }


async def _finish(
    client: jsonrpc_client.JsonRpcClient,
) -> None:
    await asyncio.gather(*client._async_tasks, return_exceptions=True)


async def test_cancel_before_handler_start_replies_true(
    tmp_path: pathlib.Path,
) -> None:
    """A cancel that lands before the handler runs ends cancelled with ``true``.

    The handler never runs; the reply carries the not-started data the
    registration declares for the delegated methods.
    """
    runner, _client, ws_context = _make_context(tmp_path)
    ran = False

    async def _impl(params: typing.Any) -> typing.Any:
        nonlocal ran
        ran = True
        return await er_dispatch._BridgeHandlers().run_action_in_project(
            runner=runner, params=params, ws_context=ws_context
        )

    hooked: list[asyncio.Task[typing.Any]] = []
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        client, sent = _serving_client(_impl, hooked)
        await client.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": _internal_client_types.RUN_ACTION_IN_PROJECT,
                "params": _wire_params(),
            }
        )
        await client.handle_message(
            {
                "jsonrpc": "2.0",
                "method": _internal_client_types.CANCEL_REQUEST,
                "params": {"id": 1},
            }
        )
        await _finish(client)
    finally:
        logger.remove(handler_id)

    assert not ran
    assert sent[0]["error"]["code"] == finecode_jsonrpc.REQUEST_CANCELLED
    assert sent[0]["error"]["data"] == {"subtreeStopped": True}
    assert not any("ERROR" in entry for entry in seen)
    # A call cancelled before its WM run started leaves no in-flight entry,
    # so a following reload of the project is not refused on its account.
    assert in_flight_runs.runs_in_project(ws_context, tmp_path) == []
    assert er_dispatch._delegated_calls == {}


async def test_cancel_while_joining_runner_start_replies_true_and_start_survives(
    tmp_path: pathlib.Path,
) -> None:
    """Cancelling a joiner of a runner start ends it cancelled, not the start.

    The joiner's reply carries ``true`` (it started no child), and the start
    another caller began still completes: a later call through the same runner
    succeeds.
    """
    runner, _client, ws_context = _make_context(tmp_path)
    child_client = wm_testing.FakeErClient()
    child_client.configure_response(_child_response(True))
    starting = wm_testing.make_initializing_runner(
        working_dir_path=tmp_path, client=child_client
    )
    ws_context.ws_projects_extension_runners[tmp_path] = {"test_env": starting}

    task = asyncio.ensure_future(_run_delegated(runner, _make_params(), ws_context))
    for _ in range(5):
        await asyncio.sleep(0)
    assert in_flight_runs.runs_in_project(ws_context, tmp_path)
    _ = task.cancel()
    try:
        await task
    except JsonRpcHandlerError as error:
        assert error.code == finecode_jsonrpc.REQUEST_CANCELLED
        assert error.data == {"subtreeStopped": True}
    else:
        raise AssertionError("expected the joining call to be cancelled")

    assert starting.status != runner_client.RunnerStatus.RUNNING
    assert not starting.initialized_event.is_set()
    assert task.done()

    starting.status = runner_client.RunnerStatus.RUNNING
    starting.initialized_event.set()
    result = await _run_delegated(runner, _make_params(), ws_context)
    assert result.subtree_stopped is True
    # The cancelled joiner left no in-flight entry behind.
    assert in_flight_runs.runs_in_project(ws_context, tmp_path) == []
    assert er_dispatch._delegated_calls == {}


async def test_connection_loss_cancels_delegated_call_and_waits_for_child(
    tmp_path: pathlib.Path,
) -> None:
    """Losing the parent connection cancels the call but keeps it tracked.

    The WM forwards the cancel to the child, answers the lost parent
    cancelled with the child's verdict once it replies, and logs nothing
    above DEBUG. A second channel failure cancels nothing again.
    """
    runner, _client, ws_context = _make_context(tmp_path)
    child = _DelayedChildClient(_child_response(True))
    ws_context.ws_projects_extension_runners[tmp_path] = {
        "test_env": wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="test_env", client=child
        )
    }

    async def _impl(params: typing.Any) -> typing.Any:
        return await er_dispatch._BridgeHandlers().run_action_in_project(
            runner=runner, params=params, ws_context=ws_context
        )

    hooked: list[asyncio.Task[typing.Any]] = []
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        client, sent = _serving_client(_impl, hooked)
        await client.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": _internal_client_types.RUN_ACTION_IN_PROJECT,
                "params": _wire_params(),
            }
        )
        await asyncio.wait_for(child.request_started.wait(), timeout=5.0)
        client._fail_pending_requests("transport gone")
        client._fail_pending_requests("transport gone")
        assert in_flight_runs.runs_in_project(ws_context, tmp_path)
        child.reply_ready.set()
        await _finish(client)
    finally:
        logger.remove(handler_id)

    assert len(hooked) == 1
    assert any(
        method == _internal_client_types.CANCEL_REQUEST
        for method, _ in child.sent_requests
    )
    replies = [m for m in sent if m.get("id") == 1]
    assert replies and replies[0]["error"]["code"] == (
        finecode_jsonrpc.REQUEST_CANCELLED
    )
    assert replies[0]["error"]["data"] == {"subtreeStopped": True}
    assert seen == []
    assert in_flight_runs.runs_in_project(ws_context, tmp_path) == []
    assert er_dispatch._delegated_calls == {}


async def test_uncancelled_call_runs_to_its_end_without_error_logs(
    tmp_path: pathlib.Path,
) -> None:
    """A parent that never cancels (older ER) sees a normal end, logged clean."""
    runner, client, ws_context = _make_context(tmp_path)
    client.configure_response(_child_response(True))
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        result = await _run_delegated(runner, _make_params(), ws_context)
    finally:
        logger.remove(handler_id)

    assert result.subtree_stopped is True
    assert seen == []
