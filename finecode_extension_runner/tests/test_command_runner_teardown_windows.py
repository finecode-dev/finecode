"""Windows teardown: scope exit and cancelled entry do one immediate tree kill.

Why it matters: without process groups, a scope exit must still reap the
leader and its listed descendants, or a cancelled Windows run leaks trees.
Collected and skipped on POSIX; exercised on the Windows CI leg.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


async def test_scope_exit_tree_kills_on_windows() -> None:
    """Exiting a scope reaps the leader and its descendants."""
    from test_command_runner_ownership import _make_runner

    runner, slots, _, _ = _make_runner()
    async with runner.run(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    ) as proc:
        assert proc.is_alive()
    assert not proc.is_alive()
    assert slots.in_flight == 0


async def test_cancelled_entry_tree_kills_on_windows() -> None:
    """A cancelled entry stops the spawned leader before propagating."""
    from test_command_runner_ownership import _make_runner

    runner, slots, _, _ = _make_runner()

    async def entry() -> None:
        async with runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        ):
            await asyncio.sleep(30)

    task: asyncio.Task[None] = asyncio.ensure_future(entry())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.in_flight == 0
