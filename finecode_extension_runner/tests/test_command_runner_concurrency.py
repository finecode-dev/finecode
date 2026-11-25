"""`CommandRunner` draws from the ER's shared process-slot gate (ADR-0090).

The slot is acquired at scope entry and released when the scope exits after
the process (or its group) is gone, so the gate bounds how many subprocesses
are alive at once, which is what actually causes resource contention.

``CommandRunnerConfig.max_concurrent_processes`` survives as an *optional*
local ceiling on top of the shared gate, for a project that wants to pin a
noisy ER below the machine budget.
"""

from __future__ import annotations

import asyncio
import sys

from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
    resolve_command_runner_concurrency,
)
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


async def test_second_scope_waits_until_first_scope_exits() -> None:
    """The gate is held for the scope's life, not just until spawn returns."""
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=1),
    )
    loop = asyncio.get_running_loop()

    async def enter_second() -> None:
        async with runner.run([sys.executable, "-c", "import time; time.sleep(0.01)"]) as proc2:
            await proc2.wait_for_end()

    start1 = loop.time()
    async with runner.run(
        [sys.executable, "-c", "import time; time.sleep(0.3)"]
    ) as proc1:
        elapsed1 = loop.time() - start1
        # The first scope entered quickly — it acquired the slot immediately.
        assert elapsed1 < 0.2
        start2 = loop.time()
        task2 = asyncio.create_task(enter_second())
        # Give task2 a chance to block on the held slot.
        await asyncio.sleep(0.05)
        assert not task2.done()
        await proc1.wait_for_end()
    # Leaving the first scope released the slot; the second scope ran after.
    await asyncio.wait_for(task2, timeout=5.0)
    assert loop.time() - start2 > 0.2


async def test_two_processes_run_concurrently_when_budget_allows_it() -> None:
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=2),
    )
    loop = asyncio.get_running_loop()

    start = loop.time()
    async with runner.run(
        [sys.executable, "-c", "import time; time.sleep(0.2)"]
    ) as proc1:
        async with runner.run(
            [sys.executable, "-c", "import time; time.sleep(0.2)"]
        ) as proc2:
            elapsed = loop.time() - start

            # Both spawn without waiting on each other.
            assert elapsed < 0.15
            await proc2.wait_for_end()
        await proc1.wait_for_end()


async def test_config_without_explicit_limit_has_no_local_cap() -> None:
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=4),
    )

    assert runner._local_cap is None


async def test_configured_limit_still_caps_below_the_gate() -> None:
    """The optional service-config ceiling must keep working even when the
    shared gate would allow more.
    """
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(max_concurrent_processes=1),
        process_slots=ProcessSlots(target=4),
    )
    loop = asyncio.get_running_loop()

    async def enter_second() -> None:
        async with runner.run(
            [sys.executable, "-c", "import time; time.sleep(0.01)"]
        ) as proc2:
            await proc2.wait_for_end()

    async with runner.run(
        [sys.executable, "-c", "import time; time.sleep(0.2)"]
    ) as proc1:
        start2 = loop.time()
        task2 = asyncio.create_task(enter_second())
        await asyncio.sleep(0.05)
        assert not task2.done()
        await proc1.wait_for_end()

    await asyncio.wait_for(task2, timeout=5.0)
    assert loop.time() - start2 > 0.1


def test_resolve_prefers_configured_value() -> None:
    decision = resolve_command_runner_concurrency(4)
    assert decision is not None
    assert decision.value == 4
    assert "config" in decision.source


def test_resolve_clamps_non_positive_value_to_one() -> None:
    assert resolve_command_runner_concurrency(0).value == 1
    assert resolve_command_runner_concurrency(-2).value == 1


def test_resolve_returns_none_when_unset() -> None:
    assert resolve_command_runner_concurrency(None) is None


