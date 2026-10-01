"""The WM reports its own event-loop starvation instead of leaving an operator
to infer it from an ER-side RPC timeout.

When the loop is starved, the WM records the lag together with the runner,
budget and in-flight-run state that says which kind of pressure caused it.  The
records must stay bounded (one warning per stall per cooldown, one recovery
line when it ends) and silent on a healthy loop, or the signal that makes the
next starvation report attributable becomes noise nobody reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from loguru import logger

from finecode.wm_server import context, domain, host_pressure, wm_server
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services import event_loop_lag_monitor

_WARNING_MESSAGE = "WM event loop lag exceeded threshold"
_RECOVERY_MESSAGE = "WM event loop lag recovered"

_CONTEXT_FIELDS = (
    "lag_ms",
    "threshold_ms",
    "runners_starting",
    "runners_running",
    "budget_granted",
    "budget_total",
    "in_flight_runs",
    "window_ms",
    "wm_cpu_ms",
    "wm_cpu_pct",
    "voluntary_switches",
    "involuntary_switches",
    "load_1m",
    "cpu_count",
    "ready_callbacks",
)


@contextlib.contextmanager
def _capture(level: str = "INFO"):
    records: list[dict] = []
    sink_id = logger.add(lambda message: records.append(message.record), level=level)
    try:
        yield records
    finally:
        logger.remove(sink_id)


async def test_warns_with_lag_and_context_after_a_synthetic_loop_block() -> None:
    """A block of the loop past the threshold must leave a WM-side warning.

    Without it, starvation is only visible as a timeout on whatever other
    process happened to be talking to the WM, with no evidence in the WM's own
    log of when it started or what was running at the time.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        interval_sec=0.02, threshold_sec=0.05, cooldown_sec=10.0
    )

    with _capture(level="WARNING") as records:
        task = asyncio.create_task(monitor.run(ws_context))
        try:
            # Let the monitor take on-time samples first, so the block below is
            # unambiguously what made a later sample late rather than the task's
            # own first scheduling.
            await asyncio.sleep(0.1)
            time.sleep(0.5)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    warnings = [r for r in records if r["message"].startswith(_WARNING_MESSAGE)]
    assert warnings, "a synthetic loop block must produce a lag warning"
    record = warnings[0]
    extra = record["extra"]
    assert extra["lag_ms"] >= 50
    for field in _CONTEXT_FIELDS:
        assert field in extra
    # The fields stay bound for OTel, but plain log sinks render only the
    # message, so the evidence must be in the message text as well.
    assert f"lag={extra['lag_ms']}ms" in record["message"]
    assert f"WM CPU {extra['wm_cpu_ms']}ms" in record["message"]
    assert f"running={extra['runners_running']}" in record["message"]


def test_warning_is_rate_limited_and_recovery_reports_suppressed_samples() -> None:
    """A continuous stall must cost one warning per cooldown, and the samples
    hidden in between must be counted when the stall ends.

    Unbounded warnings turn a starvation episode into a log flood that buries
    the surrounding diagnostics; a recovery line without the suppressed count
    hides how long the stall actually lasted.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=1.0, cooldown_sec=30.0
    )

    with _capture(level="INFO") as records:
        monitor.observe(ws_context, now=100.0, lag=2.0)  # opens the episode
        monitor.observe(ws_context, now=101.0, lag=2.0)  # within cooldown
        monitor.observe(ws_context, now=120.0, lag=3.0)  # still within cooldown
        monitor.observe(ws_context, now=130.0, lag=2.0)  # cooldown elapsed
        monitor.observe(ws_context, now=131.0, lag=0.1)  # recovered
        monitor.observe(ws_context, now=132.0, lag=0.1)  # idle again

    warnings = [r for r in records if r["message"].startswith(_WARNING_MESSAGE)]
    recoveries = [r for r in records if r["message"].startswith(_RECOVERY_MESSAGE)]
    assert len(warnings) == 2
    assert len(recoveries) == 1
    assert recoveries[0]["extra"]["suppressed_warnings"] == 2
    assert recoveries[0]["extra"]["starvation_ms"] == 31_000
    assert "31000ms" in recoveries[0]["message"]


def test_warning_reports_wm_cpu_and_context_switches_over_the_late_window() -> None:
    """A warning must say what the WM process itself consumed while it was late.

    That is what separates the causes: CPU close to the window means the WM's
    own code held the loop, CPU near zero with involuntary switches means the
    OS gave the CPU to other processes.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=1.0, cooldown_sec=30.0
    )

    def usage(cpu_sec: float, voluntary: int, involuntary: int):
        return event_loop_lag_monitor.ProcessUsage(
            cpu_sec=cpu_sec,
            voluntary_switches=voluntary,
            involuntary_switches=involuntary,
        )

    with _capture(level="WARNING") as records:
        monitor.observe(ws_context, now=100.0, lag=0.0, usage=usage(10.0, 50, 5))
        monitor.observe(ws_context, now=102.5, lag=2.0, usage=usage(10.25, 53, 405))

    (record,) = records
    extra = record["extra"]
    assert extra["window_ms"] == 2500
    assert extra["wm_cpu_ms"] == 250
    assert extra["wm_cpu_pct"] == 10
    assert extra["voluntary_switches"] == 3
    assert extra["involuntary_switches"] == 400
    assert "WM CPU 250ms over 2500ms window (10%)" in record["message"]
    assert "involuntary=+400" in record["message"]


