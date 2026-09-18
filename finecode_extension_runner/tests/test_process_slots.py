"""The ER's resizable process-slot gate (ADR-0090).

One gate is shared by ``CommandRunner`` and ``ProcessExecutor`` inside an ER.
It must throttle without ever revoking a slot that was already granted, and it
must let waiters through when the target grows.
"""

from __future__ import annotations

import asyncio

from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.impls.process_executor import ProcessExecutor
from finecode_extension_runner.process_slots import ProcessSlots


class _NoopLogger:
    def debug(self, message: str) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def exception(self, exception: Exception) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...


def _identity(value: int) -> int:
    return value


async def test_growing_target_releases_waiters() -> None:
    """A budget top-up must let queued work start, not leave it sleeping."""
    gate = ProcessSlots(target=1)
    await gate.acquire()

    waiter1 = asyncio.create_task(gate.acquire())
    waiter2 = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    assert not waiter1.done()
    assert not waiter2.done()

    await gate.set_target(3)
    await asyncio.wait_for(waiter1, timeout=1)
    await asyncio.wait_for(waiter2, timeout=1)
    assert gate.in_flight == 3

    await gate.release()
    await gate.release()
    await gate.release()


async def test_shrinking_target_blocks_new_grants_without_revoking_held_ones() -> None:
    """A shrunken budget must not yank slots out from under running work; it
    only stops new work from starting until the held slots drain naturally.
    """
    gate = ProcessSlots(target=3)
    await gate.acquire()
    await gate.acquire()

    await gate.set_target(1)
    assert gate.in_flight == 2  # held slots survive the shrink

    waiter = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    assert not waiter.done()

    await gate.release()
    await asyncio.sleep(0)
    assert not waiter.done()  # still over target: 1 held, target 1

    await gate.release()
    await asyncio.wait_for(waiter, timeout=1)
    assert gate.in_flight == 1
    await gate.release()


async def test_command_runner_and_process_executor_share_one_gate() -> None:
    """Both subprocess spawners must draw from the same ER-level gate.

    With a target of one, a subprocess held open by ``CommandRunner`` must
    block a ``ProcessExecutor`` submission until the subprocess exits — two
    independent pools, one machine budget.
    """
    gate = ProcessSlots(target=1)
    runner = CommandRunner(
        logger=_NoopLogger(), config=CommandRunnerConfig(), process_slots=gate
    )
    executor = ProcessExecutor(process_slots=gate)

    proc = await runner.run("sleep 0.3")

    async def _submit() -> int:
        with executor.activate():
            return await executor.submit(_identity, 42)

    submit_task = asyncio.create_task(_submit())
    await asyncio.sleep(0.1)
    assert not submit_task.done()

    await proc.wait_for_end()
    assert await asyncio.wait_for(submit_task, timeout=5) == 42


async def test_command_runner_releases_slot_when_process_exits() -> None:
    """The gate slot is held for the process's lifetime, not just `run()`'s
    body — releasing on return would let unbounded spawns pile up.
    """
    gate = ProcessSlots(target=1)
    runner = CommandRunner(
        logger=_NoopLogger(), config=CommandRunnerConfig(), process_slots=gate
    )

    proc = await runner.run("sleep 0.1")
    assert gate.in_flight == 1

    await proc.wait_for_end()
    # The release runs in a background task watching `proc.wait()`.
    for _ in range(100):
        if gate.in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert gate.in_flight == 0
