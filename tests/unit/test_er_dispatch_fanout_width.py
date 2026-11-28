"""The ER back-channel workspace fan-out is bounded by depth, not width.

``finecode/runActionInWorkspace`` is the only route by which a handler can fan
an action out across the workspace. Its width is bounded by the workspace's
project count — every requested path is validated against ``ws_projects`` — so
the recursion-depth cap is what actually bounds nested orchestration, matching
what the project executor already enforces.
"""

from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import context
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types
from finecode.wm_server.services.run_service import er_dispatch
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed

_CANONICAL_SOURCE = "test.actions.TestAction"


def _make_context(project_count: int, tmp_path: pathlib.Path):
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    caller_runner = None
    for index in range(project_count):
        dir_path = tmp_path / f"p{index}"
        project = wm_testing.make_single_action_project(
            dir_path=dir_path,
            action_name="test_action",
            action_source=_CANONICAL_SOURCE,
        )
        project.actions[0].canonical_source = _CANONICAL_SOURCE
        runner = wm_testing.make_running_runner(working_dir_path=dir_path)
        runner.client.configure_response(
            wm_testing.make_run_action_response(result_by_format={"json": {}})
        )
        ws_context.ws_projects[dir_path] = project
        ws_context.ws_projects_extension_runners[dir_path] = {"test_env": runner}
        if index == 0:
            caller_runner = runner

    assert caller_runner is not None
    return caller_runner, ws_context


def _make_params(orchestration_depth: int):
    return _internal_client_types.RunActionInWorkspaceParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=orchestration_depth
        ),
        project_paths=None,
    )


async def test_width_is_not_a_limit(tmp_path: pathlib.Path) -> None:
    """A workspace-wide gather from a handler is not refused for being wide.

    The observed failure was every project of a 72-project workspace being
    refused a workspace-wide nested lint at depth 1 — a false positive that
    made ``inspect_code`` unusable in that workspace size class.
    """
    runner, ws_context = _make_context(72, tmp_path)

    result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=runner,
        params=_make_params(orchestration_depth=1),
        ws_context=ws_context,
    )

    assert len(result.results_by_project) == 72


async def test_depth_limit_is_enforced_on_the_back_channel(
    tmp_path: pathlib.Path,
) -> None:
    """The back-channel gains the same height guard the project path has.

    The width cap was the only guard on this route; removing it without a depth
    check would leave a recursive handler chain unbounded.
    """
    runner, ws_context = _make_context(72, tmp_path)

    await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=runner,
        params=_make_params(orchestration_depth=7),
        ws_context=ws_context,
    )

    with pytest.raises(ActionRunFailed) as exc_info:
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner,
            params=_make_params(orchestration_depth=8),
            ws_context=ws_context,
        )

    assert "Orchestration depth 8 reached limit 8" in str(exc_info.value)
