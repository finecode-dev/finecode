"""Regression test for issue #12: a deaf ER must not hang runner replacement.

The bug: `reload_config`'s replace step -> `restart_extension_runners` ->
`stop_extension_runner` -> `_internal_client_api.shutdown` awaited a
`send_request(SHUTDOWN)` with no timeout. When the ER's channel was deaf, that
future never resolved, so the whole recovery parked forever with no log and no
error.

This keeps the in-process shape of the original reproduction — a fake ER client
whose channel never answers — as a regression guard. The real-process variant
lives in `tests/e2e/wm/test_repro_reload_hang_e2e.py`.
"""

from __future__ import annotations

import asyncio
import pathlib
import threading

import pytest

import finecode_jsonrpc
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, runner_manager


class _UnresponsiveErClient:
    """An ER client whose RPC channel is deaf.

    A request is written, but the channel never delivers a response, so the
    client raises ``ResponseTimeout`` — as a bounded RPC does once its timeout
    elapses. ``server_process_stopped`` is left unset: the OS process is still
    alive, matching a runner that stays listed as RUNNING.
    """

    def __init__(self) -> None:
        self.readable_id = "unresponsive-er"
        self.server_process_stopped = threading.Event()
        self.force_kill_called = False
        self.sent_requests: list[tuple[str, object]] = []

    async def send_request(
        self, method: str, params: object = None, timeout: float | None = None
    ) -> object:
        self.sent_requests.append((method, params))
        raise finecode_jsonrpc.ResponseTimeout(f"No response on '{method}' within {timeout}s")

    def notify(self, method: str, params: object = None) -> None:
        self.sent_requests.append((method, params))

    def force_kill(self) -> None:
        self.force_kill_called = True


async def test_restart_gives_up_on_deaf_channel(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead shutdown channel force-kills the runner and lets restart finish."""
    client = _UnresponsiveErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="some_action"
    )
    ws_context = wm_testing.make_workspace_context(
        project=project, runner=runner, env_name="test_env"
    )

    started: list[str] = []

    async def _fake_start_runner(**kwargs):
        started.append(kwargs["env_name"])
        return runner

    monkeypatch.setattr(runner_manager, "start_runner", _fake_start_runner)

    await asyncio.wait_for(
        runner_manager.restart_extension_runners(
            runner_working_dir_path=tmp_path, ws_context=ws_context
        ),
        timeout=2.0,
    )

    assert client.force_kill_called
    assert started == ["test_env"]
    assert _internal_client_types.EXIT not in [
        method for method, _ in client.sent_requests
    ]
