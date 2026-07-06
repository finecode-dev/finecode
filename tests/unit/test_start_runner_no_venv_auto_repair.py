from __future__ import annotations

import pathlib
from unittest import mock

import pytest

from finecode.wm_server import domain, testing as wm_testing
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import runner_start_service
from finecode.wm_server.services.run_service import proxy_utils


async def test_ensure_action_metadata_auto_repairs_a_no_venv_runner(
    tmp_path: pathlib.Path,
) -> None:
    """A venv-start failure must not surface as a bare "runner failed to start"
    error requiring a manual `prepare-envs` run — whether the venv never
    existed, or was just wiped by ``get_python_cmd`` after detecting it was
    stale/relocated (see ``finecode_cmd.VenvRelocatedError``), a NO_VENV runner
    must trigger the same auto-repair that the dispatch path
    (``get_or_start_runner_with_auto_prepare``) already gets.
    """
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="get_src_artifact_language",
        handler_env="dev_no_runtime",
    )
    action = project.actions[0]
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )

    async def _fake_start_runner(*, project_def, env_name, ws_context, **_):
        # Mirror what _start_extension_runner_process does on a missing/stale
        # venv: register a NO_VENV runner for this env, then fail.
        runners_by_env = ws_context.ws_projects_extension_runners.setdefault(
            project_def.dir_path, {}
        )
        no_venv_runner = wm_testing.make_running_runner(
            working_dir_path=project_def.dir_path, env_name=env_name
        )
        no_venv_runner.status = domain.ExtensionRunnerStatus.NO_VENV
        runners_by_env[env_name] = no_venv_runner
        raise runner_manager.RunnerFailedToStart("venv not found")

    async def _fake_repair(project_def, env_name, ws_context_):
        assert env_name == "dev_no_runtime"
        action.canonical_source = f"resolved.{action.source}"

    with (
        mock.patch.object(runner_manager, "start_runner", side_effect=_fake_start_runner),
        mock.patch.object(
            runner_start_service, "repair_no_venv_env", side_effect=_fake_repair
        ),
    ):
        await proxy_utils.ensure_action_metadata(action, project, ws_context)

    assert action.canonical_source == f"resolved.{action.source}"


async def test_ensure_action_metadata_does_not_auto_repair_non_venv_failures(
    tmp_path: pathlib.Path,
) -> None:
    """A startup failure unrelated to a missing venv (e.g. a real crash) must
    still surface directly — auto-repair is only for NO_VENV runners."""
    from finecode.wm_server.errors import ActionNotResolvableError

    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="get_src_artifact_language",
        handler_env="dev_no_runtime",
    )
    action = project.actions[0]
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )

    async def _fake_start_runner(*, project_def, env_name, ws_context, **_):
        runners_by_env = ws_context.ws_projects_extension_runners.setdefault(
            project_def.dir_path, {}
        )
        failed_runner = wm_testing.make_running_runner(
            working_dir_path=project_def.dir_path, env_name=env_name
        )
        failed_runner.status = domain.ExtensionRunnerStatus.FAILED
        runners_by_env[env_name] = failed_runner
        raise runner_manager.RunnerFailedToStart("process crashed")

    with (
        mock.patch.object(runner_manager, "start_runner", side_effect=_fake_start_runner),
        mock.patch.object(runner_start_service, "repair_no_venv_env") as fake_repair,
    ):
        with pytest.raises(ActionNotResolvableError):
            await proxy_utils.ensure_action_metadata(action, project, ws_context)

    fake_repair.assert_not_called()
