"""The ER's resizable process-slot gate (ADR-0090).

One gate is shared by ``CommandRunner`` and ``ProcessExecutor`` inside an ER.
It must throttle without ever revoking a slot that was already granted, and it
must let waiters through when the target grows.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from loguru import logger

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

    proc = await runner.run([sys.executable, "-c", "import time; time.sleep(0.3)"])

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

    proc = await runner.run([sys.executable, "-c", "import time; time.sleep(0.1)"])
    assert gate.in_flight == 1

    await proc.wait_for_end()
    # The release runs in a background task watching `proc.wait()`.
    for _ in range(100):
        if gate.in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert gate.in_flight == 0


class _ImmediateBackend:
    """A WM backend granting every lease at once, recording both directions."""

    def __init__(self) -> None:
        self.leases: list[str] = []
        self.releases: list[str] = []
        self._counter = 0

    async def lease(self) -> str:
        self._counter += 1
        lease_id = f"lease-{self._counter}"
        self.leases.append(lease_id)
        return lease_id

    async def release(self, lease_id: str) -> None:
        self.releases.append(lease_id)


class _Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: object) -> None:
        self.messages.append(str(message))


async def test_acquire_returns_its_lease_and_release_returns_it() -> None:
    """Each holder hands back exactly the lease it was given."""
    gate = ProcessSlots(target=8)
    backend = _ImmediateBackend()
    gate.attach_budget(backend.lease, backend.release)

    lease_id = await gate.acquire()
    assert lease_id == "lease-1"
    assert backend.leases == ["lease-1"]

    await gate.release(lease_id)
    assert backend.releases == ["lease-1"]
    assert gate.in_flight == 0


async def test_overlapping_acquires_keep_their_own_ids() -> None:
    """Releases carry their own acquire's id even when they overlap."""
    gate = ProcessSlots(target=8)
    backend = _ImmediateBackend()
    gate.attach_budget(backend.lease, backend.release)

    first = await gate.acquire()
    second = await gate.acquire()
    await gate.release(first)
    await gate.release(second)
    assert backend.releases == [first, second]
    assert gate.in_flight == 0


async def test_backend_cap_bounds_concurrent_work() -> None:
    """A backend granting one at a time serializes work the local gate allows."""
    gate = ProcessSlots(target=8)
    capacity = asyncio.Semaphore(1)
    releases: list[str] = []
    counter = 0
    active = 0
    peak = 0

    async def lease() -> str:
        nonlocal counter
        await capacity.acquire()
        counter += 1
        return f"lease-{counter}"

    async def release(lease_id: str) -> None:
        releases.append(lease_id)
        capacity.release()

    gate.attach_budget(lease, release)

    async def _unit() -> None:
        nonlocal active, peak
        lease_id = await gate.acquire()
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        await gate.release(lease_id)

    await asyncio.gather(*(_unit() for _ in range(4)))
    assert peak == 1
    assert sorted(releases) == [f"lease-{n}" for n in range(1, 5)]
    assert gate.in_flight == 0


async def test_failed_lease_keeps_the_local_slot() -> None:
    """A throttle never refuses work: a failed lease still runs locally."""
    gate = ProcessSlots(target=8)

    async def lease() -> str:
        raise RuntimeError("WM unreachable")

    releases: list[str] = []

    async def release(lease_id: str) -> None:
        releases.append(lease_id)

    gate.attach_budget(lease, release)
    warnings = _Warnings()
    sink = logger.add(warnings)
    try:
        assert await gate.acquire() is None
    finally:
        logger.remove(sink)
    assert sum("Process budget lease failed" in m for m in warnings.messages) == 1
    assert gate.in_flight == 1

    await gate.release(None)
    assert releases == []
    assert gate.in_flight == 0


async def test_cancelled_acquire_returns_its_grant() -> None:
    """A grant landing after its acquire was cancelled is still released."""
    gate = ProcessSlots(target=8)
    pending: list[asyncio.Future[str]] = []
    releases: list[str] = []

    async def lease() -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending.append(future)
        return await future

    async def release(lease_id: str) -> None:
        releases.append(lease_id)

    gate.attach_budget(lease, release)

    task = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0.05)
    assert gate.in_flight == 1
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    for _ in range(100):
        if gate.in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert gate.in_flight == 0

    pending[0].set_result("late-grant")
    for _ in range(100):
        if releases == ["late-grant"]:
            break
        await asyncio.sleep(0.01)
    assert releases == ["late-grant"]

    pending_future = gate.acquire()
    second = asyncio.create_task(pending_future)
    await asyncio.sleep(0.05)
    pending[1].set_result("next-grant")
    assert await asyncio.wait_for(second, timeout=5) == "next-grant"
    await gate.release("next-grant")
    assert releases == ["late-grant", "next-grant"]
    assert gate.in_flight == 0


async def test_failed_release_warns_but_returns_the_slot() -> None:
    """A release the WM never acknowledges must still free local capacity."""
    gate = ProcessSlots(target=1)
    backend = _ImmediateBackend()

    async def release(lease_id: str) -> None:
        backend.releases.append(lease_id)
        raise RuntimeError("WM unreachable")

    gate.attach_budget(backend.lease, release)
    lease_id = await gate.acquire()

    warnings = _Warnings()
    sink = logger.add(warnings)
    try:
        await gate.release(lease_id)
    finally:
        logger.remove(sink)
    assert sum("Process budget release failed" in m for m in warnings.messages) == 1
    assert gate.in_flight == 0
