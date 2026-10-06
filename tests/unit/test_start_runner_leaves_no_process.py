"""Requirement tests (ADR-0097): a start that ends before RUNNING leaves no process.

REQUIREMENT: a runner start can fail, time out or be cancelled at any step before
the runner reaches RUNNING. None of those outcomes may leave the ER's OS process
running or leave a waiter parked on ``initialized_event`` forever, because a
leaked ER process outlives the WM that spawned it and a hanging waiter wedges the
caller. A runner that *did* reach RUNNING is a different case: it gets a graceful
exit, never a force-kill from this path.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

import pytest

import finecode_jsonrpc
from finecode.wm_server import context, domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager


class _FakeJsonRpcClient:
    """Stand-in for ``JsonRpcClient`` that records kills and can stall in
    ``start()`` instead of spawning a process."""

    instances: list["_FakeJsonRpcClient"] = []
    wait_forever: bool = False

    def __init__(self, *, message_types, readable_id, tracing=None) -> None:
        self.readable_id = readable_id
        self.pid = None
        self.server_exit_callback = None
        self.force_kill_called = False
        self.start_entered = asyncio.Event()
        self.startup_timeline = finecode_jsonrpc.StartupTimeline()
        type(self).instances.append(self)

    async def start(self, **_kwargs) -> None:
        self.start_entered.set()
        if type(self).wait_forever:
            await asyncio.Event().wait()

    def force_kill(self) -> None:
        self.force_kill_called = True

    def feature(self, name, impl) -> None: ...


def _make_context(
    tmp_path: pathlib.Path, env_name: str = "dev_no_runtime"
) -> tuple[context.WorkspaceContext, domain.ResolvedProject]:
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        handler_env=env_name,
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project
    # Pretend the raw config is already known so the start path does not try to
    # read it from disk (the config branch is exercised elsewhere).
    ws_context.ws_projects_raw_configs[project.dir_path] = {}
    # Keep `_start_extension_runner_process` from starting a real IO thread.
    ws_context.runner_io_thread = object()  # type: ignore[assignment]
    return ws_context, project


def _runner_from_context(
    ws_context: context.WorkspaceContext, project: domain.Project, env_name: str
) -> object:
    return ws_context.ws_projects_extension_runners[project.dir_path][env_name]


async def _patch_start_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeJsonRpcClient.instances = []
    _FakeJsonRpcClient.wait_forever = False
    monkeypatch.setattr(
        runner_manager.finecode_cmd, "get_python_cmd", lambda *a, **k: "fake-python"
    )
    monkeypatch.setattr(
        runner_manager.jsonrpc_client, "JsonRpcClient", _FakeJsonRpcClient
    )


async def test_cancelled_start_kills_its_process_and_sets_the_event(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start cancelled while the ER is still coming up must not leak the ER
    process, and must not leave a caller waiting on an event that will never
    fire."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch)
    _FakeJsonRpcClient.wait_forever = True

    task = asyncio.create_task(
        runner_manager._start_runner(
            project_def=project,
            env_name="dev_no_runtime",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    )

    async with asyncio.timeout(5):
        while (
            not _FakeJsonRpcClient.instances
            or not _FakeJsonRpcClient.instances[0].start_entered.is_set()
        ):
            await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    runner = _runner_from_context(ws_context, project, "dev_no_runtime")
    client = _FakeJsonRpcClient.instances[0]
    assert client.force_kill_called
    assert runner.status == domain.ExtensionRunnerStatus.FAILED
    assert runner.initialized_event.is_set()


async def test_init_failure_kills_process_and_preserves_the_exception_type(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An initialize failure is translated to ``RunnerFailedToStart``, and the
    caller (auto-repair) keys on that type — abandoning the start must kill the
    process without replacing the exception."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch)

    async def _fail_init(runner, project):
        raise runner_manager.RunnerFailedToStart("initialize failed")

    monkeypatch.setattr(runner_manager, "_init_lsp_client", _fail_init)
    monkeypatch.setattr(runner_manager, "notify_project_changed", _noop_project_changed)

    with pytest.raises(runner_manager.RunnerFailedToStart):
        await runner_manager._start_runner(
            project_def=project,
            env_name="dev_no_runtime",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )

    runner = _runner_from_context(ws_context, project, "dev_no_runtime")
    assert _FakeJsonRpcClient.instances[0].force_kill_called
    assert runner.status == domain.ExtensionRunnerStatus.FAILED


async def test_update_config_failure_kills_process_and_preserves_the_type(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``EnvironmentOutOfDateError`` must survive the abandon path: the caller
    decides whether to reinstall the environment from that type alone."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch)
    await _patch_successful_init(monkeypatch)

    async def _fail_update_config(
        *, runner, project, handlers_to_initialize, ws_context, pass_label="other"
    ):
        raise runner_manager.EnvironmentOutOfDateError(
            "environment is stale", env_name=runner.env_name
        )

    monkeypatch.setattr(runner_manager, "update_runner_config", _fail_update_config)

    with pytest.raises(runner_manager.EnvironmentOutOfDateError):
        await runner_manager._start_runner(
            project_def=project,
            env_name="dev_no_runtime",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )

    runner = _runner_from_context(ws_context, project, "dev_no_runtime")
    assert _FakeJsonRpcClient.instances[0].force_kill_called
    assert runner.status == domain.ExtensionRunnerStatus.FAILED


async def test_failure_after_running_does_not_kill(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the runner is RUNNING it has been sent a graceful-exit channel; a
    later failure must not force-kill it, and its status must stay RUNNING."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch)
    await _patch_successful_init(monkeypatch)

    async def _fail_push(runner):
        raise RuntimeError("forwarding failed")

    bridge = mock.MagicMock()
    bridge.push_er_forwarding_to_runner = _fail_push
    monkeypatch.setattr(runner_manager.wm_bridge, "handlers", lambda: bridge)

    with pytest.raises(RuntimeError):
        await runner_manager._start_runner(
            project_def=project,
            env_name="dev_no_runtime",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )

    runner = _runner_from_context(ws_context, project, "dev_no_runtime")
    assert not _FakeJsonRpcClient.instances[0].force_kill_called
    assert runner.status == domain.ExtensionRunnerStatus.RUNNING


async def test_no_venv_failure_keeps_no_venv_and_kills_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing venv is repairable, and the auto-repair branch keys on the
    NO_VENV status — the abandon path must not overwrite it. The client is
    attached before the start attempt (it must be, for the ADR-0097 kill
    latch), but it is never spawned, so there is no process to kill and the
    waiter must still be released."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch)
    monkeypatch.setattr(
        runner_manager.finecode_cmd,
        "get_python_cmd",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("venv not found")),
    )
    monkeypatch.setattr(runner_manager, "notify_project_changed", _noop_project_changed)

    with pytest.raises(runner_manager.RunnerFailedToStart):
        await runner_manager._start_runner(
            project_def=project,
            env_name="dev_no_runtime",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )

    runner = _runner_from_context(ws_context, project, "dev_no_runtime")
    assert runner.client is not None
    assert not runner.client.start_entered.is_set()
    assert runner.status == domain.ExtensionRunnerStatus.NO_VENV
    assert runner.initialized_event.is_set()


async def _noop_project_changed(project) -> None: ...


async def _patch_successful_init(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok_init(runner, project):
        return None

    class _RunnerInfo:
        log_file_path = None

    async def _get_runner_info(client):
        return _RunnerInfo()

    async def _ok_update_config(
        *, runner, project, handlers_to_initialize, ws_context, pass_label="other"
    ):
        return None

    async def _ok_finish(runner, project, ws_context):
        return None

    monkeypatch.setattr(runner_manager, "_init_lsp_client", _ok_init)
    monkeypatch.setattr(
        runner_manager._internal_client_api, "get_runner_info", _get_runner_info
    )
    monkeypatch.setattr(runner_manager, "update_runner_config", _ok_update_config)
    monkeypatch.setattr(runner_manager, "_finish_runner_init", _ok_finish)
    monkeypatch.setattr(runner_manager, "notify_project_changed", _noop_project_changed)
