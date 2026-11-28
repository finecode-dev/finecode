"""The single runner-counting implementation must match the WM's state."""

from __future__ import annotations

import contextlib
from pathlib import Path

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_client, runner_counts
from finecode.wm_server.testing import FakeErClient


def _make_context() -> context.WorkspaceContext:
    ws_context = context.WorkspaceContext([])
    project_dir = Path("/ws/a")
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
    return ws_context


def test_by_status_zero_filled_and_starting_counts() -> None:
    """Every lifecycle status must appear in the snapshot, even at zero.

    A missing key forces every consumer to guess whether zero or unknown was
    meant, so the zero-fill is what keeps the table honest.
    """
    ws_context = _make_context()

    counts = runner_counts.count_runners(ws_context)

    assert set(counts.by_status) == set(domain.ExtensionRunnerStatus)
    assert counts.by_status[domain.ExtensionRunnerStatus.NO_VENV] == 0
    assert counts.by_status[domain.ExtensionRunnerStatus.EXITED] == 0
    assert counts.running == 2
    assert counts.starting == 2
    assert counts.projects_running == 1


def test_yielded_runner_counts_as_neither_used_nor_waiting() -> None:
    """A runner that yielded its startup slot is not waiting for one.

    It is still INITIALIZING but holds no permit and queues for none (ADR-0100),
    so counting it as waiting would report a queue that does not exist.
    """
    ws_context = context.WorkspaceContext([])
    project_dir = Path("/ws/a")
    ws_context.ws_projects_extension_runners[project_dir] = {
        "yielded": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="yielded",
            status=domain.ExtensionRunnerStatus.INITIALIZING,
            startup_slot_release=None,
            awaiting_startup_slot=False,
        ),
    }

    counts = runner_counts.count_runners(ws_context)

    assert counts.startup_slots_used == 0
    assert counts.startup_slots_waiting == 0
    assert counts.starting == 1


def test_active_counts_and_splits_per_env() -> None:
    """Active must count runners with open requests, split by environment.

    An operator tracing a stall needs to know which env is actually executing,
    not just how many runners exist.
    """
    ws_context = context.WorkspaceContext([])
    project_dir = Path("/ws/a")
    ws_context.ws_projects_extension_runners[project_dir] = {
        "env_a": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="env_a",
            status=domain.ExtensionRunnerStatus.RUNNING,
            active_requests=1,
        ),
        "env_b": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="env_b",
            status=domain.ExtensionRunnerStatus.RUNNING,
            active_requests=2,
        ),
        "env_c": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="env_c",
            status=domain.ExtensionRunnerStatus.RUNNING,
            active_requests=0,
        ),
    }

    counts = runner_counts.count_runners(ws_context)

    assert counts.active == 2
    assert counts.by_env["env_a"] == runner_counts.EnvCounts(running=1, active=1)
    assert counts.by_env["env_b"] == runner_counts.EnvCounts(running=1, active=1)


def test_live_runner_pids_skips_dead_and_pidless() -> None:
    """Only live runners with a known pid can be footprint targets.

    Including a dead runner would attribute its old pid's reuse to the wrong
    env; including a pid-less one would crash the walk.
    """
    ws_context = context.WorkspaceContext([])
    project_dir = Path("/ws/a")
    live_client = FakeErClient()
    live_client.pid = 4242
    pidless_client = FakeErClient()
    pidless_client.pid = None
    ws_context.ws_projects_extension_runners[project_dir] = {
        "live": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="live",
            status=domain.ExtensionRunnerStatus.RUNNING,
            client=live_client,  # type: ignore[arg-type]
        ),
        "failed": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="failed",
            status=domain.ExtensionRunnerStatus.FAILED,
            client=live_client,  # type: ignore[arg-type]
        ),
        "exited": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="exited",
            status=domain.ExtensionRunnerStatus.EXITED,
            client=live_client,  # type: ignore[arg-type]
        ),
        "pidless": runner_client.ExtensionRunnerInfo(
            working_dir_path=project_dir,
            env_name="pidless",
            status=domain.ExtensionRunnerStatus.RUNNING,
            client=pidless_client,  # type: ignore[arg-type]
        ),
    }

    targets = runner_counts.live_runner_pids(ws_context)

    live_runner = ws_context.ws_projects_extension_runners[project_dir]["live"]
    assert targets == [(live_runner.readable_id, 4242)]


def test_record_runner_peaks_never_raises_and_latches(monkeypatch) -> None:
    """A broken counter must cost observability, never a start.

    The hook runs inside the start path, so raising would break every ER
    start; instead it records that peaks may be stale and stays quiet after
    the first report.
    """
    ws_context = context.WorkspaceContext([])
    monkeypatch.setattr(
        runner_counts,
        "count_runners",
        lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    records: list = []

    @contextlib.contextmanager
    def _capture():
        sink_id = logger.add(lambda message: records.append(message.record))
        try:
            yield
        finally:
            logger.remove(sink_id)

    with _capture():
        runner_counts.record_runner_peaks(ws_context)

    assert ws_context.resource_peaks.hook_failed
    assert len(records) == 1

    records.clear()
    with _capture():
        runner_counts.record_runner_peaks(ws_context)

    assert records == []
    assert ws_context.resource_peaks.hook_failed


def test_action_meta_dumps_fold_into_startup_slots() -> None:
    """Dumps hold startup permits, so the gauges must include them or the queue is undercounted."""
    ws_context = context.WorkspaceContext([])
    ws_context.action_meta_dump_stats.running = 2
    ws_context.action_meta_dump_stats.waiting = 3

    counts = runner_counts.count_runners(ws_context)

    assert counts.startup_slots_used == 2
    assert counts.startup_slots_waiting == 3
