"""Tests for what the WM says when an ER asks it to run an action somewhere
that is not a project.

The project paths on ``finecode/runActionInWorkspace`` come out of a handler's
payload — typically a caller-supplied URI that some ER turned into a path — so
the WM has never vetted them.  An unvetted path used to reach a bare dict lookup
deep in the fan-out, and the caller was handed a ``KeyError`` whose whole message
was the repr of a ``PosixPath``: it named the path but never said what the path
had failed to match, or that matching a project was the thing being attempted.
"""

from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import errors
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types
from finecode.wm_server.services.run_service import er_dispatch

_CANONICAL_SOURCE = "test.actions.TestAction"


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


def _make_context(dir_path: pathlib.Path):
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runner = wm_testing.make_running_runner(
        working_dir_path=dir_path, env_name="test_env"
    )
    runner.client.configure_response(
        wm_testing.make_run_action_response(result_by_format={"json": {}})
    )
    return runner, wm_testing.make_workspace_context(project=project, runner=runner)


async def test_unknown_project_path_is_named_along_with_the_known_ones(
    tmp_path: pathlib.Path,
) -> None:
    runner, ws_context = _make_context(tmp_path)
    # the shape the relative-URI bug produced: the project directory with its
    # own last segment appended a second time
    unknown = tmp_path / tmp_path.name

    with pytest.raises(errors.ProjectError) as exc_info:
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner,
            params=_make_params([str(unknown)]),
            ws_context=ws_context,
        )

    message = str(exc_info.value)
    assert str(unknown) in message
    assert "not a project in this workspace" in message
    # the near miss is what turns "wrong path" into "wrong how"
    assert str(tmp_path) in message
    assert "test_action" in message


async def test_the_hint_names_the_closest_projects_not_every_project(
    tmp_path: pathlib.Path,
) -> None:
    """A workspace can hold a hundred projects; listing all of them buries the
    answer under a screenful of paths that have nothing to do with the mistake."""
    runner, ws_context = _make_context(tmp_path)
    near = tmp_path / "packages" / "near"
    far = [pathlib.Path("/elsewhere") / f"far{i}" for i in range(10)]
    for extra in [near, *far]:
        ws_context.ws_projects[extra] = ws_context.ws_projects[tmp_path]

    with pytest.raises(errors.ProjectError) as exc_info:
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner,
            params=_make_params([str(near / "typo")]),
            ws_context=ws_context,
        )

    message = str(exc_info.value)
    assert str(near) in message
    assert "and 9 more" in message
    assert sum(str(f) in message for f in far) <= 1


async def test_a_known_project_path_still_runs(tmp_path: pathlib.Path) -> None:
    """The guard must reject only paths that are genuinely not projects."""
    runner, ws_context = _make_context(tmp_path)

    result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=runner,
        params=_make_params([str(tmp_path)]),
        ws_context=ws_context,
    )

    assert list(result.results_by_project) == [tmp_path.as_posix()]
