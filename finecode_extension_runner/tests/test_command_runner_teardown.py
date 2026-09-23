"""`AsyncProcess` can stop what it started, including what that started.

The trap these pin down is that the command the runner holds may be a launcher
rather than the final worker: a launcher that forks rather than execs -- npm,
a `.cmd` shim, a driver script -- exits the moment it is signalled, handing
back a returncode that reads exactly like a clean death, while what it started
keeps running. Anything deciding "is it gone yet?" from that exit code
therefore stops escalating precisely when escalation was still needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

import pytest

from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.process_slots import ProcessSlots


# Process groups, `killpg`/`SIGKILL` and `os.kill(pid, 0)` (which is
# `CTRL_C_EVENT` on Windows) are all POSIX-only.
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="process groups and killpg are POSIX-only; os.kill(pid, 0) is "
    "CTRL_C_EVENT on Windows",
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

# A launcher that dies on SIGTERM but leaves a SIGTERM-ignoring child behind in
# its own group -- the shape a forking wrapper takes. SIGTERM is blocked before
# the fork so the child inherits the blocked disposition from birth (no race on
# the child installing a handler); unblocking afterwards returns the signal to
# the launcher alone.
_LAUNCHER = (
    "import os, signal, subprocess, sys, time\n"
    "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n"
    "print(os.getpid(), child.pid, flush=True)\n"
    "time.sleep(300)\n"
)


def _python(script: str) -> list[str]:
    return [sys.executable, "-c", script]


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
async def test_is_alive_while_the_command_ignores_sigterm() -> None:
    """The regression this file exists for.

    `get_exit_code()` reports the command's own fate, and signalling the group
    makes a process that ignores the signal sit still while a wrapper would
    have exited. A teardown that reads the exit code concludes "gone" and
    stops one rung too early.
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
async def test_is_alive_tracks_the_group_after_a_forking_launcher_exits() -> None:
    """A launcher that dies on SIGTERM must not read as "everything is gone".

    A forking launcher (npm, a `.cmd` shim) exits as soon as it is signalled
    while what it started keeps running in the same group: a teardown that
    stops at the launcher's exit code orphans the work still running under it.
    """
    process = await _runner().run(_python(_LAUNCHER), new_process_group=True)
    lines = process.stdout_lines()
    launcher_pid, child_pid = (int(part) for part in (await lines.__anext__()).split())

    process.terminate()

    # `wait_for_end()` cannot return here: the child holds the inherited
    # stdout pipe open, so the drain never sees EOF. Poll the exit code
    # instead -- it is set the moment the launcher itself dies.
    deadline = asyncio.get_running_loop().time() + 5.0
    while (
        process.get_exit_code() is None
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)

    assert process.get_exit_code() is not None
    assert process.is_alive(), (
        "the child that ignored SIGTERM is still a member of the launcher's "
        "group, so the group outlives the launcher"
    )

    process.kill()
    assert await _wait_gone(launcher_pid), "the launcher survived kill()"
    assert await _wait_gone(child_pid), "the child survived kill()"
    assert not process.is_alive()


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


@pytest.mark.asyncio
async def test_signalling_a_group_that_refuses_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group that refuses the signal is not a teardown failure.

    A group may contain a member the caller cannot signal (a setuid child, or
    a zombie-only group on macOS), and that member is also one the teardown
    could not have stopped by any means. An error here would surface to the
    operator as a failed stop for a command that was already being torn down.
    """
    process = await _runner().run(
        _python("import time; time.sleep(30)"), new_process_group=True
    )

    def fake(_pgid: int, sig: int) -> None:
        if sig == 0:
            return
        raise PermissionError()

    try:
        monkeypatch.setattr(os, "killpg", fake)
        process.terminate()
        process.kill()
    finally:
        monkeypatch.undo()
        process.kill()
        await process.wait_for_end(timeout=5.0)
