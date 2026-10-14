""":func:`start_runners_with_presets` isolates per-project start failures.

When the WM resolves projects on demand or an eager ``addDir`` starts a whole
workspace, one broken project must not block its siblings: every other project
still becomes a ``ResolvedProject``, and the failure is reported as a
``ProjectsFailedToResolve`` naming exactly the projects that failed.  A
single-project call keeps raising the original exception object so callers that
recover a single project (``reload-config``, environment install, an
on-demand gate call) see the same failure types they always did.
"""

from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.runner import preset_resolution, runner_client, runner_manager
from finecode.wm_server.services import prepare_envs_service, runner_start_service
from finecode.wm_server.testing import make_running_runner


def _raw_config() -> dict:
    return {"tool": {"finecode": {"env": {"dev_workspace": {}}}}}


def _make_project(dir_path: pathlib.Path) -> domain.CollectedProject:
    return domain.CollectedProject(
        name=dir_path.name,
        dir_path=dir_path,
        def_path=dir_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[],
        services=[],
        action_handler_configs={},
    )


def _make_context(
    paths: list[pathlib.Path],
) -> context.WorkspaceContext:
    ws_context = context.WorkspaceContext(ws_dirs_paths=list(paths))
    for path in paths:
        ws_context.ws_projects[path] = _make_project(path)
        ws_context.ws_projects_raw_configs[path] = _raw_config()
    return ws_context


def _register_running_dev_workspace(
    ws_context: context.WorkspaceContext, dir_path: pathlib.Path
) -> runner_client.ExtensionRunnerInfo:
    runner = make_running_runner(working_dir_path=dir_path, env_name="dev_workspace")
    ws_context.ws_projects_extension_runners.setdefault(dir_path, {})[
        "dev_workspace"
    ] = runner
    return runner


async def _stub_read_project_config(*args: object, **kwargs: object) -> None:
    pass


def _stub_start_dev_workspace_runner(ws_context: context.WorkspaceContext):
    async def _fake(project_def: domain.Project, ws_context, **kwargs: object):
        return _register_running_dev_workspace(ws_context, project_def.dir_path)

    return _fake


async def test_first_pass_start_failure_does_not_block_siblings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One project whose dev_workspace runner fails to start must not block the
    rest: siblings resolve, and the raised failure names only the failed one."""
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    ws_context = _make_context([a, b, c])

    async def _fake(project_def: domain.Project, ws_context, **kwargs: object):
        if project_def.dir_path == a:
            raise runner_manager.RunnerFailedToStart("venv missing")
        return _register_running_dev_workspace(ws_context, project_def.dir_path)

    monkeypatch.setattr(runner_manager, "_start_dev_workspace_runner", _fake)
    monkeypatch.setattr(
        runner_manager.preset_resolution,
        "read_project_config_with_py_presets",
        _stub_read_project_config,
    )
    recorded: list[pathlib.Path] = []

    async def _recording_update_runner_config(**kwargs: object) -> None:
        recorded.append(kwargs["project"].dir_path)

    monkeypatch.setattr(
        runner_manager, "update_runner_config", _recording_update_runner_config
    )

    projects = [ws_context.ws_projects[p] for p in (a, b, c)]
    with pytest.raises(runner_manager.RunnerFailedToStart) as excinfo:
        await runner_manager.start_runners_with_presets(projects, ws_context)

    assert isinstance(excinfo.value, runner_manager.ProjectsFailedToResolve)
    assert set(excinfo.value.per_project) == {a}
    assert isinstance(ws_context.ws_projects[b], domain.ResolvedProject)
    assert isinstance(ws_context.ws_projects[c], domain.ResolvedProject)
    assert recorded == [b, c]


async def test_second_pass_failures_are_isolated_and_wrapped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second-pass failure — a config error, a disconnected dev_workspace
    runner, or an updateConfig error — is recorded per project; the healthy
    sibling still resolves.  The disconnected-runner failure is wrapped in
    ``RunnerFailedToStart`` like the single-project path wraps it."""
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    ws_context = _make_context([a, b, c])
    monkeypatch.setattr(
        runner_manager,
        "_start_dev_workspace_runner",
        _stub_start_dev_workspace_runner(ws_context),
    )

    async def _read_presets(
        project: domain.Project, ws_context: context.WorkspaceContext, **kwargs: object
    ) -> None:
        if project.dir_path == b:
            raise preset_resolution.DevWorkspaceRunnerNotConnectedError(
                "dev_workspace not connected"
            )

    monkeypatch.setattr(
        runner_manager.preset_resolution,
        "read_project_config_with_py_presets",
        _read_presets,
    )

    async def _updating(**kwargs: object) -> None:
        if kwargs["project"].dir_path == a:
            raise runner_manager.RunnerFailedToStart("updateConfig failed")

    monkeypatch.setattr(runner_manager, "update_runner_config", _updating)

    projects = [ws_context.ws_projects[p] for p in (a, b, c)]
    with pytest.raises(runner_manager.ProjectsFailedToResolve) as excinfo:
        await runner_manager.start_runners_with_presets(projects, ws_context)

    assert set(excinfo.value.per_project) == {a, b}
    assert isinstance(excinfo.value.per_project[a], runner_manager.RunnerFailedToStart)
    assert isinstance(excinfo.value.per_project[b], runner_manager.RunnerFailedToStart)
    # The disconnected-runner failure carries the original message.
    assert "not connected" in excinfo.value.per_project[b].message
    assert isinstance(ws_context.ws_projects[c], domain.ResolvedProject)


