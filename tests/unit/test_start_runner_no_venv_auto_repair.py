from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

import pytest

import finecode_jsonrpc
from finecode.wm_server import domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.errors import ActionNotResolvableError
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
        mock.patch.object(
            runner_manager, "start_runner", side_effect=_fake_start_runner
        ),
        mock.patch.object(runner_start_service, "repair_env", side_effect=_fake_repair),
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
        mock.patch.object(
            runner_manager, "start_runner", side_effect=_fake_start_runner
        ),
        mock.patch.object(runner_start_service, "repair_env") as fake_repair,
    ):
        with pytest.raises(ActionNotResolvableError):
            await proxy_utils.ensure_action_metadata(action, project, ws_context)

    fake_repair.assert_not_called()


def _failed_runner(tmp_path: pathlib.Path, env_name: str):
    runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name=env_name
    )
    runner.status = domain.ExtensionRunnerStatus.FAILED
    return runner


def _crashed_exc() -> runner_manager.RunnerFailedToStart:
    """A start failure built through a real ``raise ... from``, as the spawn
    site builds it: ``ServerFailedToStart`` caused by
    ``ServerExitedBeforePort``."""
    try:
        raise finecode_jsonrpc.ServerExitedBeforePort(1)
    except finecode_jsonrpc.ServerExitedBeforePort as cause:
        try:
            raise runner_manager.RunnerFailedToStart("process exited") from cause
        except runner_manager.RunnerFailedToStart as exc:
            return exc


def test_crashed_before_port_is_repairable_when_included(
    tmp_path: pathlib.Path,
) -> None:
    """A runner the run needs whose ER exited before publishing its port is
    repaired — a crash that early means the venv never got to serve, so an
    install-then-restart may fix it."""
    runner = _failed_runner(tmp_path, "dev_no_runtime")
    assert (
        runner_start_service.start_failure_is_repairable(
            runner, _crashed_exc(), include_crashed=True
        )
        is True
    )


def test_timeout_is_never_repairable(tmp_path: pathlib.Path) -> None:
    """A port timeout is a load problem, not a broken venv: repairing would
    only add an install on top of a slow start."""
    runner = _failed_runner(tmp_path, "dev_no_runtime")
    exc = runner_manager.RunnerFailedToStart("Didn't get port in 30 seconds")
    assert (
        runner_start_service.start_failure_is_repairable(
            runner, exc, include_crashed=True
        )
        is False
    )


def test_no_venv_is_repairable_whatever_the_exception(
    tmp_path: pathlib.Path,
) -> None:
    """A missing venv is always repairable, regardless of the exception or
    the crashed flag."""
    runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="dev_no_runtime"
    )
    runner.status = domain.ExtensionRunnerStatus.NO_VENV
    assert (
        runner_start_service.start_failure_is_repairable(
            runner, RuntimeError("boom"), include_crashed=True
        )
        is True
    )
    assert (
        runner_start_service.start_failure_is_repairable(
            runner, RuntimeError("boom"), include_crashed=False
        )
        is True
    )


def test_crashed_is_not_repairable_when_excluded(
    tmp_path: pathlib.Path,
) -> None:
    """Metadata resolution never crash-repairs: it must not repair an env no
    run needs."""
    runner = _failed_runner(tmp_path, "dev_no_runtime")
    assert (
        runner_start_service.start_failure_is_repairable(
            runner, _crashed_exc(), include_crashed=False
        )
        is False
    )
    assert (
        runner_start_service.start_failure_is_repairable(
            None, _crashed_exc(), include_crashed=True
        )
        is False
    )


