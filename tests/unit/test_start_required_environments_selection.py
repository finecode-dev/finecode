"""The run gate starts only the matrix children the dispatch will run.

An unselected axis child must neither be started nor auto-repaired: its venv
may be missing or stale (CI-only), and creating it locally is the defect.
"""

from __future__ import annotations

import pathlib
from unittest import mock

import pytest

import finecode_jsonrpc
from finecode.wm_server import context, domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import runner_start_service
from finecode.wm_server.services.run_service import proxy_utils


def _make_project(tmp_path: pathlib.Path):
    project_dir = tmp_path / "p"
    project_dir.mkdir(exist_ok=True)
    project = wm_testing.make_multi_env_action_project(
        dir_path=project_dir,
        action_name="test_action",
        handler_envs=[
            "testing@cpython-3.13",
            "testing@cpython-3.14",
            "dev_no_runtime",
        ],
    )
    for handler in project.actions[0].handlers:
        if handler.env == "testing@cpython-3.13":
            handler.interpreter = "cpython@3.13"
        elif handler.env == "testing@cpython-3.14":
            handler.interpreter = "cpython@3.14"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project
    return project, ws_context


def _recording_start_runner(started: list[str]):
    async def _fake_start_runner(**kwargs):
        started.append(kwargs["env_name"])

    return _fake_start_runner


async def _run_gate(
    tmp_path: pathlib.Path,
    selection: dict | None,
) -> list[str]:
    project, ws_context = _make_project(tmp_path)
    started: list[str] = []
    with mock.patch.object(
        runner_manager,
        "start_runner",
        side_effect=_recording_start_runner(started),
    ):
        await proxy_utils.start_required_environments(
            {project.dir_path: [project.actions[0].name]},
            ws_context,
            selected_interpreters_by_project=selection,
        )
    return started


async def test_only_selected_child_is_started(tmp_path: pathlib.Path) -> None:
    """A selection for one child starts that child (plus the non-matrix env)
    and leaves the unselected child alone."""
    project, ws_context = _make_project(tmp_path)
    started: list[str] = []

    with mock.patch.object(
        runner_manager,
        "start_runner",
        side_effect=_recording_start_runner(started),
    ):
        await proxy_utils.start_required_environments(
            {project.dir_path: [project.actions[0].name]},
            ws_context,
            selected_interpreters_by_project={project.dir_path: {"cpython@3.14"}},
        )

    assert sorted(started) == ["dev_no_runtime", "testing@cpython-3.14"]


async def test_no_selection_starts_every_child(tmp_path: pathlib.Path) -> None:
    """Without a selection (omitted, project absent, or None for the project)
    the gate keeps today's behaviour and starts every child."""
    expected = [
        "dev_no_runtime",
        "testing@cpython-3.13",
        "testing@cpython-3.14",
    ]
    assert sorted(await _run_gate(tmp_path, None)) == expected
    assert sorted(
        await _run_gate(tmp_path, {tmp_path / "other": {"cpython@3.14"}})
    ) == (expected)
    assert sorted(await _run_gate(tmp_path, {tmp_path / "p": None})) == expected


async def test_non_matrix_env_starts_despite_selection(
    tmp_path: pathlib.Path,
) -> None:
    """A handler with no interpreter is started regardless of the selection —
    covered by the first test's ``dev_no_runtime`` assertion, restated here
    with a selection naming only the other child."""
    project, ws_context = _make_project(tmp_path)
    started: list[str] = []

    with mock.patch.object(
        runner_manager,
        "start_runner",
        side_effect=_recording_start_runner(started),
    ):
        await proxy_utils.start_required_environments(
            {project.dir_path: [project.actions[0].name]},
            ws_context,
            selected_interpreters_by_project={project.dir_path: {"cpython@3.13"}},
        )

    assert "dev_no_runtime" in started
    assert "testing@cpython-3.11" not in started


async def test_crashed_needed_env_is_repaired_once_then_fails(
    tmp_path: pathlib.Path,
) -> None:
    """A needed env whose ER crashes before publishing its port is repaired
    once; if the restart still fails, the error names the env and project."""
    project_dir = tmp_path / "p"
    project_dir.mkdir(exist_ok=True)
    project = wm_testing.make_single_action_project(
        dir_path=project_dir,
        action_name="test_action",
        handler_env="dev_no_runtime",
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project

    async def _fake_start_runner(**kwargs) -> None:
        runners_by_env = ws_context.ws_projects_extension_runners.setdefault(
            project.dir_path, {}
        )
        crashed_runner = wm_testing.make_running_runner(
            working_dir_path=project.dir_path, env_name="dev_no_runtime"
        )
        crashed_runner.status = domain.ExtensionRunnerStatus.FAILED
        runners_by_env["dev_no_runtime"] = crashed_runner
        try:
            raise finecode_jsonrpc.ServerExitedBeforePort(1)
        except finecode_jsonrpc.ServerExitedBeforePort as cause:
            raise runner_manager.RunnerFailedToStart("process exited") from cause

    with (
        mock.patch.object(
            runner_manager, "start_runner", side_effect=_fake_start_runner
        ),
        mock.patch.object(
            runner_start_service,
            "repair_env",
            side_effect=runner_manager.RunnerFailedToStart("still broken"),
        ) as fake_repair,
        pytest.raises(proxy_utils.StartingEnvironmentsFailed) as exc_info,
    ):
        await proxy_utils.start_required_environments(
            {project.dir_path: [project.actions[0].name]}, ws_context
        )

    message = str(exc_info.value)
    assert "dev_no_runtime" in message
    assert project.name is not None and project.name in message
    fake_repair.assert_called_once_with(project, "dev_no_runtime", ws_context)