class _BudgetBackend:
    """Fake WM budget: hands out lease ids and tracks which are still held.

    While ``hold`` is set and not yet signalled, a lease request stays pending.
    """

    def __init__(self) -> None:
        self.hold: asyncio.Event | None = None
        self.release_hold: asyncio.Event | None = None
        self._issued = 0
        self.outstanding: set[str] = set()

    async def lease(self) -> str:
        if self.hold is not None:
            await self.hold.wait()
        self._issued += 1
        lease_id = f"L{self._issued}"
        self.outstanding.add(lease_id)
        return lease_id

    async def release(self, lease_id: str) -> None:
        if self.release_hold is not None:
            await self.release_hold.wait()
        self.outstanding.remove(lease_id)


def _runner_with_budget(
    target: int, local_cap: int
) -> tuple[CommandRunner, ProcessSlots, _BudgetBackend]:
    slots = ProcessSlots(target=target)
    backend = _BudgetBackend()
    slots.attach_budget(backend.lease, backend.release)
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(max_concurrent_processes=local_cap),
        process_slots=slots,
    )
    return runner, slots, backend


async def _cancel_and_settle(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Let cleanup tasks scheduled by the cancellation run.
    for _ in range(5):
        await asyncio.sleep(0)


async def test_run_cancelled_while_waiting_for_lease_holds_nothing() -> None:
    runner, slots, backend = _runner_with_budget(target=2, local_cap=1)
    backend.hold = asyncio.Event()
    assert runner._local_cap is not None
    permits_before = runner._local_cap._value

    async def enter() -> None:
        async with runner.run([sys.executable, "-c", "pass"]):
            pass  # pragma: no cover

    task = asyncio.create_task(enter())
    await asyncio.sleep(0.05)
    assert slots.in_flight == 1

    await _cancel_and_settle(task)

    assert slots.in_flight == 0
    assert runner._local_cap._value == permits_before
    # The pending lease request is released once the WM grants it.
    backend.hold.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert backend._issued == 1
    assert backend.outstanding == set()


async def test_run_cancelled_while_waiting_for_local_cap_releases_its_lease() -> None:
    runner, slots, backend = _runner_with_budget(target=2, local_cap=1)
    assert runner._local_cap is not None

    # The first process holds the only local-cap permit.
    async with runner.run([sys.executable, "-c", "import time; time.sleep(5)"]) as proc1:
        in_flight_before = slots.in_flight
        permits_before = runner._local_cap._value
        leases_before = set(backend.outstanding)

        async def enter() -> None:
            async with runner.run([sys.executable, "-c", "pass"]):
                pass  # pragma: no cover

        task = asyncio.create_task(enter())
        await asyncio.sleep(0.05)
        # Lease granted, now blocked on the local-cap permit.
        assert slots.in_flight == in_flight_before + 1
        assert len(backend.outstanding) == len(leases_before) + 1

        await _cancel_and_settle(task)

        assert slots.in_flight == in_flight_before
        assert runner._local_cap._value == permits_before
        assert backend.outstanding == leases_before


async def test_entry_cancel_returns_slot_without_waiting_for_wm_release() -> None:
    """A cancelled entry frees local state even when the WM never answers.

    If the local slot stayed held until the WM acknowledged the release, a
    stalled WM would wedge the runner; the lease is released in the
    background instead."""
    runner, slots, backend = _runner_with_budget(target=2, local_cap=1)
    assert runner._local_cap is not None

    async with runner.run([sys.executable, "-c", "import time; time.sleep(5)"]):
        in_flight_before = slots.in_flight
        permits_before = runner._local_cap._value
        backend.release_hold = asyncio.Event()

        async def enter() -> None:
            async with runner.run([sys.executable, "-c", "pass"]):
                pass  # pragma: no cover

        task = asyncio.create_task(enter())
        await asyncio.sleep(0.05)
        assert slots.in_flight == in_flight_before + 1
        leases_before = set(backend.outstanding)

        loop = asyncio.get_running_loop()
        start = loop.time()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        elapsed = loop.time() - start
        try:
            assert elapsed < 0.5
            assert slots.in_flight == in_flight_before
            assert runner._local_cap._value == permits_before
            # WM release still pending in the background.
            assert len(backend.outstanding) == len(leases_before)
        finally:
            backend.release_hold.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if len(backend.outstanding) == len(leases_before) - 1:
                break
        assert len(backend.outstanding) == len(leases_before) - 1
