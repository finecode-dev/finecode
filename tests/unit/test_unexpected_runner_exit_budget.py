"""Tests for restarting a runner that exited, and for the budget that bounds it.

An extension runner that exits unexpectedly used to be terminal: the next demand
was refused forever, and the leases it held were never reclaimed. Restarting on
demand fixes that, but a runner that keeps dying must not be restarted forever —
the fourth unexpected exit in ten minutes is reported with its cause and the way
out instead (ADR-0109 rules 3-4).
"""

from __future__ import annotations

import pathlib
import time
from unittest import mock

import pytest

from finecode.wm_server import domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_client, runner_manager

_ENV = "test_env"


def _project(tmp_path: pathlib.Path) -> domain.ResolvedProject:
    return wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="a", handler_env=_ENV
    )


def _exited_runner(tmp_path: pathlib.Path) -> runner_client.ExtensionRunnerInfo:
    runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name=_ENV
    )
    runner.status = domain.ExtensionRunnerStatus.EXITED
    return runner


async def test_an_exited_runner_restarts_on_next_demand(
    tmp_path: pathlib.Path,
) -> None:
    """A dead runner must be replaced rather than refused forever.

    A crashed runner is recoverable; leaving it EXITED makes every later demand
    fail on a channel nothing will ever answer, which is what the restart on
    next demand exists to prevent.
    """
    project = _project(tmp_path)
    runner = _exited_runner(tmp_path)
    ws_context = wm_testing.make_workspace_context(
        project=project, runner=runner, env_name=_ENV
    )
    replacement = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name=_ENV
    )

    with mock.patch.object(
        runner_manager, "start_runner", return_value=replacement
    ) as start:
        got = await runner_manager.get_or_start_runner(project, _ENV, ws_context)

    assert got is replacement
    _ = start.assert_awaited_once()


async def test_the_fourth_unexpected_exit_in_the_window_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """Past the budget the demand must fail fast, naming the cause and the way out.

    Restarting forever turns a crash into an infinite loop of crashes and hides
    the failure that causes them; the refusal has to say how to clear it.
    """
    project = _project(tmp_path)
    runner = _exited_runner(tmp_path)
    ws_context = wm_testing.make_workspace_context(
        project=project, runner=runner, env_name=_ENV
    )
    ws_context.unexpected_runner_exits[(tmp_path, _ENV)] = [time.monotonic()] * 3

    with (
        mock.patch.object(runner_manager, "start_runner") as start,
        pytest.raises(runner_manager.RunnerFailedToStart) as exc_info,
    ):
        await runner_manager.get_or_start_runner(project, _ENV, ws_context)

    assert "exited unexpectedly" in str(exc_info.value)
    assert "reload-config" in str(exc_info.value)
    start.assert_not_called()


def test_exits_older_than_the_window_do_not_count(tmp_path: pathlib.Path) -> None:
    """A crash an hour ago must not make a single crash now look like a pattern."""
    project = _project(tmp_path)
    runner = _exited_runner(tmp_path)
    ws_context = wm_testing.make_workspace_context(
        project=project, runner=runner, env_name=_ENV
    )
    stale = time.monotonic() - runner_manager._UNEXPECTED_EXIT_BUDGET_WINDOW_SEC - 1
    ws_context.unexpected_runner_exits[(tmp_path, _ENV)] = [stale, stale, stale]

    exits = runner_manager._unexpected_exits_within_budget(
        ws_context, tmp_path, _ENV
    )

    assert exits == []
    assert (tmp_path, _ENV) not in ws_context.unexpected_runner_exits


async def test_a_wm_stop_is_flagged_so_it_is_not_counted(
    tmp_path: pathlib.Path,
) -> None:
    """A stop the WM asked for is not a crash and must not open the budget.

    Without the flag, every configuration recovery — which replaces runners by
    stopping them — would look like a wave of unexpected exits and trip the
    refusal for a project that never crashed.
    """
    project = _project(tmp_path)
    fake_client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name=_ENV, client=fake_client
    )
    fake_client.configure_response({})
    fake_client.server_process_stopped.set()
    ws_context = wm_testing.make_workspace_context(
        project=project, runner=runner, env_name=_ENV
    )

    await runner_manager.stop_extension_runner(runner, ws_context)

    assert runner.stopped_by_wm is True
