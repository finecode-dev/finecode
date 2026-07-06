from __future__ import annotations

import asyncio
import pathlib
import threading

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager


async def test_stop_extension_runner_waits_for_process_to_actually_exit(
    tmp_path: pathlib.Path,
) -> None:
    """stop_extension_runner must not return before the runner's process has
    actually exited. Returning early lets a caller act on the runner's
    environment (e.g. delete or recreate it) while the process may still be
    running and using it.
    """
    client = wm_testing.FakeErClient()
    client.configure_response(None)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    stop_task = asyncio.create_task(runner_manager.stop_extension_runner(runner))
    await asyncio.sleep(0.05)
    assert not stop_task.done()

    client.server_process_stopped.set()
    await asyncio.wait_for(stop_task, timeout=2)


async def test_stop_extension_runner_gives_up_after_timeout_instead_of_hanging(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a runner never confirms it stopped, stop_extension_runner must still
    return rather than block its caller indefinitely. The timeout is
    shortened here purely to keep the test fast.
    """
    monkeypatch.setattr(runner_manager, "_STOP_TIMEOUT_SEC", 0.05)
    client = wm_testing.FakeErClient()
    client.configure_response(None)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    await asyncio.wait_for(runner_manager.stop_extension_runner(runner), timeout=2)


def test_stop_extension_runner_sync_waits_for_process_to_actually_exit(
    tmp_path: pathlib.Path,
) -> None:
    """Same contract as the async variant: the sync stop path must not
    return before the runner's process has actually exited.
    """
    client = wm_testing.FakeErClient()
    client.configure_response(None)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    stop_thread = threading.Thread(
        target=runner_manager.stop_extension_runner_sync, args=(runner,)
    )
    stop_thread.start()
    stop_thread.join(timeout=0.05)
    assert stop_thread.is_alive()

    client.server_process_stopped.set()
    stop_thread.join(timeout=2)
    assert not stop_thread.is_alive()