def test_warning_includes_host_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lag warning must carry the host's memory/IO pressure.

    The same lag means different things on an idle host and one that is
    swapping; without the pressure fields the reader has to guess which one it
    was looking at.
    """
    pressure = host_pressure.HostPressure(
        mem_available_mb=100,
        swap_used_mb=200,
        psi_memory_full_avg10=0.1,
        psi_io_full_avg10=0.2,
        psi_cpu_some_avg10=0.3,
    )
    monkeypatch.setattr(
        event_loop_lag_monitor.host_pressure, "read_host_pressure", lambda: pressure
    )
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=1.0, cooldown_sec=30.0
    )

    with _capture(level="WARNING") as records:
        monitor.observe(ws_context, now=100.0, lag=2.0)

    (record,) = records
    assert "swap used=200MB" in record["message"]
    assert record["extra"]["swap_used_mb"] == 200
    assert record["extra"]["psi_memory_full_avg10"] == 0.1


def test_recovery_reports_max_lag_and_usage_over_the_whole_episode() -> None:
    """The recovery line must cover the samples the cooldown hid.

    The warnings show only the first late sample of a stall, and the suppressed
    ones are often much worse; without the episode's maximum lag and CPU
    totals, a multi-second stall reads as a one-second blip.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=1.0, cooldown_sec=30.0
    )

    def usage(cpu_sec: float, voluntary: int, involuntary: int):
        return event_loop_lag_monitor.ProcessUsage(
            cpu_sec=cpu_sec,
            voluntary_switches=voluntary,
            involuntary_switches=involuntary,
        )

    with _capture(level="INFO") as records:
        monitor.observe(ws_context, now=100.0, lag=0.0, usage=usage(10.0, 0, 0))
        monitor.observe(ws_context, now=101.5, lag=1.0, usage=usage(10.5, 10, 1))
        monitor.observe(ws_context, now=107.0, lag=5.0, usage=usage(11.0, 20, 2))
        monitor.observe(ws_context, now=110.0, lag=2.5, usage=usage(11.5, 30, 3))
        monitor.observe(ws_context, now=110.5, lag=0.0, usage=usage(12.0, 40, 4))

    (recovery,) = [r for r in records if r["message"].startswith(_RECOVERY_MESSAGE)]
    extra = recovery["extra"]
    assert extra["lagging_samples"] == 3
    assert extra["max_lag_ms"] == 5000
    assert extra["suppressed_warnings"] == 2
    # From the start of the first late sample's window to the recovery sample.
    assert extra["episode_window_ms"] == 10_500
    assert extra["episode_wm_cpu_ms"] == 2000
    assert extra["episode_involuntary_switches"] == 4
    assert "max lag=5000ms" in recovery["message"]
    assert "WM CPU 2000ms over 10500ms window (19%)" in recovery["message"]


