"""Lifecycle integration of the resolution gate (B rules 8-10).

Config recovery, ``removeDir`` and shutdown all interact with in-flight
resolutions: recovery waits on them and registers a marker gates wait on, a
removed project leaves no resolution trace, and shutdown cancels batch tasks
before sweeping runners.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from resolution_fake import SwappingResolver

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._workspace import _handle_remove_dir
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import (
    config_reload_service,
    shutdown_service,
)
from finecode.wm_server.services import (
    project_resolution_service as prs,
)
from finecode.wm_server.testing import make_running_runner


def _make_collected(path: pathlib.Path) -> domain.CollectedProject:
    return domain.CollectedProject(
        name=path.name,
        dir_path=path,
        def_path=path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[],
        services=[],
        action_handler_configs={},
    )


def _make_context(path: pathlib.Path) -> context.WorkspaceContext:
    ws_context = context.WorkspaceContext(ws_dirs_paths=[path])
    ws_context.ws_projects[path] = _make_collected(path)
    return ws_context


async def _pump_until(predicate: object, *, tries: int = 50) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_gate_joins_a_recovery_in_progress(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request for an unresolved project mid-recovery waits for the recovery
    and starts no resolution of its own; it gets the object the recovery
    resolved (AC15)."""
    a = tmp_path / "a"
    ws_context = _make_context(a)
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)

    recovery = asyncio.create_task(
        config_reload_service.reload_config(
            ws_context, project_dir=a, rescan=False, kill_in_flight_runs=False
        )
    )
    # Recovery registered its marker and is blocked inside its start.
    await _pump_until(lambda: a in ws_context.project_resolution_tasks)

    gate = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await asyncio.sleep(0)
    assert not gate.done()

    block.set()
    await recovery
    outcome = await gate

    # Only the recovery's own start happened — the gate made no call.
    assert resolver.calls == [[a]]
    assert isinstance(outcome.resolved[a], domain.ResolvedProject)


