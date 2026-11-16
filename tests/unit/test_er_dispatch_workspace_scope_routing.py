"""Where a handler's back-channel dispatch runs when it names no projects.

A workspace-scoped action runs once, hosted in the workspace root, and does its
own per-project fan-out. Dispatched from a handler without project paths, it
used to run in every project that declares it — each instance then fanning out
to every project again, N² dispatches in an N-project workspace.
"""

from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import context, domain, errors
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, runner_client
from finecode.wm_server.services.run_service import er_dispatch

_CANONICAL_SOURCE = "test.actions.TestAction"
_PACKAGE_COUNT = 3


def _add_project(
    ws_context: context.WorkspaceContext,
    dir_path: pathlib.Path,
    scope: domain.ActionScope,
    action_name: str = "test_action",
) -> runner_client.ExtensionRunnerInfo:
    project =wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name=action_name,
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    project.actions[0].scope = scope
    runner = wm_testing.make_running_runner(working_dir_path=dir_path)
    runner.client.configure_response(
        wm_testing.make_run_action_response(result_by_format={"json": {}})
    )
    ws_context.ws_projects[dir_path] = project
    ws_context.ws_projects_extension_runners[dir_path] = {"test_env": runner}
    return runner


def _make_workspace(
    tmp_path: pathlib.Path,
    scope: domain.ActionScope,
    root_action_name: str = "test_action",
) -> tuple[
    runner_client.ExtensionRunnerInfo,
    list[runner_client.ExtensionRunnerInfo],
    list[pathlib.Path],
    context.WorkspaceContext,
]:
    """A root project plus packages, all declaring the action; the root
    project's runner is the caller."""
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    root_runner = _add_project(ws_context, tmp_path, scope, root_action_name)
    packages = [tmp_path / f"p{index}" for index in range(_PACKAGE_COUNT)]
    package_runners = [_add_project(ws_context, p, scope) for p in packages]
    return root_runner, package_runners, packages, ws_context


def _make_params(
    project_paths: list[str] | None,
) -> _internal_client_types.RunActionInWorkspaceParams:
    return _internal_client_types.RunActionInWorkspaceParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        project_paths=project_paths,
    )


async def test_workspace_action_without_paths_runs_once_in_the_root(
    tmp_path: pathlib.Path,
) -> None:
    """Running it in every declaring project multiplies its own fan-out by the
    project count, and concurrent instances race on shared workspace state."""
    root_runner, _, _, ws_context = _make_workspace(
        tmp_path, domain.ActionScope.WORKSPACE
    )

    result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=root_runner, params=_make_params(None), ws_context=ws_context
    )

    assert list(result.results_by_project) == [tmp_path.as_posix()]


async def test_workspace_action_without_paths_goes_to_the_root_from_any_caller(
    tmp_path: pathlib.Path,
) -> None:
    """The hosting project is the workspace root, not whichever project's
    handler happened to ask."""
    _, package_runners, _, ws_context = _make_workspace(
        tmp_path, domain.ActionScope.WORKSPACE
    )

    result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=package_runners[0], params=_make_params(None), ws_context=ws_context
    )

    assert list(result.results_by_project) == [tmp_path.as_posix()]


async def test_workspace_action_with_explicit_paths_runs_where_asked(
    tmp_path: pathlib.Path,
) -> None:
    """Explicit paths stay an escape hatch: the inspect_code bridges run a
    workspace-scoped lint in each project on purpose."""
    root_runner, _, packages, ws_context = _make_workspace(
        tmp_path, domain.ActionScope.WORKSPACE
    )

    result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=root_runner,
        params=_make_params([str(packages[1])]),
        ws_context=ws_context,
    )

    assert list(result.results_by_project) == [packages[1].as_posix()]


async def test_project_action_without_paths_still_runs_in_every_declaring_project(
    tmp_path: pathlib.Path,
) -> None:
    """Hosting on the root applies only to workspace scope; a per-project
    gather that ran in one project would silently drop every other result."""
    root_runner, _, packages, ws_context = _make_workspace(
        tmp_path, domain.ActionScope.PROJECT
    )

    result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=root_runner, params=_make_params(None), ws_context=ws_context
    )

    assert sorted(result.results_by_project) == sorted(
        p.as_posix() for p in [tmp_path, *packages]
    )


async def test_root_that_does_not_declare_the_action_is_an_error(
    tmp_path: pathlib.Path,
) -> None:
    """Falling back to every declaring project would bring the N² fan-out back
    without a trace; the caller has to learn the root cannot host it."""
    _, package_runners, _, ws_context = _make_workspace(
        tmp_path, domain.ActionScope.WORKSPACE, root_action_name="other_action"
    )

    with pytest.raises(errors.ActionNotFoundError) as exc_info:
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=package_runners[0],
            params=_make_params(None),
            ws_context=ws_context,
        )

    assert "test_action" in str(exc_info.value)
    assert str(tmp_path) in str(exc_info.value)


async def test_ambiguous_workspace_root_is_an_error(tmp_path: pathlib.Path) -> None:
    """With no single root there is no host; guessing one would run the action
    against the wrong slice of the workspace."""
    first, second = tmp_path / "a", tmp_path / "b"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[first, second])
    runner = _add_project(ws_context, first, domain.ActionScope.WORKSPACE)
    _ = _add_project(ws_context, second, domain.ActionScope.WORKSPACE)

    with pytest.raises(errors.ProjectError) as exc_info:
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner, params=_make_params(None), ws_context=ws_context
        )

    assert "test_action" in str(exc_info.value)
