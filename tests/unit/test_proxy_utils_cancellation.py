from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types
from finecode.wm_server.services.run_service import exceptions, proxy_utils

pytestmark = pytest.mark.anyio


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
    """When the ER cancels a request (surfaced to the WM as
    ``runner_client.ActionRunCancelled``), ``proxy_utils.run_action`` must
    translate it into ``errors.ActionCancelledError`` — a distinguishable
    domain exception, not tied to any wire-protocol detail — instead of the
    generic ``ActionRunFailed`` used for real failures. This distinction is
    what lets the WM's dispatch boundary send a genuine JSON-RPC
    RequestCancelled response to the IDE instead of an ERROR-level failure.
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
    """Regression guard: an ordinary action failure reported by the ER (not a
    cancellation) must still be classified as ``ActionRunFailed`` — the
    cancellation-specific branch added to ``_run_action_in_env_runner`` must
    not change behavior for real failures. Also exercises the
    ``user_messages.error`` IDE-toast call on this path, which must not raise
    even with no client connected in this harness.
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
