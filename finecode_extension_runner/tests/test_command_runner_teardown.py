"""`AsyncProcess` can stop what it started, including what that started.

The trap these pin down is that a command is spawned through a shell, so the
process the runner holds may be a wrapper rather than the command itself. A
shell that forks rather than execs exits the moment it is signalled -- handing
back a returncode that reads exactly like a clean death -- while the command it
started keeps running. Anything deciding "is it gone yet?" from that exit code
therefore stops escalating precisely when escalation was still needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import sys

import pytest

from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
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


def _runner() -> CommandRunner:
    return CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=4),
    )


_STUBBORN = (
    "import os, signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "print(os.getpid(), child.pid, flush=True)\n"
    "time.sleep(300)\n"
)
"""A command that refuses SIGTERM and leaves a child behind, like an agent
mid-tool-call. It reports both pids on stdout so a test can check them."""


def _python(script: str) -> str:
    return f"{sys.executable} -c {shlex.quote(script)}"


def _is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return True
    return False


async def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    loop_deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < loop_deadline:
        if _is_gone(pid):
            return True
        await asyncio.sleep(0.05)
    return _is_gone(pid)


@pytest.mark.asyncio
async def test_kill_reaches_a_command_that_ignores_sigterm_and_its_child() -> None:
    process = await _runner().run(_python(_STUBBORN), new_process_group=True)

    lines = process.stdout_lines()
    agent_pid, child_pid = (int(part) for part in (await lines.__anext__()).split())

    process.terminate()
    with pytest.raises(TimeoutError):
        await process.wait_for_end(timeout=0.5)
    assert process.is_alive(), (
        "SIGTERM is ignored here, so the command is still running -- "
        "even though the shell that started it may already have exited"
    )

    process.kill()
    with contextlib.suppress(TimeoutError):
        await process.wait_for_end(timeout=2.0)

    assert await _wait_gone(agent_pid), "the command survived kill()"
    assert await _wait_gone(child_pid), "the command's child survived kill()"
    assert not process.is_alive()


@pytest.mark.asyncio
async def test_is_alive_does_not_trust_the_shells_exit_code() -> None:
    """The regression this file exists for.

    `get_exit_code()` reports the shell's fate, and signalling the group makes
    the shell exit while the command that ignores the signal runs on. A teardown
    that reads the exit code concludes "gone" and stops one rung too early.
    """
    process = await _runner().run(_python(_STUBBORN), new_process_group=True)
    lines = process.stdout_lines()
    agent_pid, child_pid = (int(part) for part in (await lines.__anext__()).split())

    try:
        process.terminate()
        await asyncio.sleep(0.3)

        assert process.is_alive()
        assert not _is_gone(agent_pid)
    finally:
        process.kill()
        await _wait_gone(agent_pid)
        await _wait_gone(child_pid)


@pytest.mark.asyncio
async def test_terminate_stops_an_ordinary_command_without_a_process_group() -> None:
    """The default spawn keeps the old signal semantics: no session of its own,
    and `terminate()` signals the process rather than a group."""
    process = await _runner().run(_python("import time; time.sleep(300)"))

    process.terminate()
    with contextlib.suppress(TimeoutError):
        await process.wait_for_end(timeout=2.0)

    assert not process.is_alive()
    assert process.get_exit_code() is not None


@pytest.mark.asyncio
async def test_signals_after_exit_are_a_no_op() -> None:
    """A teardown ladder races the exit it is hoping for, and losing that race
    is the good outcome -- not an error to handle."""
    process = await _runner().run(_python("pass"), new_process_group=True)
    await process.wait_for_end(timeout=5.0)

    assert not process.is_alive()
    process.terminate()
    process.kill()
