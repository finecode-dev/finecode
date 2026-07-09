"""ADR-0049 - unit tests for the WM-side ER log-forwarding toggle.

Drives ``wm_server.push_er_forwarding_to_runner`` / ``_sync_er_forwarding``
against a ``FakeErClient`` and asserts on the control RPCs it captures, per
``wm_server.testing``. No real ER, no socket.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from finecode.wm_server import domain, testing as wm_testing, wm_server
from finecode.wm_server.runner import _internal_client_types, runner_client


@pytest.fixture(autouse=True)
def _reset_log_delivery():
    wm_server.reset_log_delivery()
    yield
    wm_server.reset_log_delivery()


def _configure_ok(client: wm_testing.FakeErClient) -> None:
    client.configure_response(
        _internal_client_types.ErUpdateLoggingResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErUpdateLoggingResult(),
        )
    )


async def test_first_subscribe_pushes_forward_enabled_at_min_level(
    tmp_path: pathlib.Path,
) -> None:
    client = wm_testing.FakeErClient()
    _configure_ok(client)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    wm_server._log_registry.register("conn-a", "INFO")
    await wm_server.push_er_forwarding_to_runner(runner)

    [(method, params)] = client.sent_requests
    assert method == _internal_client_types.ER_UPDATE_LOGGING
    assert params.forward is True
    assert params.forward_level == "INFO"
    assert runner.log_forwarding == (True, "INFO")


async def test_second_subscribe_at_lower_level_repushes_lower_level(
    tmp_path: pathlib.Path,
) -> None:
    client = wm_testing.FakeErClient()
    _configure_ok(client)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    wm_server._log_registry.register("conn-a", "INFO")
    await wm_server.push_er_forwarding_to_runner(runner)

    wm_server._log_registry.register("conn-b", "DEBUG")
    await wm_server.push_er_forwarding_to_runner(runner)

    assert len(client.sent_requests) == 2
    _, params = client.sent_requests[1]
    assert params.forward is True
    assert params.forward_level == "DEBUG"


async def test_last_unsubscribe_disables_forwarding(tmp_path: pathlib.Path) -> None:
    client = wm_testing.FakeErClient()
    _configure_ok(client)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    wm_server._log_registry.register("conn-a", "INFO")
    await wm_server.push_er_forwarding_to_runner(runner)

    wm_server._log_registry.unregister("conn-a")
    await wm_server.push_er_forwarding_to_runner(runner)

    assert len(client.sent_requests) == 2
    _, params = client.sent_requests[1]
    assert params.forward is False


async def test_redundant_subscribe_at_same_level_sends_no_second_rpc(
    tmp_path: pathlib.Path,
) -> None:
    client = wm_testing.FakeErClient()
    _configure_ok(client)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    wm_server._log_registry.register("conn-a", "INFO")
    await wm_server.push_er_forwarding_to_runner(runner)
    await wm_server.push_er_forwarding_to_runner(runner)  # no registry change

    assert len(client.sent_requests) == 1


async def test_uninitialized_runner_is_skipped_until_ready(
    tmp_path: pathlib.Path,
) -> None:
    client = wm_testing.FakeErClient()
    _configure_ok(client)
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=tmp_path,
        env_name="test_env",
        status=domain.ExtensionRunnerStatus.INITIALIZING,
        client=client,
    )
    wm_server._log_registry.register("conn-a", "INFO")

    await wm_server.push_er_forwarding_to_runner(runner)
    assert client.sent_requests == []

    runner.initialized_event.set()
    await wm_server.push_er_forwarding_to_runner(runner)
    assert len(client.sent_requests) == 1


async def test_sync_er_forwarding_pushes_to_every_runner_in_context(
    tmp_path: pathlib.Path,
) -> None:
    client = wm_testing.FakeErClient()
    _configure_ok(client)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    project = domain.Project(
        name="p",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    wm_server._log_registry.register("conn-a", "INFO")
    wm_server._sync_er_forwarding(ws_context)

    for _ in range(100):
        if client.sent_requests:
            break
        await asyncio.sleep(0.01)

    assert len(client.sent_requests) == 1
    _, params = client.sent_requests[0]
    assert params.forward is True
    assert params.forward_level == "INFO"
