"""`AsyncProcess` tears down the whole tree it started on Windows.

Windows has no process groups, so a direct signal stops the child but orphans
everything it spawned -- and the orphan keeps running with the working
directory pinned, which surfaces as `WinError 32` when the test temp dir is
cleaned up. These tests pin the psutil tree kill that `terminate()` and
`kill()` route to there; the POSIX side lives in
`test_command_runner_teardown.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
from pathlib import Path

import pytest

from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.process_slots import ProcessSlots

# The inverse of `test_command_runner_teardown.py`: psutil is a Windows-only
# dependency of the runner, so these tests can only run where it is installed.
pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows tree teardown via psutil; POSIX teardown is covered by "
    "test_command_runner_teardown.py",
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


_SPAWNER = (
    "import os, subprocess, sys, time\n"
    "grandchild = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(300)']\n"
    ")\n"
    "print(os.getpid(), grandchild.pid, flush=True)\n"
    "time.sleep(300)\n"
)
"""A command with a child of its own, like an agent mid-tool-call. It reports
both pids on stdout so a test can check them."""


def _python(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _is_gone(pid: int) -> bool:
    import psutil  # type: ignore[import-untyped]  # noqa: PLC0415
    # Lazy: psutil is a Windows-only dependency, and this module is skipped
    # everywhere else -- a top-level import would break POSIX collection.

    return not psutil.pid_exists(pid)


async def _wait_gone(pid: int, timeout: float = 30.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _is_gone(pid):
            return True
        await asyncio.sleep(0.1)
    return _is_gone(pid)


@pytest.mark.asyncio
async def test_terminate_removes_the_child_and_its_grandchild() -> None:
    """Stopping the agent must also stop what the agent started.

    A child left running after its parent's timeout keeps write access to the
    project and pins the temp dir, which fails the cleanup with `WinError 32`
    -- the leak the interim skips in the agent suites were hiding.
    """
    process = await _runner().run(_python(_SPAWNER), new_process_group=True)

    lines = process.stdout_lines()
    child_pid, grandchild_pid = (
        int(part) for part in (await lines.__anext__()).split()
    )

    process.terminate()
    with contextlib.suppress(TimeoutError):
        await process.wait_for_end(timeout=30.0)

    assert await _wait_gone(child_pid), "the child survived terminate()"
    assert await _wait_gone(grandchild_pid), "the grandchild survived terminate()"


@pytest.mark.asyncio
async def test_terminate_after_exit_is_a_no_op() -> None:
    """A teardown ladder races the exit it is hoping for, and losing that race
    is the good outcome -- not an error to handle."""
    process = await _runner().run(_python("pass"), new_process_group=True)
    await process.wait_for_end(timeout=30.0)

    assert not process.is_alive()
    process.terminate()
    process.kill()


@pytest.mark.asyncio
async def test_a_wedged_tree_releases_its_working_directory(
    tmp_path: Path,
) -> None:
    """The shape that reddened the agent suites: a wedged run whose child holds
    the temp dir open must let go of it once torn down, so the directory can
    be removed without `WinError 32`."""
    work = tmp_path / "work"
    work.mkdir()

    process = await _runner().run(_python(_SPAWNER), cwd=work, new_process_group=True)
    lines = process.stdout_lines()
    child_pid, grandchild_pid = (
        int(part) for part in (await lines.__anext__()).split()
    )

    try:
        process.terminate()
        with contextlib.suppress(TimeoutError):
            await process.wait_for_end(timeout=30.0)

        assert await _wait_gone(child_pid), "the child survived terminate()"
        assert await _wait_gone(grandchild_pid), "the grandchild survived terminate()"
        shutil.rmtree(work)
        assert not work.exists()
    finally:
        process.kill()
        await _wait_gone(child_pid)
        await _wait_gone(grandchild_pid)