def test_samples_below_threshold_stay_silent() -> None:
    """A healthy loop must produce no records at all.

    A monitor that logs on every sample (or "recovers" when nothing was wrong)
    trains operators to ignore it, which is the opposite of its purpose.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=1.0, cooldown_sec=30.0
    )

    with _capture(level="INFO") as records:
        for i in range(10):
            monitor.observe(ws_context, now=100.0 + i * 0.5, lag=0.05)

    assert records == []


async def test_snapshot_counts_runners_budget_and_in_flight_runs(tmp_path) -> None:
    """The context attached to a warning must describe the WM as it was.

    ERs mid-startup compete for the startup half of the combined budget while
    subprocess work holds the other half, so counting the two phases separately
    is what lets a reader tell a spawn fan-out from a work fan-out (ADR-0093).
    """
    ws_context = context.WorkspaceContext([])
    project_dir = tmp_path / "project"
    ws_context.ws_projects_extension_runners[project_dir] = {
        "initializing": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="initializing",
            status=domain.ExtensionRunnerStatus.INITIALIZING,
        ),
        "repairing": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="repairing",
            status=domain.ExtensionRunnerStatus.REPAIRING,
        ),
        "running_a": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="running_a",
            status=domain.ExtensionRunnerStatus.RUNNING,
        ),
        "running_b": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="running_b",
            status=domain.ExtensionRunnerStatus.RUNNING,
        ),
        "failed": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="failed",
            status=domain.ExtensionRunnerStatus.FAILED,
        ),
    }
    ws_context.in_flight_runs[project_dir] = {
        "run-1": domain.InFlightRun(
            run_id="run-1",
            action_name="lint",
            project_path=project_dir,
            started_at=0.0,
        ),
        "run-2": domain.InFlightRun(
            run_id="run-2",
            action_name="test",
            project_path=project_dir,
            started_at=0.0,
        ),
    }
    lease = await ws_context.process_budget.lease("runner", requested=1)
    try:
        snapshot = event_loop_lag_monitor.snapshot_context(ws_context)

        assert snapshot.runners_starting == 2
        assert snapshot.runners_running == 2
        assert snapshot.budget_granted == 1
        assert snapshot.budget_total == ws_context.process_budget.size
        assert snapshot.in_flight_runs == 2
    finally:
        await ws_context.process_budget.release(lease.lease_id)


async def test_stop_cancels_the_lag_monitor_task() -> None:
    """Shutting the WM down must cancel its lag monitor.

    A surviving monitor keeps sampling a loop whose server is gone, and in a
    process that starts more than one server it accumulates one task per start.
    """
    ws_context = context.WorkspaceContext([])
    original_task = wm_server._lag_monitor_task
    try:
        task = wm_server._start_lag_monitor(ws_context)
        assert not task.done()

        wm_server.stop()

        assert task.cancelling() == 1
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert wm_server._lag_monitor_task is None
    finally:
        wm_server._lag_monitor_task = original_task


def test_lag_summary_reports_latest_and_window_max() -> None:
    """The snapshot needs both the last sample and the recent worst.

    Latest alone misses a stall that already recovered; the window max keeps
    it visible until the window slides past.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=100.0, cooldown_sec=1000.0
    )
    monitor.observe(ws_context, now=100.0, lag=0.1)
    monitor.observe(ws_context, now=100.5, lag=2.4)
    monitor.observe(ws_context, now=101.0, lag=0.05)

    summary = monitor.lag_summary(101.0)

    assert summary.latest_ms == 50
    assert summary.max_recent_ms == 2400
    assert summary.window_sec == 30.0


def test_lag_summary_window_excludes_old_samples() -> None:
    """Samples outside the caller's window must not move the maximum.

    A long window for a slow poll and a short one for a fast poll need
    different answers from the same deque.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=100.0, cooldown_sec=1000.0
    )
    monitor.observe(ws_context, now=100.0, lag=2.4)
    monitor.observe(ws_context, now=145.0, lag=0.05)

    assert monitor.lag_summary(145.0).max_recent_ms == 50
    assert monitor.lag_summary(145.0, window_sec=60.0).max_recent_ms == 2400


def test_lag_samples_pruned_beyond_max_window() -> None:
    """The deque must stay bounded no matter how long the server runs.

    Without pruning, a 0.5 s sample interval would grow the deque without
    bound over a days-long server lifetime.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(
        threshold_sec=100.0, cooldown_sec=1000.0
    )
    monitor.observe(ws_context, now=0.0, lag=1.0)
    monitor.observe(
        ws_context,
        now=event_loop_lag_monitor.LAG_WINDOW_MAX_SEC + 1.0,
        lag=0.1,
    )

    assert len(monitor._samples) == 1
    assert monitor._samples[0][0] == event_loop_lag_monitor.LAG_WINDOW_MAX_SEC + 1.0


