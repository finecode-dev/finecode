"""PRD-0008 — in-process integration tests for refusing recovery while a run is
in flight (``workspace/reloadConfig``, ``runners/restart``).

Drives the real dispatch loop over a real TCP loopback connection (see
``tests/integration/conftest.py``); no subprocess, no ``tests/e2e/``.

The registry entries are created through the real tracker the run path uses, so
what is asserted is the same state a real run would leave behind. Replacing
runners needs operating-system processes and is substituted, as in
``test_config_recovery.py``.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from finecode.wm_server import domain
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services import config_reload_service, in_flight_runs
from finecode.wm_server.services.run_service import proxy_utils


def _seed_project(
    wm_client, project_dir: pathlib.Path
) -> runner_client.ExtensionRunnerInfo:
    ws_context = wm_client.ws_context
    ws_context.ws_projects[project_dir] = domain.CollectedProject(
        name=project_dir.name,
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[domain.Action(name="lint", source="pkg.Lint", handlers=[], config={})],
        services=[],
        action_handler_configs={},
    )
    ws_context.ws_projects_raw_configs[project_dir] = {"tool": {"finecode": {}}}
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=project_dir,
        env_name="dev_workspace",
        status=runner_client.RunnerStatus.RUNNING,
    )
    ws_context.ws_projects_extension_runners[project_dir] = {"dev_workspace": runner}
    return runner


@pytest.fixture
def replaced_runners(monkeypatch: pytest.MonkeyPatch) -> list[pathlib.Path]:
    """Records the projects whose runners would have been replaced."""
    replaced: list[pathlib.Path] = []

    async def _resolve_config(projects, ws_context, **kwargs) -> None:
        return None

    async def _replace_runners(runner_working_dir_path, ws_context) -> None:
        replaced.append(runner_working_dir_path)

    async def _restart_one(
        runner_working_dir_path, env_name, ws_context, debug=False
    ) -> None:
        replaced.append(runner_working_dir_path)

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
    # The same module object the restart handler imports.
    monkeypatch.setattr(
        config_reload_service.runner_manager, "restart_extension_runner", _restart_one
    )
    return replaced


async def test_config_recovery_is_refused_while_a_run_is_in_flight(
    wm_client, replaced_runners, tmp_path
) -> None:
    """PRD-0008-AC17 — a configuration recovery of a project with a run in flight
    is refused, names the run it is waiting on, and replaces nothing.

    Recovery replaces the project's runners, so proceeding would kill the run
    mid-execution: its caller would see a transport error it cannot attribute,
    and for an action with side effects nobody could say whether they completed.
    """
    _seed_project(wm_client, tmp_path)

    async with in_flight_runs.track(
        wm_client.ws_context,
        run_id="run-7",
        action_name="test",
        project_path=tmp_path,
    ):
        result = await wm_client.request(
            "workspace/reloadConfig", {"project": str(tmp_path)}
        )

    entry = result["projects"][0]
    assert entry["status"] == "refused"
    # "Busy" would support none of retry, target elsewhere, or override.
    assert "test" in entry["error"] and "run-7" in entry["error"]
    assert [(run["runId"], run["action"]) for run in entry["inFlight"]] == [
        ("run-7", "test")
    ]
    assert replaced_runners == []


async def test_runner_restart_is_refused_while_a_run_is_in_flight(
    wm_client, replaced_runners, tmp_path
) -> None:
    """PRD-0008-AC17 — a runner restart is refused on the same terms.

    A restart kills a live run exactly as thoroughly as a configuration recovery
    does; refusing one rung and not the other would leave the cheaper rung as an
    unguarded way to do the same damage.
    """
    _seed_project(wm_client, tmp_path)

    async with in_flight_runs.track(
        wm_client.ws_context,
        run_id="run-9",
        action_name="test",
        project_path=tmp_path,
    ):
        result = await wm_client.request("runners/restart", {"project": str(tmp_path)})

    assert result["restarted"] == []
    assert result["failed"] == []
    assert [entry["project"] for entry in result["refused"]] == [str(tmp_path)]
    assert "run-9" in result["refused"][0]["error"]
    assert replaced_runners == []


async def test_refusal_is_scoped_to_the_project_with_the_run(
    wm_client, replaced_runners, tmp_path
) -> None:
    """PRD-0008-AC17 — a run in one project refuses only that project; the rest of
    a workspace-wide recovery still happens.

    An all-or-nothing refusal would make workspace recovery unusable in any
    session that is ever busy, which for an editor running background lints is
    all of them.
    """
    busy, idle = tmp_path / "busy", tmp_path / "idle"
    _seed_project(wm_client, busy)
    _seed_project(wm_client, idle)

    async with in_flight_runs.track(
        wm_client.ws_context, run_id="run-3", action_name="lint", project_path=busy
    ):
        result = await wm_client.request(
            "workspace/reloadConfig", {"allProjects": True}
        )

    status_by_project = {
        entry["project"]: entry["status"] for entry in result["projects"]
    }
    assert status_by_project == {str(busy): "refused", str(idle): "recovered"}
    assert replaced_runners == [idle]


async def test_override_proceeds_and_says_what_it_killed(
    wm_client, replaced_runners, tmp_path
) -> None:
    """PRD-0008-AC17 — a caller that explicitly accepts killing the run gets the
    recovery.

    A hung run is indistinguishable from a working one, and restarting its runner
    is the usual remedy — so a refusal with no way past it would leave the wedged
    run holding its own remedy hostage.
    """
    _seed_project(wm_client, tmp_path)

    async with in_flight_runs.track(
        wm_client.ws_context, run_id="run-1", action_name="test", project_path=tmp_path
    ):
        result = await wm_client.request(
            "workspace/reloadConfig",
            {"project": str(tmp_path), "killInFlightRuns": True},
        )

        assert result["projects"][0]["status"] == "recovered"
        assert replaced_runners == [tmp_path]
        # The killed run is forgotten: its own cleanup cannot be relied on, since
        # the process carrying it is gone.
        assert in_flight_runs.runs_in_project(wm_client.ws_context, tmp_path) == []


async def test_override_is_not_reached_by_omission(
    wm_client, replaced_runners, tmp_path
) -> None:
    """PRD-0008-AC17 — killing a run is never what omitting a parameter means.

    A caller filling an unfamiliar schema omits what it was given no reason to
    fill; if that meant "kill whatever is running", the destructive choice would
    be the default one.
    """
    _seed_project(wm_client, tmp_path)

    async with in_flight_runs.track(
        wm_client.ws_context, run_id="run-2", action_name="test", project_path=tmp_path
    ):
        for params in (
            {"project": str(tmp_path)},
            {"project": str(tmp_path), "killInFlightRuns": False},
        ):
            result = await wm_client.request("workspace/reloadConfig", params)
            assert result["projects"][0]["status"] == "refused"

    assert replaced_runners == []


async def test_the_run_path_registers_the_run_it_dispatches(
    wm_client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """PRD-0008-AC17 — a run registers itself for the whole of its dispatch.

    Refusal is only as good as the registry behind it: a run the WM dispatched
    without registering is one a recovery would kill while believing the project
    idle.
    """
    ws_context = wm_client.ws_context
    _seed_project(wm_client, tmp_path)
    project = domain.ResolvedProject.from_collected(ws_context.ws_projects[tmp_path])
    ws_context.ws_projects[tmp_path] = project

    seen_during_dispatch: list[list[str]] = []

    async def _execute_action(**kwargs):
        seen_during_dispatch.append(
            [
                run.action_name
                for run in in_flight_runs.runs_in_project(ws_context, tmp_path)
            ]
        )
        return runner_client.RunActionResponse(result_by_format={}, return_code=0)

    monkeypatch.setattr(proxy_utils, "_execute_action", _execute_action)

    await proxy_utils.run_action(
        action_name="lint",
        params={},
        project_def=project,
        ws_context=ws_context,
        run_trigger=runner_client.RunActionTrigger.USER,
        dev_env=runner_client.DevEnv.IDE,
    )

    assert seen_during_dispatch == [["lint"]]
    assert tmp_path not in ws_context.in_flight_runs


async def test_the_streaming_run_path_registers_the_run_it_dispatches(
    wm_client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """PRD-0008-AC17 — a streaming run registers itself for as long as it streams.

    This is the path every editor-driven lint, format and diagnostic takes:
    anything with a ``partialResultToken``. A recovery that saw only the
    non-streaming register would report itself successful over exactly the runs
    an editor keeps in flight, and the caller would get a transport failure it
    cannot attribute — the outcome refusal exists to remove.
    """
    ws_context = wm_client.ws_context
    _seed_project(wm_client, tmp_path)
    collected = ws_context.ws_projects[tmp_path]
    collected.actions = [
        domain.Action(
            name="lint",
            source="pkg.Lint",
            handlers=[
                domain.ActionHandler(
                    name="ruff",
                    source="pkg.RuffLint",
                    config={},
                    env="dev_workspace",
                    dependencies=[],
                )
            ],
            config={},
        )
    ]
    project = domain.ResolvedProject.from_collected(collected)
    ws_context.ws_projects[tmp_path] = project
    runner = ws_context.ws_projects_extension_runners[tmp_path]["dev_workspace"]

    seen_during_dispatch: list[list[str]] = []

    async def _get_or_start_runner(**kwargs):
        return runner

    async def _get_partial_results(result_list, partial_result_token, runner):
        await asyncio.Event().wait()

    async def _run_action_and_notify(**kwargs):
        seen_during_dispatch.append(
            [
                run.action_name
                for run in in_flight_runs.runs_in_project(ws_context, tmp_path)
            ]
        )
        return runner_client.RunActionResponse(result_by_format={}, return_code=0)

    monkeypatch.setattr(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        _get_or_start_runner,
    )
    monkeypatch.setattr(proxy_utils, "get_partial_results", _get_partial_results)
    monkeypatch.setattr(proxy_utils, "run_action_and_notify", _run_action_and_notify)

    async with proxy_utils.run_with_partial_results(
        action_name="lint",
        params={},
        partial_result_token="token-1",
        project_dir_path=tmp_path,
        run_trigger=runner_client.RunActionTrigger.USER,
        dev_env=runner_client.DevEnv.IDE,
        ws_context=ws_context,
    ) as ctx:
        async for _ in ctx.partials:
            pass

    assert seen_during_dispatch == [["lint"]]
    assert tmp_path not in ws_context.in_flight_runs


async def test_registry_entry_is_removed_when_the_run_fails(
    wm_client, tmp_path
) -> None:
    """A run that ends in failure leaves nothing behind in the in-flight registry.

    An entry that outlives its run refuses every future recovery of that project,
    silently and permanently — a worse failure than the one refusal prevents, and
    the failure path is where it is most likely to happen.
    """
    ws_context = wm_client.ws_context

    with pytest.raises(RuntimeError):
        async with in_flight_runs.track(
            ws_context, run_id="run-5", action_name="test", project_path=tmp_path
        ):
            assert in_flight_runs.runs_in_project(ws_context, tmp_path) != []
            raise RuntimeError("action failed in the runner")

    assert in_flight_runs.runs_in_project(ws_context, tmp_path) == []
    assert tmp_path not in ws_context.in_flight_runs


async def test_registry_entry_is_removed_when_the_run_is_cancelled(
    wm_client, tmp_path
) -> None:
    """A cancelled run leaves nothing behind either.

    ``asyncio.CancelledError`` is a ``BaseException``, so an ``except Exception``
    cleanup would miss it — which is why the removal is in a ``finally``.
    """
    ws_context = wm_client.ws_context

    with pytest.raises(asyncio.CancelledError):
        async with in_flight_runs.track(
            ws_context, run_id="run-6", action_name="test", project_path=tmp_path
        ):
            raise asyncio.CancelledError

    assert tmp_path not in ws_context.in_flight_runs
