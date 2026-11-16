"""Streaming WAL events: accepted/selected/dispatched/completed/failed."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
import typing
from unittest import mock

import finecode.wm_server
from finecode.wm_server import domain, wal
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services.run_service import proxy_utils
from finecode.wm_server.services.run_service.exceptions import (
    ActionCancelledError,
    ActionRunFailed,
)

_CANONICAL_SOURCE = "test.actions.TestAction"


def _make_project(tmp_path: pathlib.Path):
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return project, runner, ws_context


def _emit_calls(mock_emit: mock.Mock) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    for call in mock_emit.call_args_list:
        _, kwargs = call
        calls.append((kwargs["event_type"], kwargs))
    return calls


async def test_notify_emits_dispatched_then_completed() -> None:
    """One env produces exactly one dispatched and one completed event."""
    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))
    writer = object()
    with (
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            return_value=runner_client.RunActionResponse(
                result_by_format={}, return_code=0, status="ok"
            ),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        await proxy_utils.run_action_and_notify(
            action_name="test_action",
            params={"a": 1},
            partial_result_token="tok",
            runner=runner,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            wal_run_id="wal-1",
            result_formats=None,
            progress_token=None,
            caller_kwargs=None,
            orchestration_depth=2,
            wal_writer=typing.cast(typing.Any, writer),
        )
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.dispatched", "run.completed"]
    dispatched = events[0][1]
    completed = events[1][1]
    assert dispatched["wal_run_id"] == "wal-1"
    assert completed["wal_run_id"] == "wal-1"
    assert dispatched["payload"].env_name == runner.env_name
    assert dispatched["payload"].runner_id == runner.readable_id
    assert completed["payload"].return_code == 0


async def test_notify_nonzero_return_is_still_completed() -> None:
    """A nonzero return code is a completed run, not a failure."""
    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))
    with (
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            return_value=runner_client.RunActionResponse(
                result_by_format={}, return_code=2, status="ok"
            ),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        await proxy_utils.run_action_and_notify(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            runner=runner,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            wal_run_id="wal-2",
            result_formats=None,
            progress_token=None,
            caller_kwargs=None,
            orchestration_depth=0,
            wal_writer=object(),
        )
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.dispatched", "run.completed"]
    assert events[1][1]["payload"].return_code == 2


async def test_notify_disabled_writer_does_not_fail() -> None:
    """Passing no writer skips WAL without changing the result."""
    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))
    with mock.patch.object(
        proxy_utils,
        "run_action_in_runner",
        return_value=runner_client.RunActionResponse(
            result_by_format={"json": {}}, return_code=0, status="ok"
        ),
    ):
        response = await proxy_utils.run_action_and_notify(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            runner=runner,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            wal_run_id="wal-3",
            result_formats=None,
            progress_token=None,
            caller_kwargs=None,
            orchestration_depth=0,
            wal_writer=None,
        )
    assert response.return_code == 0


async def test_notify_service_failure_emits_failed_once() -> None:
    """A backend failure emits one failed event and no completed event."""
    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))
    with (
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            side_effect=ActionRunFailed("backend boom"),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        try:
            await proxy_utils.run_action_and_notify(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                runner=runner,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                wal_run_id="wal-4",
                result_formats=None,
                progress_token=None,
                caller_kwargs=None,
                orchestration_depth=0,
                wal_writer=object(),
            )
        except ActionRunFailed:
            pass
        else:
            raise AssertionError("expected ActionRunFailed")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.dispatched", "run.failed"]
    assert events[1][1]["payload"].error == "backend boom"


async def test_notify_runner_cancellation_emits_failed() -> None:
    """A runner-reported cancellation records one terminal failure."""
    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))
    with (
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            side_effect=ActionCancelledError("stop"),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        try:
            await proxy_utils.run_action_and_notify(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                runner=runner,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                wal_run_id="wal-5",
                result_formats=None,
                progress_token=None,
                caller_kwargs=None,
                orchestration_depth=0,
                wal_writer=object(),
            )
        except ActionCancelledError:
            pass
        else:
            raise AssertionError("expected ActionCancelledError")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.dispatched", "run.failed"]
    assert events[1][1]["payload"].error.startswith("cancelled: ")


async def test_notify_task_cancellation_emits_no_terminal() -> None:
    """Task cancellation after dispatch writes no terminal event, matching the plain path."""
    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))

    async def _cancelled(*args: typing.Any, **kwargs: typing.Any):
        raise asyncio.CancelledError

    with (
        mock.patch.object(proxy_utils, "run_action_in_runner", _cancelled),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        try:
            await proxy_utils.run_action_and_notify(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                runner=runner,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                wal_run_id="wal-6",
                result_formats=None,
                progress_token=None,
                caller_kwargs=None,
                orchestration_depth=0,
                wal_writer=object(),
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.dispatched"]


async def test_notify_unexpected_exception_emits_no_terminal() -> None:
    """An unexpected runner error writes no terminal event, matching the plain path."""
    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))
    with (
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            side_effect=RuntimeError("backend exploded"),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        try:
            await proxy_utils.run_action_and_notify(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                runner=runner,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                wal_run_id="wal-7",
                result_formats=None,
                progress_token=None,
                caller_kwargs=None,
                orchestration_depth=0,
                wal_writer=object(),
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.dispatched"]


async def test_streaming_emits_accepted_and_selected(
    tmp_path: pathlib.Path,
) -> None:
    """The streaming owner emits accepted once and selected per env."""
    _project, _runner, ws_context = _make_project(tmp_path)
    fake_runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    sentinel = object()

    async def _fake_get_or_start(**kwargs: typing.Any):
        return fake_runner

    async def _fake_notify(**kwargs: typing.Any):
        return runner_client.RunActionResponse(
            result_by_format={}, return_code=0, status="ok"
        )

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_get_or_start,
        ),
        mock.patch.object(proxy_utils, "run_action_and_notify", _fake_notify),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={"x": 1},
            partial_result_token="tok",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
            wal_writer=typing.cast(typing.Any, sentinel),
        ) as ctx:
            async for _ in ctx:
                pass
    events = _emit_calls(mock_emit)
    kinds = [e for e, _ in events]
    assert kinds[0] == "run.accepted"
    assert kinds[1] == "runner.selected"
    assert all(call.args[0] is sentinel for call in mock_emit.call_args_list)
    accepted = events[0][1]
    assert accepted["payload"].params_hash == wal.params_hash({"x": 1})
    assert accepted["action_name"] == "test_action"
    run_ids = {kwargs["wal_run_id"] for _, kwargs in events}
    assert len(run_ids) == 1


async def test_streaming_startup_failure_emits_no_terminal(
    tmp_path: pathlib.Path,
) -> None:
    """A runner that cannot start records selection but no terminal event."""
    _project, _runner, ws_context = _make_project(tmp_path)

    async def _failing_start(**kwargs: typing.Any):
        from finecode.wm_server.runner import runner_manager

        raise runner_manager.RunnerFailedToStart("no env")

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _failing_start,
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
        mock.patch.object(proxy_utils, "run_action_and_notify") as mock_notify,
    ):
        try:
            async with proxy_utils.run_with_partial_results(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                project_dir_path=tmp_path,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                ws_context=ws_context,
                origin=None,
                wal_writer=typing.cast(typing.Any, object()),
            ) as ctx:
                async for _ in ctx:
                    pass
        except ActionRunFailed:
            pass
        else:
            raise AssertionError("expected ActionRunFailed")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.accepted", "runner.selected"]
    assert mock_notify.call_count == 0


async def test_streaming_success_sequence(tmp_path: pathlib.Path) -> None:
    """A clean streaming run records accepted, selected, dispatched and completed."""
    _project, _runner, ws_context = _make_project(tmp_path)
    fake_runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    sentinel: typing.Any = object()

    async def _fake_get_or_start(
        **kwargs: typing.Any,
    ) -> runner_client.ExtensionRunnerInfo:
        return fake_runner

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_get_or_start,
        ),
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            return_value=runner_client.RunActionResponse(
                result_by_format={}, return_code=0, status="ok"
            ),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
            wal_writer=typing.cast(typing.Any, sentinel),
        ) as ctx:
            async for _ in ctx:
                pass
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == [
        "run.accepted",
        "runner.selected",
        "run.dispatched",
        "run.completed",
    ]
    assert all(call.args[0] is sentinel for call in mock_emit.call_args_list)


async def test_streaming_runner_failure_sequence(tmp_path: pathlib.Path) -> None:
    """A runner-reported failure after dispatch records failed and surfaces."""
    _project, _runner, ws_context = _make_project(tmp_path)
    fake_runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    sentinel: typing.Any = object()

    async def _fake_get_or_start(
        **kwargs: typing.Any,
    ) -> runner_client.ExtensionRunnerInfo:
        return fake_runner

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_get_or_start,
        ),
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            side_effect=ActionRunFailed("backend boom"),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        try:
            async with proxy_utils.run_with_partial_results(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                project_dir_path=tmp_path,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                ws_context=ws_context,
                origin=None,
                wal_writer=typing.cast(typing.Any, sentinel),
            ) as ctx:
                async for _ in ctx:
                    pass
        except ActionRunFailed as exc:
            assert "backend boom" in exc.message
        else:
            raise AssertionError("expected ActionRunFailed")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == [
        "run.accepted",
        "runner.selected",
        "run.dispatched",
        "run.failed",
    ]
    assert events[3][1]["payload"].error == "backend boom"


async def test_streaming_accepted_precedes_in_flight_track(
    tmp_path: pathlib.Path,
) -> None:
    """Acceptance is recorded before the run starts refusing recovery."""
    _project, _runner, ws_context = _make_project(tmp_path)
    fake_runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def _fake_track(
        *args: typing.Any, **kwargs: typing.Any
    ) -> typing.AsyncGenerator[None, None]:
        order.append("track")
        yield None

    async def _fake_get_or_start(
        **kwargs: typing.Any,
    ) -> runner_client.ExtensionRunnerInfo:
        return fake_runner

    def _record(*args: typing.Any, **kwargs: typing.Any) -> None:
        order.append(str(kwargs["event_type"]))

    with (
        mock.patch.object(proxy_utils.in_flight_runs, "track", _fake_track),
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_get_or_start,
        ),
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            return_value=runner_client.RunActionResponse(
                result_by_format={}, return_code=0, status="ok"
            ),
        ),
        mock.patch.object(
            proxy_utils.wal,
            "emit_run_event",
            side_effect=_record,
        ),
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
            wal_writer=typing.cast(typing.Any, object()),
        ) as ctx:
            async for _ in ctx:
                pass
    assert order[:2] == ["run.accepted", "track"]


async def test_streaming_cancel_at_track_leaves_only_accepted(
    tmp_path: pathlib.Path,
) -> None:
    """Cancellation while registering the run leaves only the acceptance record."""
    _project, _runner, ws_context = _make_project(tmp_path)

    @contextlib.asynccontextmanager
    async def _cancelling_track(
        *args: typing.Any, **kwargs: typing.Any
    ) -> typing.AsyncGenerator[None, None]:
        raise asyncio.CancelledError("stop")
        yield None

    with (
        mock.patch.object(proxy_utils.in_flight_runs, "track", _cancelling_track),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        try:
            async with proxy_utils.run_with_partial_results(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                project_dir_path=tmp_path,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                ws_context=ws_context,
                origin=None,
                wal_writer=typing.cast(typing.Any, object()),
            ):
                pass
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.accepted"]


async def test_streaming_cancel_during_runner_start_has_no_terminal(
    tmp_path: pathlib.Path,
) -> None:
    """Cancellation while starting the runner writes no terminal event."""
    _project, _runner, ws_context = _make_project(tmp_path)

    async def _cancel_start(
        **kwargs: typing.Any,
    ) -> runner_client.ExtensionRunnerInfo:
        raise asyncio.CancelledError("stop")

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _cancel_start,
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        try:
            async with proxy_utils.run_with_partial_results(
                action_name="test_action",
                params={},
                partial_result_token="tok",
                project_dir_path=tmp_path,
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                ws_context=ws_context,
                origin=None,
                wal_writer=typing.cast(typing.Any, object()),
            ) as ctx:
                async for _ in ctx:
                    pass
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError")
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.accepted", "runner.selected"]


async def test_streaming_no_handlers_emits_only_accepted(
    tmp_path: pathlib.Path,
) -> None:
    """An action with no handlers records acceptance and warns, then ends."""
    project, _runner, ws_context = _make_project(tmp_path)
    project.actions[0].handlers = []
    with (
        mock.patch.object(proxy_utils.user_messages, "warning", new=mock.AsyncMock()),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
            wal_writer=typing.cast(typing.Any, object()),
        ):
            pass
    events = _emit_calls(mock_emit)
    assert [e for e, _ in events] == ["run.accepted"]


async def test_streaming_multi_env_shares_one_run_id(
    tmp_path: pathlib.Path,
) -> None:
    """Sibling env dispatches group under one run id with per-env terminals."""
    project, _runner, ws_context = _make_project(tmp_path)
    project.actions[0].handlers.append(
        domain.ActionHandler(
            name="h2",
            source="test.handlers.H2",
            config={},
            env="env_b",
            dependencies=[],
        )
    )

    async def _fake_get_or_start(
        **kwargs: typing.Any,
    ) -> runner_client.ExtensionRunnerInfo:
        return wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name=kwargs["env_name"]
        )

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_get_or_start,
        ),
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            return_value=runner_client.RunActionResponse(
                result_by_format={}, return_code=0, status="ok"
            ),
        ),
        mock.patch.object(proxy_utils.wal, "emit_run_event") as mock_emit,
    ):
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
            wal_writer=typing.cast(typing.Any, object()),
        ) as ctx:
            async for _ in ctx:
                pass
    events = _emit_calls(mock_emit)
    kinds = sorted(e for e, _ in events)
    assert kinds == [
        "run.accepted",
        "run.completed",
        "run.completed",
        "run.dispatched",
        "run.dispatched",
        "runner.selected",
        "runner.selected",
    ]
    run_ids = {kwargs["wal_run_id"] for _, kwargs in events}
    assert len(run_ids) == 1
    dispatched_envs = {
        kwargs["payload"].env_name for e, kwargs in events if e == "run.dispatched"
    }
    assert dispatched_envs == {"test_env", "env_b"}


async def test_streaming_without_writer_emits_nothing_even_if_workspace_wal_enabled(
    tmp_path: pathlib.Path,
) -> None:
    """Only the delegated path records streaming events when the WAL is on."""
    _project, _runner, ws_context = _make_project(tmp_path)
    writer = mock.Mock(spec=wal.WalWriter)
    ws_context.wal_writer = writer
    fake_runner = wm_testing.make_running_runner(working_dir_path=tmp_path)

    async def _fake_get_or_start(
        **kwargs: typing.Any,
    ) -> runner_client.ExtensionRunnerInfo:
        return fake_runner

    async def _run_helper(wal_writer: wal.WalWriter | None) -> None:
        async with proxy_utils.run_with_partial_results(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            project_dir_path=tmp_path,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            ws_context=ws_context,
            origin=None,
            wal_writer=wal_writer,
        ) as ctx:
            async for _ in ctx:
                pass

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            _fake_get_or_start,
        ),
        mock.patch.object(
            proxy_utils,
            "run_action_in_runner",
            return_value=runner_client.RunActionResponse(
                result_by_format={}, return_code=0, status="ok"
            ),
        ),
    ):
        await _run_helper(None)
        assert writer.append.call_count == 0
        await _run_helper(writer)
        assert writer.append.call_count == 4


def test_only_delegated_executor_supplies_wal_writer() -> None:
    """A new streaming caller must decide WAL coverage explicitly."""
    root = pathlib.Path(finecode.wm_server.__file__).parent
    calls: list[bool] = []
    with_writer: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                is_target = func.id == "run_with_partial_results"
            elif isinstance(func, ast.Attribute):
                is_target = func.attr == "run_with_partial_results"
            else:
                continue
            if not is_target:
                continue
            calls.append(True)
            if any(kw.arg == "wal_writer" for kw in node.keywords):
                with_writer.add(str(path.relative_to(root)))
    assert len(calls) == 4, (
        "a new run_with_partial_results caller must decide WAL coverage per #909 C2.option"
    )
    assert with_writer == {"services/run_service/project_executor.py"}, (
        "a new run_with_partial_results caller must decide WAL coverage per #909 C2.option"
    )
