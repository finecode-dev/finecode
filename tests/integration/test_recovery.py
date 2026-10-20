"""PRD-0008 — in-process integration tests for the recovery operations
(``runners/restart``, ``actions/reload``).

Drives the real dispatch loop over a real TCP loopback connection (see
``tests/integration/conftest.py``); no subprocess, no ``tests/e2e/``.
"""

from __future__ import annotations

import pathlib

import pytest

import finecode_jsonrpc as jsonrpc_client
from finecode.wm_server import domain
from finecode.wm_server.runner import runner_client


class _StubErClient:
    """Stands in for the JSON-RPC channel to a live Extension Runner.

    A reload that succeeds is by definition a request answered by a running ER
    process, which the in-process harness has none of.  Only the transport is
    stubbed: the handler, its dispatch, and the runner state it reads are real.
    """

    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure
        self.requests: list[str] = []

    async def send_request(
        self, method: str, params: object, timeout: float | None = None
    ) -> None:
        self.requests.append(method)
        if self._failure is not None:
            raise self._failure


def _seed_project_with_action(
    wm_client, project_dir: pathlib.Path, action_source: str
) -> domain.CollectedProject:
    project = domain.CollectedProject(
        name=project_dir.name,
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[
            domain.Action(name="lint", source=action_source, handlers=[], config={})
        ],
        services=[],
        action_handler_configs={},
    )
    wm_client.ws_context.ws_projects[project_dir] = project
    return project


def _seed_started_runner(
    wm_client,
    project_dir: pathlib.Path,
    env_name: str,
    failure: Exception | None = None,
    status: runner_client.RunnerStatus = runner_client.RunnerStatus.RUNNING,
) -> runner_client.ExtensionRunnerInfo:
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=project_dir,
        env_name=env_name,
        status=status,
        client=_StubErClient(failure),
    )
    runner.initialized_event.set()
    wm_client.ws_context.ws_projects_extension_runners.setdefault(project_dir, {})[
        env_name
    ] = runner
    return runner


def _seed_runner(
    wm_client, project_dir: pathlib.Path, env_name: str
) -> runner_client.ExtensionRunnerInfo:
    ws_context = wm_client.ws_context
    ws_context.ws_projects[project_dir] = domain.Project(
        name=project_dir.name,
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
    )
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=project_dir,
        env_name=env_name,
        status=runner_client.RunnerStatus.EXITED,
    )
    ws_context.ws_projects_extension_runners[project_dir] = {env_name: runner}
    return runner


async def test_restart_unknown_project_is_an_error(wm_client, tmp_path) -> None:
    """PRD-0008-AC5 — restarting a runner of a project the workspace does not
    know is reported as an error.

    A caller that mistyped the path must be able to tell that apart from a
    restart that worked; reporting success would leave them debugging against a
    process that was never touched.
    """
    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request(
            "runners/restart",
            {"project": str(tmp_path / "not-a-project"), "env": "dev"},
        )

    error = exc_info.value.args[0]
    assert error["code"] == -32602
    assert "not-a-project" in error["message"]


async def test_restart_unknown_env_is_an_error(wm_client, tmp_path) -> None:
    """PRD-0008-AC5 — restarting an environment the project does not have is
    reported as an error, even though the project itself exists.

    This is the likelier typo of the two: the project resolves, so a silent
    no-op would look exactly like a successful restart.
    """
    _seed_runner(wm_client, tmp_path, "dev")

    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request(
            "runners/restart",
            {"project": str(tmp_path), "env": "no_such_env"},
        )

    error = exc_info.value.args[0]
    assert error["code"] == -32602
    assert "no_such_env" in error["message"]


async def test_reload_action_of_unknown_project_is_an_error(
    wm_client, tmp_path
) -> None:
    """PRD-0008-AC5 — reloading an action of a project the workspace does not
    know is reported as an error rather than as a successful reload."""
    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request(
            "actions/reload",
            {"action": "some.Action", "project": str(tmp_path / "not-a-project")},
        )

    error = exc_info.value.args[0]
    assert error["code"] == -32602


async def test_restart_reports_runner_that_did_not_come_back(
    wm_client, tmp_path
) -> None:
    """PRD-0008-AC6 — a runner that cannot come back up (here: its environment
    does not exist) is reported as such instead of being reported as restarted,
    and the report carries the state it stopped in.

    That state is the difference between "prepare this environment" and "read
    this traceback": a caller given only an error message cannot tell which of
    the two it is looking at.

    The success limb is PRD-0008-AC16 and needs a real ER process; it lives in
    tests/e2e/.
    """
    _seed_runner(wm_client, tmp_path, "dev")

    result = await wm_client.request(
        "runners/restart", {"project": str(tmp_path), "env": "dev"}
    )

    assert result["restarted"] == []
    assert [(entry["project"], entry["env"]) for entry in result["failed"]] == [
        (str(tmp_path), "dev")
    ]
    assert result["failed"][0]["error"]

    runner = wm_client.ws_context.ws_projects_extension_runners[tmp_path]["dev"]
    assert runner.status is runner_client.RunnerStatus.NO_VENV
    assert result["failed"][0]["status"] == runner.status.name


async def test_restart_names_the_command_that_would_fix_the_environment(
    wm_client, tmp_path
) -> None:
    """PRD-0008-AC14 — when a recovery fails because the environment is missing,
    the report names the command that would make it succeed.

    The alternative is what the runner said, which is an import error naming a
    module the caller never heard of — true, and useless for deciding what to do
    next.
    """
    _seed_runner(wm_client, tmp_path, "dev")

    result = await wm_client.request(
        "runners/restart", {"project": str(tmp_path), "env": "dev"}
    )

    next_step = result["failed"][0]["nextStep"]
    assert "prepare-envs" in next_step
    assert "--env=dev" in next_step
    assert tmp_path.name in next_step


