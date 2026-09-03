"""Fan-out width control on the `run` path (ADR-0067).

``OrchestrationPolicy.max_project_fanout`` is a *runaway-recursion* guard and
refuses. It applies only to nested orchestration. The throttling half of
ADR-0067 was replaced by the machine-wide process budget (ADR-0090), which
bounds subprocesses at the leaf instead of bounding project fan-out as a
proxy.
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
    """A workspace simply containing more projects than the cap is not a
    runaway loop — refusing it made every workspace-wide action unusable past
    an arbitrary workspace size.
    """
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )
    policy = OrchestrationPolicy(max_project_fanout=4)

    with mock.patch.object(
        workspace_executor.proxy_utils, "run_actions_in_projects", return_value={}
    ) as run_mock:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(10),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=0,
            policy=policy,
            origin=None,
        )

    assert run_mock.await_count == 1


async def test_wide_fanout_refused_when_nested() -> None:
    """At depth > 0 the width was produced by a handler fanning out, which is
    exactly the blast radius ADR-0016 wants bounded."""
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )
    policy = OrchestrationPolicy(max_project_fanout=4)

    with pytest.raises(ActionRunFailed) as exc_info:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(10),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=1,
            policy=policy,
            origin=None,
        )

    assert "10" in str(exc_info.value)
    assert "depth 1" in str(exc_info.value)


async def test_narrow_fanout_allowed_when_nested() -> None:
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )
    policy = OrchestrationPolicy(max_project_fanout=4)

    with mock.patch.object(
        workspace_executor.proxy_utils, "run_actions_in_projects", return_value={}
    ) as run_mock:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(3),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=2,
            policy=policy,
            origin=None,
        )

    assert run_mock.await_count == 1
