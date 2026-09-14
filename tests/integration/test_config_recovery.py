"""PRD-0008 — in-process integration tests for configuration recovery
(``workspace/reloadConfig``).

Drives the real dispatch loop over a real TCP loopback connection (see
``tests/integration/conftest.py``); no subprocess, no ``tests/e2e/``.

The two steps that need operating-system processes — re-reading a config through
a live ``dev_workspace`` runner, and replacing a project's runners — are
substituted per test. Everything the recovery itself decides (what it invalidates,
in which order it does the two steps, what it reports, and what it leaves behind
when a step fails) is real, and that is what these tests are about. The two
substituted steps are covered end to end by PRD-0008-AC3 in ``tests/e2e/``.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services import config_reload_service


def _project(project_dir: pathlib.Path, action_names: list[str]) -> domain.Project:
    return domain.CollectedProject(
        name=project_dir.name,
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[
            domain.Action(name=name, source=f"pkg.{name}", handlers=[], config={})
            for name in action_names
        ],
        services=[],
        action_handler_configs={},
    )


def _seed_project(
    wm_client, project_dir: pathlib.Path, action_names: list[str]
) -> runner_client.ExtensionRunnerInfo:
    ws_context = wm_client.ws_context
    ws_context.ws_projects[project_dir] = _project(project_dir, action_names)
    ws_context.ws_projects_raw_configs[project_dir] = {"tool": {"finecode": {}}}
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=project_dir,
        env_name="dev_workspace",
        status=runner_client.RunnerStatus.RUNNING,
    )
    ws_context.ws_projects_extension_runners[project_dir] = {"dev_workspace": runner}
    return runner


def _seed_caches(wm_client, project_dir: pathlib.Path, action_name: str) -> None:
    ws_context = wm_client.ws_context
    ws_context.ws_action_schemas[project_dir] = {action_name: None}
    node_id = f"{project_dir.as_posix()}::pkg.{action_name}"
    ws_context.cached_actions_by_id[node_id] = context.CachedAction(
        action_id=node_id, project_path=project_dir, action_source=f"pkg.{action_name}"
    )
    ws_context.project_path_by_dir_and_action[(project_dir / "src").as_posix()] = {
        action_name: project_dir
    }


class _RecordedSteps:
    """Substitutes the two steps that need real processes, and records them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, pathlib.Path]] = []
        self.actions_after: dict[pathlib.Path, list[str]] = {}
        self.fail_on: str | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch, ws_context) -> None:
        async def _resolve_config(projects, ws_context, **kwargs) -> None:
            project_dir = projects[0].dir_path
            self.calls.append(("resolve_config", project_dir))
            if self.fail_on == "resolve_config":
                raise config_reload_service.runner_manager.RunnerFailedToStart(
                    "dev_workspace runner did not start"
                )
            # Stands in for the re-read: the project is replaced by what the
            # configuration on disk now says it is.
            new_actions = self.actions_after.get(project_dir)
            if new_actions is not None:
                ws_context.ws_projects[project_dir] = _project(project_dir, new_actions)
            ws_context.ws_projects_raw_configs[project_dir] = {"tool": {"finecode": {}}}

        async def _replace_runners(runner_working_dir_path, ws_context) -> None:
            self.calls.append(("replace_runners", runner_working_dir_path))
            if self.fail_on == "replace_runners":
                raise config_reload_service.runner_manager.RunnerFailedToStart(
                    "runner did not come back up"
                )

        monkeypatch.setattr(
            config_reload_service.runner_start_service,
            "start_runners_with_auto_prepare",
            _resolve_config,
        )
        monkeypatch.setattr(
            config_reload_service.runner_manager,
            "restart_extension_runners",
            _replace_runners,
        )


@pytest.fixture
def recovery_steps(wm_client, monkeypatch: pytest.MonkeyPatch) -> _RecordedSteps:
    steps = _RecordedSteps()
    steps.install(monkeypatch, wm_client.ws_context)
    return steps