async def test_single_project_raises_the_original_exception(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-project call propagates the exact recorded exception object, so
    ``next_step.for_runner_failure``'s ``EnvironmentOutOfDateError`` check and
    the recovery path keep working."""
    a = tmp_path / "a"
    ws_context = _make_context([a])
    monkeypatch.setattr(
        runner_manager,
        "_start_dev_workspace_runner",
        _stub_start_dev_workspace_runner(ws_context),
    )
    monkeypatch.setattr(
        runner_manager.preset_resolution,
        "read_project_config_with_py_presets",
        _stub_read_project_config,
    )
    env_error = runner_manager.EnvironmentOutOfDateError(
        "env stale", env_name="dev_workspace"
    )

    async def _raising_update_runner_config(**kwargs: object) -> None:
        raise env_error

    monkeypatch.setattr(
        runner_manager, "update_runner_config", _raising_update_runner_config
    )

    with pytest.raises(runner_manager.EnvironmentOutOfDateError) as excinfo:
        await runner_manager.start_runners_with_presets(
            [ws_context.ws_projects[a]], ws_context
        )

    assert excinfo.value is env_error


async def test_auto_prepare_merges_its_failure_into_per_project(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When auto-prepare fails for one project of a multi-project batch, the
    raised failure still names every project that failed its start: the
    auto-prepare victim carries its install failure, the siblings that were
    never reached keep their own start failure."""
    a, b = tmp_path / "a", tmp_path / "b"
    ws_context = _make_context([a, b])
    original = runner_manager.ProjectsFailedToResolve(
        per_project={
            a: runner_manager.RunnerFailedToStart("start failed a"),
            b: runner_manager.RunnerFailedToStart("start failed b"),
        },
        message="Failed to start runner(s) for: a, b.",
    )
    # A has no venv → it is the auto-prepare victim; B's runner is RUNNING.
    ws_context.ws_projects_extension_runners[a] = {
        "dev_workspace": runner_client.ExtensionRunnerInfo(
            working_dir_path=a,
            env_name="dev_workspace",
            status=runner_client.RunnerStatus.NO_VENV,
        )
    }
    ws_context.ws_projects_extension_runners[b] = {
        "dev_workspace": make_running_runner(
            working_dir_path=b, env_name="dev_workspace"
        )
    }

    async def _failing_install(
        project: domain.Project, env_name: str, ws_context: context.WorkspaceContext
    ) -> None:
        raise prepare_envs_service.PrepareEnvsFailed(
            f"install failed for {project.dir_path}"
        )

    monkeypatch.setattr(
        prepare_envs_service, "install_env_for_project", _failing_install
    )

    with pytest.raises(runner_manager.ProjectsFailedToResolve) as excinfo:
        await runner_start_service._auto_prepare_and_retry(
            exc=original,
            projects=[ws_context.ws_projects[a], ws_context.ws_projects[b]],
            ws_context=ws_context,
            initialize_all_handlers=False,
        )

    assert set(excinfo.value.per_project) == {a, b}
    assert "install failed for" in excinfo.value.per_project[a].message
    # B's original start failure is untouched.
    assert excinfo.value.per_project[b].message == "start failed b"


async def _noop_update_runner_config(**kwargs: object) -> None:
    pass


async def test_resolved_projects_clear_remembered_failures(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project this call resolves is dropped from the remembered-failure map,
    whatever the caller; a project that fails stays remembered for whoever
    recorded it."""
    a, b = tmp_path / "a", tmp_path / "b"
    ws_context = _make_context([a, b])
    ws_context.project_resolution_failures[a] = "old failure a"
    ws_context.project_resolution_failures[b] = "old failure b"
    monkeypatch.setattr(
        runner_manager,
        "_start_dev_workspace_runner",
        _stub_start_dev_workspace_runner(ws_context),
    )

    async def _read_presets(
        project: domain.Project, ws_context: context.WorkspaceContext, **kwargs: object
    ) -> None:
        if project.dir_path == b:
            raise preset_resolution.DevWorkspaceRunnerNotConnectedError("down")

    monkeypatch.setattr(
        runner_manager.preset_resolution,
        "read_project_config_with_py_presets",
        _read_presets,
    )
    monkeypatch.setattr(
        runner_manager, "update_runner_config", _noop_update_runner_config
    )

    with pytest.raises(runner_manager.ProjectsFailedToResolve):
        await runner_manager.start_runners_with_presets(
            [ws_context.ws_projects[a], ws_context.ws_projects[b]], ws_context
        )

    assert a not in ws_context.project_resolution_failures
    assert ws_context.project_resolution_failures[b] == "old failure b"
