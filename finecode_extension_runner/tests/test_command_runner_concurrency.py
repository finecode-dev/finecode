"""`CommandRunner` bounds how many spawned subprocesses are *alive* at once
(Layer 2 of the prepare-envs concurrency fix — see ADR-0055/ADR-0056).

The semaphore is acquired before spawning and released only when the process
actually exits (via a background task watching `proc.wait()`), not when
`run()` returns — `run()` only spawns and returns immediately, so a bound
scoped to `run()`'s body alone would release almost instantly and fail to
bound concurrent-alive-subprocess count.
"""
from __future__ import annotations

import asyncio

from finecode_extension_runner.impls import command_runner as command_runner_module
from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
    resolve_command_runner_concurrency,
)


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
        logger=_NoopLogger(), config=CommandRunnerConfig(max_concurrent_processes=1)
    )
    loop = asyncio.get_running_loop()

    task1 = asyncio.create_task(runner.run("sleep 0.3"))
    # Give task1 a head start so it acquires the semaphore first.
    await asyncio.sleep(0.05)
    task2 = asyncio.create_task(runner.run("sleep 0.01"))

    start1 = loop.time()
    proc1 = await task1
    elapsed1 = loop.time() - start1

    start2 = loop.time()
    proc2 = await task2
    elapsed2 = loop.time() - start2

    # task1's run() returns quickly — it acquired the semaphore immediately.
    assert elapsed1 < 0.2
    # task2's run() had to wait for task1's *process* (not just task1's run()
    # call) to actually exit before it could acquire the semaphore and spawn.
    assert elapsed2 > 0.15

    await proc1.wait_for_end()
    await proc2.wait_for_end()


async def test_two_processes_run_concurrently_when_budget_allows_it() -> None:
    runner = CommandRunner(
        logger=_NoopLogger(), config=CommandRunnerConfig(max_concurrent_processes=2)
    )
    loop = asyncio.get_running_loop()

    start = loop.time()
    proc1 = await runner.run("sleep 0.2")
    proc2 = await runner.run("sleep 0.2")
    elapsed = loop.time() - start

    # Both spawn without waiting on each other.
    assert elapsed < 0.15

    await proc1.wait_for_end()
    await proc2.wait_for_end()


async def test_config_without_explicit_limit_falls_back_to_machine_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        command_runner_module,
        "default_layered_concurrency",
        lambda: 2,
    )

    runner = CommandRunner(logger=_NoopLogger(), config=CommandRunnerConfig())

    assert runner._semaphore._value == 2


def test_resolve_prefers_configured_value(monkeypatch) -> None:
    monkeypatch.setattr(
        command_runner_module, "default_layered_concurrency", lambda: 3
    )

    decision = resolve_command_runner_concurrency(4)
    assert decision.value == 4
    assert "config" in decision.source


def test_resolve_clamps_non_positive_value_to_one(monkeypatch) -> None:
    monkeypatch.setattr(
        command_runner_module, "default_layered_concurrency", lambda: 3
    )

    assert resolve_command_runner_concurrency(0).value == 1
    assert resolve_command_runner_concurrency(-2).value == 1


def test_resolve_falls_back_to_default_when_unset(monkeypatch) -> None:
    monkeypatch.setattr(
        command_runner_module, "default_layered_concurrency", lambda: 3
    )

    decision = resolve_command_runner_concurrency(None)
    assert decision.value == 3
    assert "default" in decision.source
