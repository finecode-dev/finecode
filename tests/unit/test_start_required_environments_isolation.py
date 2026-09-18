"""Requirement tests: one failed env start must not cancel its siblings.

REQUIREMENT (ADR-0097): starting the environments a run needs fans out across
projects. A single env that fails to start (a port-handshake timeout, a missing
venv, a crash) must leave the other starts alone: cancelling them marks healthy
runners FAILED and forces the next run to restart all of them. The run still
fails, and the error still names every env that failed.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

import pytest

from finecode.wm_server import context
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services.run_service import proxy_utils


def _make_projects(
    tmp_path: pathlib.Path, count: int
) -> tuple[list, context.WorkspaceContext, dict[pathlib.Path, list[str]]]:
    projects = []
    for index in range(count):
        project_dir = tmp_path / f"project_{index}"
        project_dir.mkdir()
        project = wm_testing.make_single_action_project(
            dir_path=project_dir,
            action_name=f"action_{index}",
            handler_env=f"env_{index}",
        )
        projects.append(project)

    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    actions_by_projects: dict[pathlib.Path, list[str]] = {}
    for project in projects:
        ws_context.ws_projects[project.dir_path] = project
        actions_by_projects[project.dir_path] = [project.actions[0].name]

    return projects, ws_context, actions_by_projects


def _env_name(project) -> str:
    return project.actions[0].handlers[0].env


async def test_one_failed_start_does_not_cancel_siblings(
    tmp_path: pathlib.Path,
) -> None:
    """The failing env's error must name it, and a slower sibling must run to
    completion rather than being cancelled by the failure."""
    projects, ws_context, actions_by_projects = _make_projects(tmp_path, 3)
    completed: list[str] = []

    async def _fake_start_runner(*, project_def, env_name, ws_context, **_kwargs):
        if project_def is projects[0]:
            await asyncio.sleep(0.01)
            raise runner_manager.RunnerFailedToStart("boom")
        if project_def is projects[1]:
            await asyncio.sleep(0.1)
            completed.append(env_name)
        return None

    with mock.patch.object(
        runner_manager, "start_runner", side_effect=_fake_start_runner
    ):
        with pytest.raises(proxy_utils.StartingEnvironmentsFailed) as exc_info:
            await proxy_utils.start_required_environments(
                actions_by_projects, ws_context
            )

    message = str(exc_info.value)
    assert _env_name(projects[0]) in message
    assert projects[0].name in message
    # env_1 was still running when env_0 failed and was *not* cancelled.
    assert completed == [_env_name(projects[1])]
    # env_2 also ran (no assertion on ordering, only that it was not cancelled).
    assert _env_name(projects[2]) not in message


async def test_outer_cancellation_still_cancels_every_start(
    tmp_path: pathlib.Path,
) -> None:
    """Cancelling the run must cancel the starts it fanned out — the isolation
    above is between siblings, not a promise that starts outlive their caller."""
    projects, ws_context, actions_by_projects = _make_projects(tmp_path, 3)
    cancelled: list[str] = []

    async def _fake_start_runner(*, project_def, env_name, ws_context, **_kwargs):
        if project_def is projects[1]:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                cancelled.append(env_name)
                raise
        return None

    with mock.patch.object(
        runner_manager, "start_runner", side_effect=_fake_start_runner
    ):
        task = asyncio.create_task(
            proxy_utils.start_required_environments(actions_by_projects, ws_context)
        )
        await asyncio.sleep(0.05)  # let the slow start enter its sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cancelled == [_env_name(projects[1])]
