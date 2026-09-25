"""`CommandRunner` draws from the ER's shared process-slot gate (ADR-0090).

The slot is acquired before spawning and released only when the process
actually exits (via a background task watching `proc.wait()`), not when
`run()` returns — `run()` only spawns and returns immediately, so a bound
scoped to `run()`'s body alone would release almost instantly and fail to
bound concurrent-alive-subprocess count.

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


async def test_second_run_does_not_spawn_until_first_process_exits() -> None:
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=1),
    )
    loop = asyncio.get_running_loop()

    task1 = asyncio.create_task(
        runner.run([sys.executable, "-c", "import time; time.sleep(0.3)"])
    )
    # Give task1 a head start so it acquires the slot first.
    await asyncio.sleep(0.05)
    task2 = asyncio.create_task(
        runner.run([sys.executable, "-c", "import time; time.sleep(0.01)"])
    )

    start1 = loop.time()
    proc1 = await task1
    elapsed1 = loop.time() - start1

    start2 = loop.time()
    proc2 = await task2
    elapsed2 = loop.time() - start2

    # task1's run() returns quickly — it acquired the slot immediately.
    assert elapsed1 < 0.2
    # task2's run() had to wait for task1's *process* (not just task1's run()
    # call) to actually exit before it could acquire the slot and spawn.
    assert elapsed2 > 0.15

    await proc1.wait_for_end()
    await proc2.wait_for_end()


async def test_two_processes_run_concurrently_when_budget_allows_it() -> None:
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=2),
    )
    loop = asyncio.get_running_loop()

    start = loop.time()
    proc1 = await runner.run([sys.executable, "-c", "import time; time.sleep(0.2)"])
    proc2 = await runner.run([sys.executable, "-c", "import time; time.sleep(0.2)"])
    elapsed = loop.time() - start

    # Both spawn without waiting on each other.
    assert elapsed < 0.15

    await proc1.wait_for_end()
    await proc2.wait_for_end()


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

    task1 = asyncio.create_task(
        runner.run([sys.executable, "-c", "import time; time.sleep(0.2)"])
    )
    await asyncio.sleep(0.05)
    task2 = asyncio.create_task(
        runner.run([sys.executable, "-c", "import time; time.sleep(0.01)"])
    )

    start2 = loop.time()
    proc2 = await task2
    elapsed2 = loop.time() - start2

    assert elapsed2 > 0.1

    proc1 = await task1
    await proc1.wait_for_end()
    await proc2.wait_for_end()


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