def test_lag_summary_pending_reports_overdue_sample() -> None:
    """An overdue monitor sample is itself evidence of a stall.

    When the handler runs before the monitor's own sleeper after a stall,
    neither latest nor the window max contains it — but the overdue time does.
    """
    monitor = event_loop_lag_monitor.EventLoopLagMonitor()
    monitor._next_due = 100.0

    assert monitor.lag_summary(103.0).pending_ms == 3000
    assert monitor.lag_summary(99.0).pending_ms == 0
    monitor._next_due = None
    assert monitor.lag_summary(103.0).pending_ms is None


async def test_run_clears_next_due_on_cancel() -> None:
    """A dead monitor must not report a growing pending time.

    A frozen due time would make every later snapshot report a stall that is
    not happening.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(interval_sec=0.01)
    task = asyncio.create_task(monitor.run(ws_context))
    await asyncio.sleep(0.05)
    assert monitor._next_due is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert monitor._next_due is None
    assert monitor.lag_summary(1000.0).pending_ms is None


async def test_run_clears_next_due_on_observe_error(monkeypatch) -> None:
    """An observe failure ends the monitor, and the pending time goes null.

    The terminal catch keeps the failure loud, but the schedule it leaves
    behind must not keep accusing the loop afterwards.
    """
    ws_context = context.WorkspaceContext([])

    def _raise(*args, **kwargs):
        raise RuntimeError("observe boom")

    monkeypatch.setattr(event_loop_lag_monitor.EventLoopLagMonitor, "observe", _raise)
    monitor = event_loop_lag_monitor.EventLoopLagMonitor(interval_sec=0.01)
    await monitor.run(ws_context)

    assert monitor._next_due is None
    assert monitor.lag_summary(1000.0).pending_ms is None


def test_sample_host_folds_into_peaks(monkeypatch) -> None:
    """Host memory extremes must accumulate across samples.

    The snapshot's swap max and available min are what show a slow leak an
    operator missed between polls.
    """
    ws_context = context.WorkspaceContext([])
    monitor = event_loop_lag_monitor.EventLoopLagMonitor()
    monkeypatch.setattr(
        event_loop_lag_monitor.host_pressure,
        "read_meminfo",
        lambda platform=None: event_loop_lag_monitor.host_pressure.MemInfo(
            mem_total_mb=32000,
            mem_available_mb=4100,
            swap_total_mb=20000,
            swap_used_mb=12000,
        ),
    )

    monitor._sample_host(ws_context)
    monitor._sample_host(ws_context)

    assert ws_context.resource_peaks.host_swap_used_mb == 12000
    assert ws_context.resource_peaks.host_mem_available_min_mb == 4100


async def test_host_sample_failure_keeps_sampling_and_warns_once(
    monkeypatch,
) -> None:
    """A broken host reader must cost one warning, never the monitor.

    Without the guard, the terminal catch would end stall warnings for the
    rest of the server's life on a host-reader bug.
    """
    ws_context = context.WorkspaceContext([])
    monkeypatch.setattr(
        event_loop_lag_monitor.host_pressure,
        "read_meminfo",
        lambda *a, **k: (_ for _ in ()).throw(OSError("proc gone")),
    )
    observe_calls = 0
    real_observe = event_loop_lag_monitor.EventLoopLagMonitor.observe

    def _counting_observe(self, ws_context, *, now, lag, usage=None):
        nonlocal observe_calls
        observe_calls += 1
        return real_observe(self, ws_context, now=now, lag=lag, usage=usage)

    monkeypatch.setattr(
        event_loop_lag_monitor.EventLoopLagMonitor, "observe", _counting_observe
    )

    records: list = []

    @contextlib.contextmanager
    def _capture():
        sink_id = logger.add(lambda message: records.append(message.record))
        try:
            yield
        finally:
            logger.remove(sink_id)

    first = event_loop_lag_monitor.EventLoopLagMonitor(interval_sec=0.01)
    with _capture():
        task = asyncio.create_task(first.run(ws_context))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert observe_calls > 0
    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert len(warnings) == 1

    records.clear()
    second = event_loop_lag_monitor.EventLoopLagMonitor(interval_sec=0.01)
    with _capture():
        task = asyncio.create_task(second.run(ws_context))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