async def test_concurrent_repairs_install_once(tmp_path: pathlib.Path) -> None:
    """Two concurrent repairs of the same env install exactly once: the
    second caller finds the runner RUNNING inside the lock and returns."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        handler_env="dev_no_runtime",
    )
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )
    ws_context.ws_projects_extension_runners[tmp_path]["dev_no_runtime"] = (
        _failed_runner(tmp_path, "dev_no_runtime")
    )
    installs: list[str] = []

    async def _fake_install(project_def, env_name, ws_context_) -> None:
        installs.append(env_name)
        await asyncio.sleep(0.05)

    async def _fake_restart(*, runner_working_dir_path, env_name, ws_context) -> None:
        ws_context.ws_projects_extension_runners[runner_working_dir_path][env_name] = (
            wm_testing.make_running_runner(
                working_dir_path=runner_working_dir_path, env_name=env_name
            )
        )

    with (
        mock.patch(
            "finecode.wm_server.services.prepare_envs_service.install_env_for_project",
            side_effect=_fake_install,
        ),
        mock.patch.object(
            runner_manager, "restart_extension_runner", side_effect=_fake_restart
        ),
    ):
        await asyncio.gather(
            runner_start_service.repair_env(project, "dev_no_runtime", ws_context),
            runner_start_service.repair_env(project, "dev_no_runtime", ws_context),
        )

    assert installs == ["dev_no_runtime"]


async def test_ensure_action_metadata_does_not_repair_crashed_envs(
    tmp_path: pathlib.Path,
) -> None:
    """Metadata resolution never crash-repairs, even when the failure is a
    crash before the port: it must not repair an env no run needs."""
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
        crashed_runner = wm_testing.make_running_runner(
            working_dir_path=project_def.dir_path, env_name=env_name
        )
        crashed_runner.status = domain.ExtensionRunnerStatus.FAILED
        runners_by_env[env_name] = crashed_runner
        try:
            raise finecode_jsonrpc.ServerExitedBeforePort(1)
        except finecode_jsonrpc.ServerExitedBeforePort as cause:
            raise runner_manager.RunnerFailedToStart("process exited") from cause

    with (
        mock.patch.object(
            runner_manager, "start_runner", side_effect=_fake_start_runner
        ),
        mock.patch.object(runner_start_service, "repair_env") as fake_repair,
        pytest.raises(ActionNotResolvableError),
    ):
        await proxy_utils.ensure_action_metadata(action, project, ws_context)

    fake_repair.assert_not_called()


async def test_get_or_start_repairs_crashed_env_once_then_retries(
    tmp_path: pathlib.Path,
) -> None:
    """The dispatch start repairs a crashed-before-port env once, then calls
    the plain start once more and returns its runner."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        handler_env="dev_no_runtime",
    )
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )
    calls: list[str] = []

    async def _fake_get_or_start_runner(*, project_def, env_name, ws_context, **_):
        calls.append(env_name)
        if len(calls) == 1:
            runners_by_env = ws_context.ws_projects_extension_runners.setdefault(
                project_def.dir_path, {}
            )
            crashed_runner = wm_testing.make_running_runner(
                working_dir_path=project_def.dir_path, env_name=env_name
            )
            crashed_runner.status = domain.ExtensionRunnerStatus.FAILED
            runners_by_env[env_name] = crashed_runner
            try:
                raise finecode_jsonrpc.ServerExitedBeforePort(1)
            except finecode_jsonrpc.ServerExitedBeforePort as cause:
                raise runner_manager.RunnerFailedToStart("process exited") from cause
        return wm_testing.make_running_runner(
            working_dir_path=project_def.dir_path, env_name=env_name
        )

    with (
        mock.patch.object(
            runner_manager,
            "get_or_start_runner",
            side_effect=_fake_get_or_start_runner,
        ),
        mock.patch.object(runner_start_service, "repair_env") as fake_repair,
    ):
        runner = await runner_start_service.get_or_start_runner_with_auto_prepare(
            project, "dev_no_runtime", ws_context
        )

    fake_repair.assert_called_once_with(project, "dev_no_runtime", ws_context)
    assert calls == ["dev_no_runtime", "dev_no_runtime"]
    assert runner.status == domain.ExtensionRunnerStatus.RUNNING