async def test_no_next_step_is_offered_for_an_unrelated_failure(
    wm_client, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD-0008-AC14 — a failure that is not an environment problem carries no
    next step.

    Telling a caller to reinstall an environment that was never the cause sends
    them to spend minutes on the wrong thing, and teaches them to ignore the
    field.
    """
    from finecode.wm_server.runner import runner_manager

    _seed_runner(wm_client, tmp_path, "dev")

    async def _crash(runner_working_dir_path, env_name, ws_context, debug=False):
        # A runner that starts and then dies on its own code, not on its env.
        ws_context.ws_projects_extension_runners[runner_working_dir_path][
            env_name
        ].status = runner_client.RunnerStatus.FAILED
        raise runner_manager.RunnerFailedToStart("handler raised on import")

    monkeypatch.setattr(runner_manager, "restart_extension_runner", _crash)

    result = await wm_client.request(
        "runners/restart", {"project": str(tmp_path), "env": "dev"}
    )

    entry = result["failed"][0]
    assert entry["status"] == "FAILED"
    assert "nextStep" not in entry


async def test_restart_without_a_target_is_rejected(wm_client, tmp_path) -> None:
    """PRD-0008-AC15 — a restart that states no target is rejected, and nothing
    is restarted.

    Omitting an optional parameter is what a caller filling an unfamiliar schema
    does by default; if that meant "the whole workspace", the most disruptive
    form of the operation would be the easiest one to reach by accident.
    """
    runner_before = _seed_runner(wm_client, tmp_path, "dev")

    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request("runners/restart", {})

    error = exc_info.value.args[0]
    assert error["code"] == -32602
    # The message must name both ways to state a target, not just refuse.
    assert "project" in error["message"] and "allProjects" in error["message"]

    runner_after = wm_client.ws_context.ws_projects_extension_runners[tmp_path]["dev"]
    assert runner_after is runner_before
    assert runner_after.status is runner_client.RunnerStatus.EXITED


async def test_restart_with_two_targets_is_rejected(wm_client, tmp_path) -> None:
    """PRD-0008-AC15 — a restart that states both a project and the whole
    workspace is rejected, and nothing is restarted.

    Either reading of the two would be a guess, and a caller who reads the
    success of a guess as confirmation of the scope they meant is exactly the
    silent-scope failure this addressing is meant to remove.
    """
    runner_before = _seed_runner(wm_client, tmp_path, "dev")

    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request(
            "runners/restart", {"project": str(tmp_path), "allProjects": True}
        )

    error = exc_info.value.args[0]
    assert error["code"] == -32602

    runner_after = wm_client.ws_context.ws_projects_extension_runners[tmp_path]["dev"]
    assert runner_after is runner_before
    assert runner_after.status is runner_client.RunnerStatus.EXITED


async def test_reload_action_rejects_an_environment(wm_client, tmp_path) -> None:
    """PRD-0008-AC15 — an action reload narrowed by an environment is rejected
    rather than silently applied workspace-wide.

    An action is reloaded in every environment of a project, so an environment
    narrows nothing; accepting it would let a caller read success as
    confirmation of a scope that never applied.
    """
    with pytest.raises(RuntimeError) as exc_info:
        await wm_client.request(
            "actions/reload",
            {"action": "some.Action", "project": str(tmp_path), "env": "dev"},
        )

    error = exc_info.value.args[0]
    assert error["code"] == -32602
    assert "env" in error["message"]


async def test_reload_action_reports_each_runner_it_did_not_reach(
    wm_client, tmp_path
) -> None:
    """PRD-0008-AC5 — one runner the reload cannot reach does not cancel the
    reload of the others, and each unreached runner is named.

    A caller who edits a handler and reloads must be told which environments now
    run the new code and which still run the old one. Failing the whole call
    would hide both: the reloads that did happen become invisible, and the one
    that did not is indistinguishable from them.
    """
    action_source = "some.package.LintAction"
    project_dir = tmp_path / "project"
    _seed_project_with_action(wm_client, project_dir, action_source)
    # Seeded ahead of the reachable one: an implementation that gives up on the
    # first failure would lose the reload that follows it.
    _seed_started_runner(
        wm_client,
        project_dir,
        "unreachable",
        failure=jsonrpc_client.ResponseTimeout("runner did not answer"),
    )
    _seed_started_runner(
        wm_client, project_dir, "stopped", status=runner_client.RunnerStatus.EXITED
    )
    reached = _seed_started_runner(wm_client, project_dir, "reached")

    result = await wm_client.request("actions/reload", {"action": action_source})

    assert result["reloaded"] == [{"project": str(project_dir), "envs": ["reached"]}]
    assert reached.client.requests, "the reachable runner was never asked to reload"

    failed_by_env = {entry["env"]: entry["error"] for entry in result["failed"]}
    assert set(failed_by_env) == {"unreachable", "stopped"}
    assert "did not answer" in failed_by_env["unreachable"]
    # A runner that is not running is skipped rather than reloaded, so saying it
    # was reloaded would be the silent no-op this criterion forbids.
    assert "EXITED" in failed_by_env["stopped"]
    assert all(entry["project"] == str(project_dir) for entry in result["failed"])
