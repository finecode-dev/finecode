from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import domain, testing as wm_testing
from finecode.wm_server._api_handlers._helpers import _resolve_actions_by_project

pytestmark = pytest.mark.anyio


def _build_ws_context_with_action(
    tmp_path: pathlib.Path, *, scope: domain.ActionScope | None
):
    """Build a single-project WorkspaceContext with one action of the given scope.

    ``canonical_source`` is pre-populated so scope resolution doesn't trigger a
    metadata round-trip through the (unconfigured) fake ER client.
    """
    action_source = "test.actions.TestAction"
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action", action_source=action_source
    )
    project.actions[0].canonical_source = action_source
    project.actions[0].scope = scope

    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return ws_context, action_source


async def test_explicit_project_rejected_for_workspace_scoped_action(
    tmp_path: pathlib.Path,
) -> None:
    """``runBatch`` must reject an explicit ``--project`` for a workspace-scoped
    action, mirroring the guard already enforced for single-action ``actions/run``
    (see ``_parse_and_validate_run_action_params``). Without this guard, passing
    any project (even the workspace root) silently dispatches the action hosted
    on the wrong project, which crashes deep inside handler execution instead of
    failing with a clear message.
    """
    ws_context, action_source = _build_ws_context_with_action(
        tmp_path, scope=domain.ActionScope.WORKSPACE
    )

    with pytest.raises(ValueError, match="workspace-scoped"):
        await _resolve_actions_by_project(
            project_names=[str(tmp_path)],
            action_sources=[action_source],
            ws_context=ws_context,
        )


async def test_explicit_project_accepted_for_project_scoped_action(
    tmp_path: pathlib.Path,
) -> None:
    """Control case: a normal project-scoped action must still resolve fine
    when an explicit ``--project`` is given."""
    ws_context, action_source = _build_ws_context_with_action(
        tmp_path, scope=domain.ActionScope.PROJECT
    )

    actions_by_project, name_to_source = await _resolve_actions_by_project(
        project_names=[str(tmp_path)],
        action_sources=[action_source],
        ws_context=ws_context,
    )

    assert actions_by_project == {tmp_path: ["test_action"]}
    assert name_to_source == {"test_action": action_source}
