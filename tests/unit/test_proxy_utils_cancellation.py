from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types
from finecode.wm_server.services.run_service import exceptions, proxy_utils


def _build_session(tmp_path: pathlib.Path):
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action"
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return client, project, ws_context


async def test_run_action_translates_er_cancellation_to_action_cancelled_error(
    tmp_path: pathlib.Path,
) -> None:
    """When the ER cancels a request, the WM must surface it as a distinct
    cancellation error rather than as a generic run failure, so that
    cancellations can be handled differently from real errors upstream.
    """
    client, project, ws_context = _build_session(tmp_path)
    client.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly"))

    with pytest.raises(exceptions.ActionCancelledError) as exc_info:
        await proxy_utils.run_action(
            action_name="test_action",
            params={},
            project_def=project,
            ws_context=ws_context,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
        )

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_run_action_still_translates_ordinary_failure_to_action_run_failed(
    tmp_path: pathlib.Path,
) -> None:
    """Regression guard: an ordinary action failure (not a cancellation) must
    still be reported as a normal run failure, unaffected by the new
    cancellation handling.
    """
    client, project, ws_context = _build_session(tmp_path)
    client.configure_response(
        _internal_client_types.ErRunActionResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErRunActionResult(error="boom"),
        )
    )

    with pytest.raises(exceptions.ActionRunFailed):
        await proxy_utils.run_action(
            action_name="test_action",
            params={},
            project_def=project,
            ws_context=ws_context,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
        )


async def test_run_action_in_runner_translates_er_cancellation_to_action_cancelled_error(
    tmp_path: pathlib.Path,
) -> None:
    """The streaming/partial-results dispatch path must translate an ER
    cancellation the same way the plain run path does, otherwise a downstream
    cancellation would surface as a hard failure instead of being handled
    gracefully.
    """
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    client.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly"))

    with pytest.raises(exceptions.ActionCancelledError) as exc_info:
        await proxy_utils.run_action_in_runner(
            action_name="test_action", params={}, runner=runner
        )

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_run_action_in_runner_still_translates_ordinary_failure_to_action_run_failed(
    tmp_path: pathlib.Path,
) -> None:
    """Regression guard mirroring the plain run case above, for the
    streaming/partial-results dispatch path."""
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    client.configure_response(
        _internal_client_types.ErRunActionResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErRunActionResult(error="boom"),
        )
    )

    with pytest.raises(exceptions.ActionRunFailed):
        await proxy_utils.run_action_in_runner(
            action_name="test_action", params={}, runner=runner
        )


async def test_run_handlers_in_env_runner_translates_er_cancellation_to_action_cancelled_error(
    tmp_path: pathlib.Path,
) -> None:
    """The multi-env segment orchestration path must follow the same rule as
    the plain run path: an ER cancellation must be reported as a
    cancellation, not a generic run failure.
    """
    client, project, ws_context = _build_session(tmp_path)
    client.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly"))

    with pytest.raises(exceptions.ActionCancelledError) as exc_info:
        await proxy_utils._run_handlers_in_env_runner(
            action_name="test_action",
            handler_names=["test_handler"],
            payload={},
            previous_result=None,
            env_name="test_env",
            project_def=project,
            ws_context=ws_context,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            result_formats=[],
            wal_run_id="wal-run-1",
        )

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_run_handlers_in_env_runner_still_translates_ordinary_failure_to_action_run_failed(
    tmp_path: pathlib.Path,
) -> None:
    """Regression guard mirroring the plain run case above, for the
    multi-env segment orchestration path.
    """
    client, project, ws_context = _build_session(tmp_path)
    client.configure_response(
        _internal_client_types.ErRunHandlersResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErRunHandlersResult(error="boom"),
        )
    )

    with pytest.raises(exceptions.ActionRunFailed):
        await proxy_utils._run_handlers_in_env_runner(
            action_name="test_action",
            handler_names=["test_handler"],
            payload={},
            previous_result=None,
            env_name="test_env",
            project_def=project,
            ws_context=ws_context,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            result_formats=[],
            wal_run_id="wal-run-1",
        )


def _build_multi_env_session(tmp_path: pathlib.Path):
    client_a = wm_testing.FakeErClient(readable_id="fake-er-a")
    client_b = wm_testing.FakeErClient(readable_id="fake-er-b")
    runner_a = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="env_a", client=client_a
    )
    runner_b = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="env_b", client=client_b
    )
    project = wm_testing.make_multi_env_action_project(
        dir_path=tmp_path, action_name="test_action", handler_envs=["env_a", "env_b"]
    )
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=runner_a,
        env_name="env_a",
        extra_runners={"env_b": runner_b},
    )
    return client_a, client_b, project, ws_context


async def test_run_multi_env_concurrent_translates_all_cancelled_group_to_action_cancelled_error(
    tmp_path: pathlib.Path,
) -> None:
    """When every env group in a concurrent multi-env action is cancelled,
    the overall result must be reported as a cancellation, mirroring the
    single-env behavior, rather than as a generic run failure.
    """
    client_a, client_b, project, ws_context = _build_multi_env_session(tmp_path)
    client_a.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly a"))
    client_b.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly b"))

    action = project.actions[0]

    with pytest.raises(exceptions.ActionCancelledError):
        await proxy_utils._run_multi_env_concurrent(
            action_name="test_action",
            action=action,
            payload={},
            project_def=project,
            ws_context=ws_context,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            result_formats=[],
            wal_run_id="wal-run-1",
        )


async def test_run_multi_env_concurrent_mixed_cancellation_and_failure_is_action_run_failed(
    tmp_path: pathlib.Path,
) -> None:
    """Regression guard: if only *some* env groups were cancelled and others
    genuinely failed, the overall result must stay a run failure — a
    partial cancellation must not hide a real failure from the caller.
    """
    client_a, client_b, project, ws_context = _build_multi_env_session(tmp_path)
    client_a.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly a"))
    client_b.configure_response(
        _internal_client_types.ErRunHandlersResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErRunHandlersResult(error="boom"),
        )
    )

    action = project.actions[0]

    with pytest.raises(exceptions.ActionRunFailed):
        await proxy_utils._run_multi_env_concurrent(
            action_name="test_action",
            action=action,
            payload={},
            project_def=project,
            ws_context=ws_context,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            result_formats=[],
            wal_run_id="wal-run-1",
        )
