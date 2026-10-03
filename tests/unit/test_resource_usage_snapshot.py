"""The resource-usage snapshot must be read-only, complete and never wait."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from finecode.wm_server import (
    context,
    domain,
    host_pressure,
    process_footprint,
    wm_server,
)
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services import event_loop_lag_monitor, resource_usage


@pytest.fixture(autouse=True)
def _reset_walk_flag():
    resource_usage._footprint_walk_running = False
    yield
    resource_usage._footprint_walk_running = False


def _make_runner(
    project_dir: Path, env_name: str, status, **kwargs
) -> runner_client.ExtensionRunnerInfo:
    return runner_client.ExtensionRunnerInfo(
        working_dir_path=project_dir,
        env_name=env_name,
        status=status,
        **kwargs,
    )


async def test_handler_returns_while_budget_and_startup_blocked() -> None:
    """The snapshot must never wait on the budget, a lease or a permit.

    With every work slot leased plus one parked waiter, every startup permit
    held plus one waiter, and the condition lock held, any of those waits
    would hit the timeout instead of returning.
    """
    ws_context = context.WorkspaceContext([])
    budget = ws_context.process_budget
    size = budget.size
    holders = [await budget.lease(f"holder-{i}", requested=1) for i in range(size)]
    waiter = asyncio.create_task(budget.lease("waiter", requested=1))
    await asyncio.sleep(0)
    assert not waiter.done()

    semaphore = ws_context.er_startup_semaphore
    acquired = 0
    while not semaphore.locked():
        await semaphore.acquire()
        acquired += 1
    startup_waiter = asyncio.create_task(semaphore.acquire())
    await asyncio.sleep(0)
    assert not startup_waiter.done()

    leases_before = set(budget._leases.keys())
    granted_before = budget.granted
    permits_during = semaphore._value
    try:
        await budget._condition.acquire()
        try:
            snapshot = await asyncio.wait_for(
                wm_server._handle_server_get_resource_usage({}, ws_context),
                1.0,
            )
            assert set(budget._leases.keys()) == leases_before
            assert budget.granted == granted_before
            assert semaphore._value == permits_during
        finally:
            budget._condition.release()
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        startup_waiter.cancel()
        await asyncio.gather(startup_waiter, return_exceptions=True)
        for _ in range(acquired):
            semaphore.release()
        for lease in holders:
            await budget.release(lease.lease_id)

    assert snapshot["workSlots"]["waiting"] == 1
    assert snapshot["workSlots"]["free"] == 0


async def test_consecutive_calls_agree_and_change_nothing(monkeypatch) -> None:
    """Two back-to-back snapshots must agree and leave no trace.

    Host- and time-derived fields are excluded by definition; the only writes
    allowed are idempotent peak stores.
    """
    ws_context = context.WorkspaceContext([])
    project_dir = Path("/ws/a")
    ws_context.ws_projects_extension_runners[project_dir] = {
        "e1": _make_runner(project_dir, "e1", domain.ExtensionRunnerStatus.RUNNING),
        "e2": _make_runner(
            project_dir, "e2", domain.ExtensionRunnerStatus.INITIALIZING
        ),
    }
    ws_context.in_flight_runs[project_dir] = {
        "run-1": domain.InFlightRun(
            run_id="run-1",
            action_name="lint",
            project_path=project_dir,
            started_at=100.0,
        )
    }

    def _fail(*args, **kwargs):
        raise AssertionError("must not be called")

    monkeypatch.setattr(
        "finecode.wm_server.services.process_budget.ProcessBudget.lease", _fail
    )
    monkeypatch.setattr("finecode.wm_server.services.in_flight_runs.track", _fail)
    monkeypatch.setattr("finecode.wm_server.runner.runner_manager._start_runner", _fail)
    monkeypatch.setattr("finecode.wm_server.runner.runner_client.run_action", _fail)

    runners_before = {
        path: dict(runners)
        for path, runners in ws_context.ws_projects_extension_runners.items()
    }
    flights_before = {
        path: dict(runs) for path, runs in ws_context.in_flight_runs.items()
    }

    first = await wm_server._handle_server_get_resource_usage({}, ws_context)
    second = await wm_server._handle_server_get_resource_usage({}, ws_context)

    for key in ("projects", "runners", "workSlots", "startupSlots", "inFlightRuns"):
        assert first[key] == second[key]
    for key in (
        "runnersRunning",
        "runnersStarting",
        "projectsActive",
        "workSlotsUsed",
        "workSlotsWaiting",
        "startupSlotsWaiting",
    ):
        assert first["peaks"][key] == second["peaks"][key]
    assert ws_context.ws_projects_extension_runners == runners_before
    assert ws_context.in_flight_runs == flights_before


async def test_snapshot_counts_active_and_splits_by_env() -> None:
    """Active runners must be counted and split per environment.

    An env with nothing running or active stays out of the table rather than
    cluttering it with zeros.
    """
    ws_context = context.WorkspaceContext([])
    project_dir = Path("/ws/a")
    ws_context.ws_projects_extension_runners[project_dir] = {
        "e1": _make_runner(
            project_dir,
            "e1",
            domain.ExtensionRunnerStatus.RUNNING,
            active_requests=1,
        ),
        "e2": _make_runner(
            project_dir,
            "e2",
            domain.ExtensionRunnerStatus.RUNNING,
            active_requests=2,
        ),
        "e3": _make_runner(
            project_dir,
            "e3",
            domain.ExtensionRunnerStatus.RUNNING,
            active_requests=0,
        ),
    }

    snapshot = await wm_server._handle_server_get_resource_usage({}, ws_context)

    assert snapshot["runners"]["active"] == 2
    assert snapshot["runners"]["byEnv"]["e1"]["active"] == 1
    assert snapshot["runners"]["byEnv"]["e2"]["active"] == 1
    assert "e3" in snapshot["runners"]["byEnv"]


async def test_snapshot_and_lag_monitor_share_counting(monkeypatch) -> None:
    """Both readers must go through the one counting implementation.

    A second status filter anywhere else would drift from the first the next
    time a status is added.
    """
    ws_context = context.WorkspaceContext([])
    calls = {"counts": 0, "budget": 0}
    real_counts = runner_counts_count = None
    import finecode.wm_server.runner.runner_counts as runner_counts_module

    real_counts = runner_counts_module.count_runners
    real_snapshot = ws_context.process_budget.snapshot

    def _counting_counts(ctx):
        calls["counts"] += 1
        return real_counts(ctx)

    def _counting_snapshot():
        calls["budget"] += 1
        return real_snapshot()

    monkeypatch.setattr(runner_counts_module, "count_runners", _counting_counts)
    monkeypatch.setattr(ws_context.process_budget, "snapshot", _counting_snapshot)

    resource_usage.build_snapshot(
        ws_context,
        resource_usage.WmProcessInfo(
            pid=1, uptime_sec=None, connected_clients=0, lag=None
        ),
    )
    assert calls == {"counts": 1, "budget": 1}
    event_loop_lag_monitor.snapshot_context(ws_context)
    assert calls == {"counts": 2, "budget": 2}


async def test_single_flight_second_request_refused_while_walking(
    monkeypatch,
) -> None:
    """Only one footprint walk may run at a time; the second fails fast.

    Without the guard, slow walks would pile threads in the executor.
    """
    ws_context = context.WorkspaceContext([])
    gate = threading.Event()
    walks = {"count": 0}
    try:

        def _blocking(wm_pid, _targets, **kwargs):
            walks["count"] += 1
            gate.wait(5.0)
            return process_footprint.Footprint(
                wm=process_footprint.TreeFootprint(
                    pid=wm_pid, process_count=1, rss_kb=100, swap_kb=10
                ),
                runners={},
                untracked=[],
            )

        monkeypatch.setattr(
            resource_usage.process_footprint, "read_footprint", _blocking
        )
        first = asyncio.create_task(
            wm_server._handle_server_get_resource_usage(
                {"includeProcesses": True}, ws_context
            )
        )
        while not resource_usage._footprint_walk_running:
            await asyncio.sleep(0.01)
        second = await asyncio.wait_for(
            wm_server._handle_server_get_resource_usage(
                {"includeProcesses": True}, ws_context
            ),
            1.0,
        )
        assert second["processes"] == {"error": "process walk already in progress"}
        gate.set()
        first_result = await first
        assert first_result["processes"]["wm"]["processCount"] == 1
        gate.clear()
    finally:
        gate.set()

    gate2 = threading.Event()
    try:

        def _counting(wm_pid, _targets, **kwargs):
            walks["count"] += 1
            return process_footprint.Footprint(
                wm=process_footprint.TreeFootprint(
                    pid=wm_pid, process_count=1, rss_kb=100, swap_kb=10
                ),
                runners={},
                untracked=[],
            )

        monkeypatch.setattr(
            resource_usage.process_footprint, "read_footprint", _counting
        )
        before = walks["count"]
        await wm_server._handle_server_get_resource_usage(
            {"includeProcesses": True}, ws_context
        )
        assert walks["count"] == before + 1
    finally:
        gate2.set()


async def test_single_flight_survives_cancellation(monkeypatch) -> None:
    """Cancelling a walk's request must not release the guard early.

    The thread outlives the request that started it, so releasing on
    cancellation would let a second walk pile onto the first.
    """
    ws_context = context.WorkspaceContext([])
    gate = threading.Event()
    try:

        def _blocking(wm_pid, _targets, **kwargs):
            gate.wait(5.0)
            return process_footprint.Footprint(
                wm=process_footprint.TreeFootprint(
                    pid=wm_pid, process_count=1, rss_kb=100, swap_kb=10
                ),
                runners={},
                untracked=[],
            )

        monkeypatch.setattr(
            resource_usage.process_footprint, "read_footprint", _blocking
        )
        first = asyncio.create_task(
            wm_server._handle_server_get_resource_usage(
                {"includeProcesses": True}, ws_context
            )
        )
        while not resource_usage._footprint_walk_running:
            await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = await asyncio.wait_for(
            wm_server._handle_server_get_resource_usage(
                {"includeProcesses": True}, ws_context
            ),
            1.0,
        )
        assert second["processes"] == {"error": "process walk already in progress"}
        gate.set()
        deadline = 100
        while resource_usage._footprint_walk_running and deadline > 0:
            await asyncio.sleep(0.01)
            deadline -= 1
        assert not resource_usage._footprint_walk_running
    finally:
        gate.set()


async def test_scheduling_failure_leaves_no_flag(monkeypatch) -> None:
    """A walk that was never scheduled must not wedge the guard.

    Otherwise every later opt-in request would fail without ever walking.
    """
    ws_context = context.WorkspaceContext([])
    loop = asyncio.get_running_loop()
    real_executor = loop.run_in_executor

    def _raise(*args, **kwargs):
        raise RuntimeError("executor shut down")

    monkeypatch.setattr(loop, "run_in_executor", _raise)
    snapshot = await wm_server._handle_server_get_resource_usage(
        {"includeProcesses": True}, ws_context
    )

    assert snapshot["processes"] == {"error": "RuntimeError: executor shut down"}
    assert resource_usage._footprint_walk_running is False
    _ = real_executor


async def test_in_flight_runs_sorted_oldest_first() -> None:
    """The likeliest stuck run is the oldest, so it comes first.

    A truncated or unordered list would hide exactly the run being looked for.
    """
    ws_context = context.WorkspaceContext([])
    project_dir = Path("/ws/a")
    ws_context.in_flight_runs[project_dir] = {
        "run-new": domain.InFlightRun(
            run_id="run-new",
            action_name="test",
            project_path=project_dir,
            started_at=200.0,
        ),
        "run-old": domain.InFlightRun(
            run_id="run-old",
            action_name="lint",
            project_path=project_dir,
            started_at=100.0,
        ),
    }

    snapshot = await wm_server._handle_server_get_resource_usage({}, ws_context)

    assert [run["runId"] for run in snapshot["inFlightRuns"]] == [
        "run-old",
        "run-new",
    ]


async def test_hook_failed_reports_stale_peaks() -> None:
    """A failed peak hook must be visible, not silent.

    Otherwise an operator cannot tell quiet peaks from peaks that stopped
    rising.
    """
    ws_context = context.WorkspaceContext([])
    first = await wm_server._handle_server_get_resource_usage({}, ws_context)
    assert first["peaks"]["hookFailed"] is False
    ws_context.resource_peaks.hook_failed = True
    second = await wm_server._handle_server_get_resource_usage({}, ws_context)
    assert second["peaks"]["hookFailed"] is True


async def test_stop_resets_started_at_and_lag_monitor() -> None:
    """A handler test after a start must still see null uptime.

    Stale globals would make a direct handler call report a server that is
    not running.
    """
    import time

    old_started = wm_server._started_at
    old_monitor = wm_server._lag_monitor
    old_task = wm_server._lag_monitor_task
    try:
        wm_server._started_at = time.monotonic() - 10.0
        wm_server._lag_monitor = event_loop_lag_monitor.EventLoopLagMonitor()
        wm_server.stop()
        assert wm_server._started_at is None
        assert wm_server._lag_monitor is None
        ws_context = context.WorkspaceContext([])
        snapshot = await wm_server._handle_server_get_resource_usage({}, ws_context)
        assert snapshot["wm"]["uptimeSec"] is None
    finally:
        wm_server._started_at = old_started
        wm_server._lag_monitor = old_monitor
        wm_server._lag_monitor_task = old_task


async def test_snapshot_has_every_key_and_nulls_when_host_missing(
    monkeypatch,
) -> None:
    """All-None host readers must produce nulls, never an error.

    Non-Linux hosts and containers without cgroup or PSI are normal, not
    failures.
    """
    ws_context = context.WorkspaceContext([])
    monkeypatch.setattr(
        host_pressure,
        "read_meminfo",
        lambda *a, **k: host_pressure.MemInfo(
            mem_total_mb=None,
            mem_available_mb=None,
            swap_total_mb=None,
            swap_used_mb=None,
        ),
    )
    monkeypatch.setattr(host_pressure, "read_cgroup_memory", lambda *a, **k: None)
    monkeypatch.setattr(
        host_pressure,
        "read_host_pressure",
        lambda *a, **k: host_pressure.HostPressure(
            mem_available_mb=None,
            swap_used_mb=None,
            psi_memory_full_avg10=None,
            psi_io_full_avg10=None,
            psi_cpu_some_avg10=None,
        ),
    )
    monkeypatch.setattr(
        event_loop_lag_monitor,
        "read_host_load",
        lambda *a, **k: event_loop_lag_monitor.HostLoad(load_1m=None, cpu_count=None),
    )

    snapshot = await wm_server._handle_server_get_resource_usage({}, ws_context)

    assert set(snapshot) == {
        "timestamp",
        "wm",
        "projects",
        "runners",
        "budget",
        "workSlots",
        "startupSlots",
        "inFlightRuns",
        "peaks",
        "host",
        "processes",
    }
    assert snapshot["host"]["memTotalMb"] is None
    assert snapshot["host"]["cgroup"] is None
    assert snapshot["host"]["psi"]["memoryFullAvg10"] is None
    assert snapshot["host"]["load1m"] is None
    assert snapshot["processes"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"includeProcesses": "yes"},
        {"lagWindowSec": 0},
        {"lagWindowSec": 601},
        {"lagWindowSec": float("nan")},
        {"lagWindowSec": True},
    ],
)
async def test_invalid_params_raise_value_error(params) -> None:
    """Out-of-range or mistyped params must fail as invalid params.

    A silently clamped window would report a lag maximum for a window the
    caller did not ask for.
    """
    ws_context = context.WorkspaceContext([])
    with pytest.raises(ValueError):
        await wm_server._handle_server_get_resource_usage(params, ws_context)


async def test_lag_window_echoed_with_monitor() -> None:
    """The snapshot must echo the window its lag maximum used."""
    ws_context = context.WorkspaceContext([])
    old_monitor = wm_server._lag_monitor
    try:
        wm_server._lag_monitor = event_loop_lag_monitor.EventLoopLagMonitor()
        snapshot = await wm_server._handle_server_get_resource_usage(
            {"lagWindowSec": 60}, ws_context
        )
        assert snapshot["wm"]["loopLagWindowSec"] == 60
    finally:
        wm_server._lag_monitor = old_monitor


async def test_include_processes_error_and_not_called_without_flag(
    monkeypatch,
) -> None:
    """The opt-in walk reports its own failure and never runs unasked.

    Without the flag the snapshot must not pay for a walk at all.
    """
    ws_context = context.WorkspaceContext([])
    calls = {"count": 0}

    def _raise(_wm_pid, _targets, **kwargs):
        calls["count"] += 1
        raise RuntimeError("walk boom")

    monkeypatch.setattr(resource_usage.process_footprint, "read_footprint", _raise)
    with_flag = await wm_server._handle_server_get_resource_usage(
        {"includeProcesses": True}, ws_context
    )
    assert with_flag["processes"] == {"error": "RuntimeError: walk boom"}
    assert calls["count"] == 1
    without_flag = await wm_server._handle_server_get_resource_usage({}, ws_context)
    assert without_flag["processes"] is None
    assert calls["count"] == 1


async def test_footprint_runs_off_the_loop(monkeypatch) -> None:
    """The walk must not run on the loop thread.

    Thousands of /proc reads on the loop would stall the server the snapshot
    is trying to observe.
    """
    ws_context = context.WorkspaceContext([])
    main_ident = threading.get_ident()
    seen: dict = {}

    def _record(wm_pid, _targets, **kwargs):
        seen["ident"] = threading.get_ident()
        return process_footprint.Footprint(
            wm=process_footprint.TreeFootprint(
                pid=wm_pid, process_count=0, rss_kb=0, swap_kb=0
            ),
            runners={},
            untracked=[],
        )

    monkeypatch.setattr(resource_usage.process_footprint, "read_footprint", _record)
    await wm_server._handle_server_get_resource_usage(
        {"includeProcesses": True}, ws_context
    )

    assert seen["ident"] != main_ident


def _patch_pressured_host(monkeypatch, *, psi_full: float) -> None:
    monkeypatch.setattr(
        host_pressure,
        "read_meminfo",
        lambda *a, **k: host_pressure.MemInfo(
            mem_total_mb=17920,
            mem_available_mb=545,
            swap_total_mb=20728,
            swap_used_mb=20727,
        ),
    )
    monkeypatch.setattr(host_pressure, "read_cgroup_memory", lambda *a, **k: None)
    monkeypatch.setattr(
        host_pressure,
        "read_host_pressure",
        lambda *a, **k: host_pressure.HostPressure(
            mem_available_mb=545,
            swap_used_mb=20727,
            psi_memory_full_avg10=psi_full,
            psi_io_full_avg10=1.25,
            psi_cpu_some_avg10=0.18,
        ),
    )
    monkeypatch.setattr(
        event_loop_lag_monitor,
        "read_host_load",
        lambda *a, **k: event_loop_lag_monitor.HostLoad(load_1m=None, cpu_count=None),
    )


async def test_snapshot_reports_memory_pressure_when_pressured(monkeypatch) -> None:
    """A pressured host must be visible in the snapshot the CLI polls.

    Without the verdict in the snapshot the reporter could not warn and a
    run failure would carry no host context to explain it.
    """
    ws_context = context.WorkspaceContext([])
    _patch_pressured_host(monkeypatch, psi_full=76.91)

    snapshot = await wm_server._handle_server_get_resource_usage({}, ws_context)

    assert snapshot["host"]["memoryPressure"] == {
        "active": True,
        "reasons": ["psi", "memoryExhausted"],
    }


async def test_snapshot_reports_no_pressure_verdict_without_inputs(
    monkeypatch,
) -> None:
    """A host with no readings must abstain rather than claim it is fine."""
    ws_context = context.WorkspaceContext([])
    monkeypatch.setattr(
        host_pressure,
        "read_meminfo",
        lambda *a, **k: host_pressure.MemInfo(
            mem_total_mb=None,
            mem_available_mb=None,
            swap_total_mb=None,
            swap_used_mb=None,
        ),
    )
    monkeypatch.setattr(host_pressure, "read_cgroup_memory", lambda *a, **k: None)
    monkeypatch.setattr(
        host_pressure,
        "read_host_pressure",
        lambda *a, **k: host_pressure.HostPressure(
            mem_available_mb=None,
            swap_used_mb=None,
            psi_memory_full_avg10=None,
            psi_io_full_avg10=None,
            psi_cpu_some_avg10=None,
        ),
    )
    monkeypatch.setattr(
        event_loop_lag_monitor,
        "read_host_load",
        lambda *a, **k: event_loop_lag_monitor.HostLoad(load_1m=None, cpu_count=None),
    )

    snapshot = await wm_server._handle_server_get_resource_usage({}, ws_context)

    assert snapshot["host"]["memoryPressure"] is None
    assert snapshot["peaks"]["hostPsiMemoryFullMax"] is None


async def test_snapshot_psi_peak_keeps_maximum(monkeypatch) -> None:
    """The PSI peak must survive a later calmer sample.

    A max that a low reading could lower would hide the worst of an episode
    that has already passed.
    """
    ws_context = context.WorkspaceContext([])
    _patch_pressured_host(monkeypatch, psi_full=40.0)
    first = await wm_server._handle_server_get_resource_usage({}, ws_context)
    assert first["peaks"]["hostPsiMemoryFullMax"] == 40.0

    _patch_pressured_host(monkeypatch, psi_full=12.0)
    second = await wm_server._handle_server_get_resource_usage({}, ws_context)
    assert second["peaks"]["hostPsiMemoryFullMax"] == 40.0