async def test_config_is_resolved_before_runners_are_replaced(
    wm_client, recovery_steps, tmp_path
) -> None:
    """Configuration is re-read while the project's runners are still up.

    Resolving a preset asks a running runner where the preset package lives, so a
    recovery that replaced the runners first would be unable to resolve presets
    for any project that has them — which here is nearly every project.
    """
    _seed_project(wm_client, tmp_path, ["lint"])

    await wm_client.request("workspace/reloadConfig", {"project": str(tmp_path)})

    assert [name for name, _ in recovery_steps.calls] == [
        "resolve_config",
        "replace_runners",
    ]


async def test_recovering_one_project_leaves_another_untouched(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC7 — recovering one project leaves every other project's runner
    processes and cached action routing exactly as they were.

    A recovery that reached wider than asked would restart runners someone else
    is using, and a caller could not tell from the result that it had.
    """
    project_a, project_b = tmp_path / "a", tmp_path / "b"
    _seed_project(wm_client, project_a, ["lint"])
    runner_b = _seed_project(wm_client, project_b, ["format"])
    _seed_caches(wm_client, project_a, "lint")
    _seed_caches(wm_client, project_b, "format")
    # A directory outside both projects that has resolved actions to each of them.
    shared_dir = (tmp_path / "shared").as_posix()
    wm_client.ws_context.project_path_by_dir_and_action[shared_dir] = {
        "lint": project_a,
        "format": project_b,
    }

    await wm_client.request("workspace/reloadConfig", {"project": str(project_a)})

    assert recovery_steps.calls == [
        ("resolve_config", project_a),
        ("replace_runners", project_a),
    ]

    ws_context = wm_client.ws_context
    runner_b_after = ws_context.ws_projects_extension_runners[project_b][
        "dev_workspace"
    ]
    assert runner_b_after is runner_b
    assert runner_b_after.status is runner_client.RunnerStatus.RUNNING

    assert project_a not in ws_context.ws_action_schemas
    assert project_b in ws_context.ws_action_schemas
    assert [
        cached.project_path for cached in ws_context.cached_actions_by_id.values()
    ] == [project_b]
    assert sorted(ws_context.project_path_by_dir_and_action) == sorted(
        [(project_b / "src").as_posix(), shared_dir]
    )
    assert ws_context.project_path_by_dir_and_action[shared_dir] == {
        "format": project_b
    }


async def test_recovery_reports_the_actions_it_added_and_removed(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC7 — the result names the actions the recovery introduced and
    dropped, so a caller can see what the edit on disk actually changed."""
    _seed_project(wm_client, tmp_path, ["lint", "gone"])
    recovery_steps.actions_after[tmp_path] = ["lint", "added"]

    result = await wm_client.request(
        "workspace/reloadConfig", {"project": str(tmp_path)}
    )

    assert result["projects"] == [
        {
            "project": str(tmp_path),
            "status": "recovered",
            "actionsAdded": ["added"],
            "actionsRemoved": ["gone"],
        }
    ]


async def test_recovery_waits_for_an_initialization_of_the_same_project(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC12 — a recovery does not run against an initialization of the
    same project; it waits for the lock that addDir and startRunners hold.

    Both replace the project's configuration and its runners, so overlapping them
    would leave the project holding one operation's configuration and the other's
    runners.
    """
    _seed_project(wm_client, tmp_path, ["lint"])
    ws_context = wm_client.ws_context
    init_lock = asyncio.Lock()
    ws_context.project_init_locks[tmp_path] = init_lock

    await init_lock.acquire()
    recovery = asyncio.create_task(
        wm_client.request("workspace/reloadConfig", {"project": str(tmp_path)})
    )
    await asyncio.sleep(0.05)

    assert recovery_steps.calls == [], "recovery started while init held the lock"
    assert tmp_path in ws_context.ws_projects_raw_configs

    init_lock.release()
    result = await recovery

    assert result["projects"][0]["status"] == "recovered"
    assert [name for name, _ in recovery_steps.calls] == [
        "resolve_config",
        "replace_runners",
    ]


async def test_failed_recovery_leaves_the_previous_configuration_in_effect(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC12 — a recovery that fails part way leaves the project with the
    configuration it already had, never with none at all.

    The re-read begins by dropping the configuration in effect, so a failure
    between the two would otherwise leave the project unusable by an operation
    that was supposed to repair it.
    """
    _seed_project(wm_client, tmp_path, ["lint"])
    config_before = wm_client.ws_context.ws_projects_raw_configs[tmp_path]
    recovery_steps.fail_on = "resolve_config"

    result = await wm_client.request(
        "workspace/reloadConfig", {"project": str(tmp_path)}
    )

    entry = result["projects"][0]
    assert entry["status"] == "failed"
    assert "did not start" in entry["error"]
    assert wm_client.ws_context.ws_projects_raw_configs[tmp_path] == config_before


async def test_a_cancelled_recovery_leaves_the_previous_configuration_in_effect(
    wm_client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """PRD-0008-AC12 holds for a recovery that was cancelled, not only one that
    failed.

    The caller disconnecting cancels the request, which is not an error anyone
    reports — and leaves the project just as empty as the failure the restore
    was written for. That is why the restore belongs in a ``finally`` rather
    than on the exception limbs.
    """
    _seed_project(wm_client, tmp_path, ["lint"])
    ws_context = wm_client.ws_context
    config_before = ws_context.ws_projects_raw_configs[tmp_path]

    async def _cancelled(projects, ws_context, **kwargs) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        config_reload_service.runner_start_service,
        "start_runners_with_auto_prepare",
        _cancelled,
    )

    with pytest.raises(asyncio.CancelledError):
        await config_reload_service.reload_config(
            ws_context=ws_context, project_dir=tmp_path
        )

    assert ws_context.ws_projects_raw_configs[tmp_path] == config_before


async def test_a_failed_recovery_names_the_environment_that_needs_preparing(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC14 — the next step a failed config recovery reports names a real
    environment.

    A config recovery targets a whole project, so it has no single environment
    in hand the way a runner restart does; it has to take the one the failure
    was actually about. Interpolating what it does not have reads as an
    environment literally called 'None', which sends the caller looking for
    something that does not exist.
    """
    _seed_project(wm_client, tmp_path, ["lint"])
    wm_client.ws_context.ws_projects_extension_runners[tmp_path][
        "dev_workspace"
    ].status = runner_client.RunnerStatus.NO_VENV
    recovery_steps.fail_on = "resolve_config"

    result = await wm_client.request(
        "workspace/reloadConfig", {"project": str(tmp_path)}
    )

    next_step = result["projects"][0]["nextStep"]
    assert "None" not in next_step
    assert "--env=dev_workspace" in next_step
    assert "'dev_workspace' environment" in next_step


async def test_reload_config_without_a_target_is_rejected(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC15 — a recovery that states no target is rejected, and no
    project is recovered.

    Omitting an optional parameter is the default behaviour of a caller filling a
    schema it was given no reason to fill; if that meant the whole workspace,
    every runner in it would be replaced by accident.
    """
    _seed_project(wm_client, tmp_path, ["lint"])

    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request("workspace/reloadConfig", {})

    error = exc_info.value.args[0]
    assert error["code"] == -32602
    assert "project" in error["message"] and "allProjects" in error["message"]
    assert recovery_steps.calls == []
    assert tmp_path in wm_client.ws_context.ws_projects_raw_configs


async def test_reload_config_with_two_targets_is_rejected(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC15 — a recovery that states both a project and the whole
    workspace is rejected, and no project is recovered.

    Either reading of the two would be a guess, and the caller would read the
    success of that guess as confirmation of the scope they meant.
    """
    _seed_project(wm_client, tmp_path, ["lint"])

    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request(
            "workspace/reloadConfig", {"project": str(tmp_path), "allProjects": True}
        )

    error = exc_info.value.args[0]
    assert error["code"] == -32602
    assert recovery_steps.calls == []
    assert tmp_path in wm_client.ws_context.ws_projects_raw_configs


async def test_all_projects_recovers_every_configured_project(
    wm_client, recovery_steps, tmp_path
) -> None:
    """PRD-0008-AC7 — asking for the whole workspace returns one result per
    project, so a recovery that failed for one of them stays attributable."""
    project_a, project_b = tmp_path / "a", tmp_path / "b"
    _seed_project(wm_client, project_a, ["lint"])
    _seed_project(wm_client, project_b, ["format"])

    result = await wm_client.request("workspace/reloadConfig", {"allProjects": True})

    assert {entry["project"] for entry in result["projects"]} == {
        str(project_a),
        str(project_b),
    }
    assert {entry["status"] for entry in result["projects"]} == {"recovered"}
