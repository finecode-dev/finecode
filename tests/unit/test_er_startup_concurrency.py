from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

import finecode_jsonrpc
from finecode.wm_server import context, domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager


class _ConcurrencyTrackingClient:
    """Stand-in for `JsonRpcClient` that records how many instances are
    inside `start()` at once, instead of actually spawning a process."""

    current = 0
    max_observed = 0

    def __init__(self, *, message_types, readable_id, tracing=None) -> None:
        self.readable_id = readable_id
        self.pid = None
        self.server_exit_callback = None
        self.startup_timeline = finecode_jsonrpc.StartupTimeline()

    async def start(self, **_kwargs) -> None:
        type(self).current += 1
        type(self).max_observed = max(type(self).max_observed, type(self).current)
        try:
            # Simulate the CPU/memory-bursty spawn+import window that
            # actually contends for machine resources.
            await asyncio.sleep(0.05)
        finally:
            type(self).current -= 1

    def force_kill(self) -> None: ...

    def feature(self, name, impl) -> None: ...


def _make_runner_and_project(
    tmp_path: pathlib.Path, index: int
) -> tuple[runner_manager.runner_client.ExtensionRunnerInfo, domain.Project]:
    project_dir = tmp_path / f"project_{index}"
    project_dir.mkdir()
    project = domain.Project(
        name=f"project_{index}",
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
    )
    runner = wm_testing.make_initializing_runner(
        working_dir_path=project_dir, env_name="dev_workspace"
    )
    return runner, project


async def test_er_startup_concurrency_is_bounded_by_semaphore(
    tmp_path: pathlib.Path,
) -> None:
    """`_start_extension_runner_process` must never let more ERs be mid-startup
    (spawned, not yet RPC-connected) at once than the configured cap — the
    resource contention that produces "Didn't get port in 30 seconds" failures
    (see ADR-0063) comes from exactly this window, so bounding it is the fix.
    """
    _ConcurrencyTrackingClient.current = 0
    _ConcurrencyTrackingClient.max_observed = 0

    cap = 2
    runner_count = 6

    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.er_startup_semaphore = asyncio.Semaphore(cap)
    ws_context.runner_io_thread = object()

    runners_and_projects = [
        _make_runner_and_project(tmp_path, i) for i in range(runner_count)
    ]
    for _runner, project in runners_and_projects:
        ws_context.ws_projects[project.dir_path] = project

    with (
        mock.patch.object(
            runner_manager.finecode_cmd, "get_python_cmd", return_value="fake-python"
        ),
        mock.patch.object(
            runner_manager.jsonrpc_client,
            "JsonRpcClient",
            _ConcurrencyTrackingClient,
        ),
    ):
        await asyncio.gather(
            *(
                runner_manager._start_extension_runner_process(
                    runner=runner, ws_context=ws_context
                )
                for runner, _project in runners_and_projects
            )
        )

    assert _ConcurrencyTrackingClient.max_observed == cap
