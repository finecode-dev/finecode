"""Requirement tests: a runner whose shutdown RPC goes unanswered is force-killed.

REQUIREMENT: when a runner's RPC channel is dead, the graceful stop cannot be
negotiated — the shutdown request times out or the channel reports the server
stopped. Waiting out the post-exit timeout would burn ten seconds per runner
for nothing, and sending a further `exit` notification writes to a channel
nobody is reading. `force_kill()` is reserved for exactly this case ("no live
RPC channel"); it must be applied directly, and the runner must still be
reclaimed so a workspace operation that replaces runners stays finite.

The split is load-bearing in the other direction too: a runner that *does*
answer shutdown keeps the graceful path, where force-kill is deliberately
withheld so it can finish tearing down its own children.
"""

from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

import finecode_jsonrpc
from finecode.wm_server import context
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.config import config_models
from finecode.wm_server.runner import _internal_client_types, runner_manager
from finecode.wm_server.services import config_reload_service


async def _stop_with_error(
    tmp_path: pathlib.Path, error: BaseException
) -> tuple[wm_testing.FakeErClient, float]:
    client = wm_testing.FakeErClient()
    client.configure_error(error)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])

    started = time.monotonic()
    await asyncio.wait_for(
        runner_manager.stop_extension_runner(runner, ws_context), timeout=2.0
    )
    return client, time.monotonic() - started


def _methods(client: wm_testing.FakeErClient) -> list[str]:
    return [method for method, _ in client.sent_requests]


async def test_stop_force_kills_when_shutdown_returns_timeout(
    tmp_path: pathlib.Path,
) -> None:
    """A shutdown that times out means no live channel — force-kill it."""
    client, elapsed = await _stop_with_error(
        tmp_path, finecode_jsonrpc.ResponseTimeout("no response to shutdown")
    )

    assert client.force_kill_called
    assert elapsed < runner_manager._STOP_TIMEOUT_SEC
    assert _internal_client_types.EXIT not in _methods(client)


async def test_stop_force_kills_when_shutdown_reports_server_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """A shutdown whose channel reports the server stopped is force-killed too."""
    client, elapsed = await _stop_with_error(
        tmp_path, finecode_jsonrpc.ServerStoppedError("channel closed")
    )

    assert client.force_kill_called
    assert elapsed < runner_manager._STOP_TIMEOUT_SEC
    assert _internal_client_types.EXIT not in _methods(client)


async def test_stop_waits_gracefully_when_shutdown_answers(
    tmp_path: pathlib.Path,
) -> None:
    """A runner that answers shutdown keeps the no-force-kill graceful path."""
    client = wm_testing.FakeErClient()
    client.configure_response(None)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])

    stop_task = asyncio.create_task(
        runner_manager.stop_extension_runner(runner, ws_context)
    )
    await asyncio.sleep(0)
    assert not client.force_kill_called

    client.server_process_stopped.set()
    await asyncio.wait_for(stop_task, timeout=2.0)

    assert not client.force_kill_called
    assert _internal_client_types.EXIT in _methods(client)


async def test_recovery_failure_reaps_runner_with_dead_channel(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovery that fails over a dead channel force-kills that runner.

    The recovery may fail before it reaches the runner-replacement step (config
    re-reading asks the running ER first). A dead runner left behind there keeps
    holding its process budget and its environment, which is the leak the
    recovery is supposed to close.
    """
    client = wm_testing.FakeErClient()
    client.channel_failed = True
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="some_action"
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    async def _fail(**_kwargs) -> None:
        raise finecode_jsonrpc.ResponseTimeout("no response to packages/resolvePath")

    monkeypatch.setattr(
        config_reload_service.runner_start_service,
        "start_runners_with_auto_prepare",
        _fail,
    )

    results = await config_reload_service.reload_config(
        ws_context, project_dir=tmp_path
    )

    assert results[0]["status"] == "failed"
    assert client.force_kill_called


async def test_recovery_failure_leaves_healthy_runner_running(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovery that fails for a non-channel reason leaves the runner running.

    A config error (say, a preset package no longer installed) must not be
    mistaken for a dead channel: the old configuration is still the one in
    effect, and the healthy runner serving it must stay up.
    """
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="some_action"
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    async def _fail(**_kwargs) -> None:
        raise config_models.ConfigurationError("preset package is not installed")

    monkeypatch.setattr(
        config_reload_service.runner_start_service,
        "start_runners_with_auto_prepare",
        _fail,
    )

    results = await config_reload_service.reload_config(
        ws_context, project_dir=tmp_path
    )

    assert results[0]["status"] == "failed"
    assert not client.force_kill_called
