"""Recursion-depth control on the workspace fan-out path (ADR-0095).

Workspace fan-out is bounded by height, not width. A nested fan-out's width can
never exceed the workspace's project count, so a width cap measures the
workspace rather than a runaway; the recursion-depth cap is what bounds nested
orchestration, the same rule the project executor already applies.
"""

from __future__ import annotations

import pathlib
from unittest import mock

import pytest

from finecode.wm_server import context
from finecode.wm_server.services.run_service import workspace_executor
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.execution_scopes import OrchestrationPolicy


def _actions_by_project(count: int) -> dict[pathlib.Path, list[str]]:
    return {pathlib.Path(f"/ws/project_{i}"): ["lint"] for i in range(count)}


async def test_wide_fanout_allowed_at_depth_zero() -> None:
    """A workspace with many projects is not a runaway loop, so a
    workspace-wide action is dispatched rather than refused for its width.
    """
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )

    with mock.patch.object(
        workspace_executor.proxy_utils, "run_actions_in_projects", return_value={}
    ) as run_mock:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(10),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=0,
            origin=None,
        )

    assert run_mock.await_count == 1


async def test_wide_fanout_allowed_when_nested() -> None:
    """At depth > 0 the width is still bounded by the workspace's project
    count, not by the recursion — a workspace-wide gather must not be refused
    for being wide (ADR-0095)."""
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )

    with mock.patch.object(
        workspace_executor.proxy_utils, "run_actions_in_projects", return_value={}
    ) as run_mock:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(100),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=1,
            origin=None,
        )

    assert run_mock.await_count == 1


async def test_depth_limit_refused_before_dispatch() -> None:
    """Nested orchestration is bounded by height: a caller already at the
    policy's depth limit is refused before anything fans out."""
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )
    policy = OrchestrationPolicy(max_recursion_depth=3)

    with (
        mock.patch.object(
            workspace_executor.proxy_utils,
            "run_actions_in_projects",
            return_value={},
        ) as run_mock,
        pytest.raises(ActionRunFailed) as exc_info,
    ):
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(1),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=3,
            policy=policy,
            origin=None,
        )

    assert "Orchestration depth 3 reached limit 3" in str(exc_info.value)
    assert run_mock.await_count == 0
