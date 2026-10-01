"""Active-request counting must reflect requests actually in flight."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import finecode_jsonrpc
from finecode.wm_server.runner import runner_client
from finecode.wm_server.testing import (
    make_run_action_response,
    make_running_runner,
)


class _BlockingClient:
    """Fake client whose send_request parks until released."""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release
        self.pid: int | None = None

    async def send_request(
        self,
        method: str,
        params=None,
        timeout=None,  # noqa: ANN001, ANN002, ANN003
    ):
        from finecode.wm_server.runner import _internal_client_types

        await self._release.wait()
        if "runHandlers" in method:
            return _internal_client_types.ErRunHandlersResponse(
                id=1,
                jsonrpc="2.0",
                result=_internal_client_types.ErRunHandlersResult(
                    result={}, result_by_format={}, status="success"
                ),
            )
        return make_run_action_response()


def _make_runner(release: asyncio.Event) -> runner_client.ExtensionRunnerInfo:
    return make_running_runner(
        working_dir_path=Path("/ws/a"),
        env_name="test_env",
        client=_BlockingClient(release),  # type: ignore[arg-type]
    )


async def test_run_action_active_while_pending_and_zero_after() -> None:
    """An operator must see a runner as active while its request is open.

    Otherwise the resource snapshot would report an executing runner as idle
    and hide where the work actually is.
    """
    release = asyncio.Event()
    runner = _make_runner(release)
    task = asyncio.create_task(runner_client.run_action(runner, "lint", params={}))
    await asyncio.sleep(0)
    assert runner.active_requests == 1
    release.set()
    await task
    assert runner.active_requests == 0


async def test_run_action_cancel_returns_counter_to_zero() -> None:
    """Cancelling a pending request must not leave a stuck active count.

    A leaked count would make the runner look busy forever after the caller
    went away.
    """
    release = asyncio.Event()
    runner = _make_runner(release)
    task = asyncio.create_task(runner_client.run_action(runner, "lint", params={}))
    await asyncio.sleep(0)
    assert runner.active_requests == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.active_requests == 0


async def test_run_action_server_stopped_returns_counter_to_zero() -> None:
    """A dead runner must not leave its active count behind either.

    The error path is the normal way a runner death surfaces, so it has to
    decrement like every other exit.
    """
    release = asyncio.Event()

    class _FailingClient(_BlockingClient):
        async def send_request(
            self,
            method: str,
            params=None,
            timeout=None,  # noqa: ANN001, ANN002, ANN003
        ):
            raise finecode_jsonrpc.ServerStoppedError("stopped")

    runner = make_running_runner(
        working_dir_path=Path("/ws/a"),
        env_name="test_env",
        client=_FailingClient(release),  # type: ignore[arg-type]
    )
    with pytest.raises(runner_client.ActionRunFailed):
        await runner_client.run_action(runner, "lint", params={})
    assert runner.active_requests == 0


async def test_run_handlers_active_while_pending_and_zero_after() -> None:
    """Multi-env segment calls must count exactly like plain action calls.

    Otherwise the snapshot would under-report activity for every fanned-out
    run while correctly reporting single-env ones.
    """
    release = asyncio.Event()
    runner = _make_runner(release)
    task = asyncio.create_task(
        runner_client.run_handlers(runner, "lint", handler_names=["h"], params={})
    )
    await asyncio.sleep(0)
    assert runner.active_requests == 1
    release.set()
    await task
    assert runner.active_requests == 0