async def test_gate_reports_and_remembers_a_failed_recovery(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the recovery the gate joined fails, the gate reports the recovery's
    message, and the failure is remembered for the next request (AC15)."""
    a = tmp_path / "a"
    ws_context = _make_context(a)
    block = asyncio.Event()
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        block=block,
        fail_paths={a},
        fail_message="config broke",
    )

    recovery = asyncio.create_task(
        config_reload_service.reload_config(
            ws_context, project_dir=a, rescan=False, kill_in_flight_runs=False
        )
    )
    await _pump_until(lambda: a in ws_context.project_resolution_tasks)

    gate = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await asyncio.sleep(0)

    block.set()
    await recovery
    outcome = await gate

    assert resolver.calls == [[a]]
    assert a in outcome.failed
    assert "config broke" in outcome.failed[a]
    assert ws_context.project_resolution_failures[a] == "config broke"


async def test_recovery_skips_restart_when_no_runner_was_live(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery of a project that had no live dev_workspace runner restarts
    nothing: the runners it leaves behind were started from the re-read config
    (AC15)."""
    a = tmp_path / "a"
    ws_context = _make_context(a)
    resolver = SwappingResolver(ws_context, monkeypatch)
    restarted: list[pathlib.Path] = []

    async def _recording_restart(
        *,
        runner_working_dir_path: pathlib.Path,
        ws_context: context.WorkspaceContext,
        **kwargs: object,
    ) -> None:
        restarted.append(runner_working_dir_path)

    monkeypatch.setattr(runner_manager, "restart_extension_runners", _recording_restart)

    result = await config_reload_service.reload_config(
        ws_context, project_dir=a, rescan=False, kill_in_flight_runs=False
    )

    assert result[0]["status"] == "recovered"
    assert restarted == []
    assert resolver.calls == [[a]]


async def test_recovery_of_resolved_project_registers_no_marker_and_restarts(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery of a resolved project with a live runner restarts the runners
    and registers no marker — a concurrent gate request proceeds as at HEAD."""
    a = tmp_path / "a"
    ws_context = _make_context(a)
    ws_context.ws_projects[a] = domain.ResolvedProject.from_collected(
        _make_collected(a)
    )
    ws_context.ws_projects_extension_runners[a] = {
        "dev_workspace": make_running_runner(
            working_dir_path=a, env_name="dev_workspace"
        )
    }
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)
    restarted: list[pathlib.Path] = []

    async def _recording_restart(
        *,
        runner_working_dir_path: pathlib.Path,
        ws_context: context.WorkspaceContext,
        **kwargs: object,
    ) -> None:
        restarted.append(runner_working_dir_path)

    monkeypatch.setattr(runner_manager, "restart_extension_runners", _recording_restart)

    recovery = asyncio.create_task(
        config_reload_service.reload_config(
            ws_context, project_dir=a, rescan=False, kill_in_flight_runs=False
        )
    )
    # No marker registered while the recovery's start is blocked.
    await _pump_until(lambda: len(resolver.calls) == 1)
    assert a not in ws_context.project_resolution_tasks

    block.set()
    result = await recovery

    assert result[0]["status"] == "recovered"
    assert restarted == [a]


async def test_recovery_waits_for_in_flight_batch_and_clears_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery waits for the project's in-flight resolution before re-reading
    the config, and clears its remembered failure (AC15)."""
    a = tmp_path / "a"
    ws_context = _make_context(a)
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)

    gate = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await _pump_until(lambda: len(resolver.calls) == 1)
    # Seed the remembered failure while the batch is in flight — if it were
    # present before the gate call, the gate would serve it without a batch.
    ws_context.project_resolution_failures[a] = "old failure"

    recovery = asyncio.create_task(
        config_reload_service.reload_config(
            ws_context, project_dir=a, rescan=False, kill_in_flight_runs=False
        )
    )
    await asyncio.sleep(0)
    # The recovery is waiting on the in-flight batch: it has not reached its
    # own start (which follows the raw-config pop).
    assert not recovery.done()
    assert len(resolver.calls) == 1

    block.set()
    await recovery
    await gate

    assert a not in ws_context.project_resolution_failures


async def test_remove_dir_during_in_flight_resolution_leaves_no_trace(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project removed while its resolution is in flight is dropped from the
    workspace, its raw config and the failure memory; the waiting gate reports
    it is not a project (AC17)."""
    a = tmp_path / "a"
    ws_context = _make_context(a)
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)

    gate = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await _pump_until(lambda: len(resolver.calls) == 1)
    # Seed the failure memory while the batch is in flight, so removeDir has
    # an entry to drop (a pre-seeded entry would make the gate serve it
    # without starting the batch this test is built around).
    ws_context.project_resolution_failures[a] = "stale"

    await _handle_remove_dir({"dirPath": str(a)}, ws_context)

    block.set()
    outcome = await gate

    assert a not in ws_context.ws_projects
    assert a not in ws_context.ws_projects_raw_configs
    assert a not in ws_context.project_resolution_failures
    assert a in outcome.failed
    assert "not a project in this workspace" in outcome.failed[a]


async def test_shutdown_cancels_batch_tasks_before_the_sweep(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancels and awaits in-flight resolution batches before the
    runner sweep snapshots statuses — a half-stopped batch must not confuse
    it (B rule 10)."""
    a = tmp_path / "a"
    ws_context = _make_context(a)
    ws_context.ws_projects_extension_runners[a] = {
        "dev": make_running_runner(working_dir_path=a, env_name="dev")
    }
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)

    gate = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await _pump_until(lambda: len(resolver.calls) == 1)
    batch = ws_context.project_resolution_tasks[a]

    sweep_saw_done: list[bool] = []

    async def _recording_stop(
        runner: object, ws_context: context.WorkspaceContext, **kwargs: object
    ) -> None:
        sweep_saw_done.append(batch.done())

    monkeypatch.setattr(runner_manager, "stop_extension_runner", _recording_stop)

    await shutdown_service.on_shutdown(ws_context)

    assert sweep_saw_done == [True]
    assert batch.done()
    assert resolver.calls == [[a]]
    with pytest.raises(asyncio.CancelledError):
        await gate
