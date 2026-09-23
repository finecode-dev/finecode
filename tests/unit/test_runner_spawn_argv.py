"""The ER spawn command is an argv list with no shell wrapper.

The WM must exec the ER interpreter directly -- ``python -m
finecode_extension_runner.cli start ...`` -- because a shell in between
re-parses the command (a space in a project path splits it) and, on Windows,
lets ``cmd.exe`` own the child. Pins the exact argv shape the server receives.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager


async def test_runner_server_cmd_is_argv(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = wm_testing.make_initializing_runner(
        working_dir_path=tmp_path, env_name="test_env"
    )
    runner.cmd_override = "/srv/venvs/dev/bin/python"
    # `_start_extension_runner_process` now uses `runner.client`, attached by
    # `_start_runner` before the start; a real client keeps this test's
    # class-level patches of `JsonRpcClient._start_server` / `connect_to_server`
    # in the path.
    runner.client = runner_manager._make_runner_client(runner)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[tmp_path] = domain.Project(
        name="t",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
    )
    ws_context.runner_io_thread = object()

    captured: dict = {}

    async def _fake_start_server(self, **kwargs):
        captured.update(kwargs)

    async def _fake_connect_to_server(self, **kwargs):
        pass

    monkeypatch.setattr(
        runner_manager.jsonrpc_client.JsonRpcClient,
        "_start_server",
        _fake_start_server,
    )
    monkeypatch.setattr(
        runner_manager.jsonrpc_client.JsonRpcClient,
        "connect_to_server",
        _fake_connect_to_server,
    )

    await runner_manager._start_extension_runner_process(runner, ws_context)

    server_cmd = captured["full_cmd"]
    assert isinstance(server_cmd, list)
    assert server_cmd[0] == os.fspath(pathlib.Path(runner.cmd_override))
    assert server_cmd[1:4] == ["-m", "finecode_extension_runner.cli", "start"]
    assert server_cmd[4:] == [
        "--log-level=INFO",
        f"--project-path={tmp_path.as_posix()}",
        "--env-name=test_env",
    ]
