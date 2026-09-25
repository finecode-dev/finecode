from __future__ import annotations

import pathlib
import time

import pytest

from finecode.wm_server import context
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import shutdown_service


async def test_shutdown_kills_process_of_a_still_initializing_runner(
    tmp_path: pathlib.Path,
) -> None:
    """A runner still starting up when the WM shuts down must not survive it.

    A runner in this state was never sent a graceful exit request (that only
    happens once it's RUNNING), so it has no way to learn the WM is going
    away. Without an explicit kill here, its OS process — which may already
    be spawned even though it never became reachable — keeps running
    unmanaged after the WM that owned it is gone.
    """
    initializing_client = wm_testing.FakeErClient()
    initializing_runner = wm_testing.make_initializing_runner(
        working_dir_path=tmp_path, env_name="testing", client=initializing_client
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects_extension_runners[tmp_path] = {
        "testing": initializing_runner,
    }

    await shutdown_service.on_shutdown(ws_context)

    assert initializing_client.force_kill_called
    assert initializing_client.sent_requests == []


async def test_shutdown_still_gracefully_stops_a_running_runner(
    tmp_path: pathlib.Path,
) -> None:
    """Broadening the shutdown sweep to reap stuck starts must not change how
    an already-RUNNING runner is stopped — it still gets the cooperative
    shutdown/exit RPCs, not a force-kill, so it can flush/clean up normally.
    """
    running_client = wm_testing.FakeErClient()
    running_client.configure_response(None)
    running_client.server_process_stopped.set()
    running_runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="dev", client=running_client
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects_extension_runners[tmp_path] = {"dev": running_runner}

    await shutdown_service.on_shutdown(ws_context)

    assert not running_client.force_kill_called
    sent_methods = [method for method, _ in running_client.sent_requests]
    assert "exit" in sent_methods


async def test_shutdown_stops_slow_runners_concurrently_not_sequentially(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Total shutdown time must not grow with the number of runners.

    on_shutdown runs synchronously inside the WM's own event loop, so a
    sequential sweep blocks the whole server for as long as it takes. In a
    workspace with dozens of runners, a handful being slow to confirm they
    stopped turns a bounded per-runner wait into a WM that is unresponsive —
    and looks orphaned/hung to an operator — for minutes. With N runners that
    each take the full timeout to give up, wall time must stay close to one
    timeout period, not N of them.
    """
    monkeypatch.setattr(runner_manager, "_STOP_TIMEOUT_SEC", 0.2)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    runners_by_env = {}
    for i in range(8):
        client = wm_testing.FakeErClient()
        client.configure_response(None)
        # server_process_stopped deliberately left unset — this runner never
        # confirms, forcing each stop attempt to consume the full timeout.
        runners_by_env[f"env_{i}"] = wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name=f"env_{i}", client=client
        )
    ws_context.ws_projects_extension_runners[tmp_path] = runners_by_env

    start = time.monotonic()
    await shutdown_service.on_shutdown(ws_context)
    elapsed = time.monotonic() - start

    # Sequential would take ~8 * 0.2s = 1.6s; concurrent stays near 0.2s.
    assert elapsed < 0.2 * 4
