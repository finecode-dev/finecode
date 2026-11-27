"""A daemon that escapes by setsid outlives the scope; `dmypy stop` ends it.

Why it matters: the scope's stop must not reach helpers that detach
themselves (dmypy daemonizes out of the group), while an explicit stop still
works afterwards.
"""

from __future__ import annotations

import pathlib
import shutil

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


@pytest.mark.skipif(shutil.which("dmypy") is None, reason="dmypy not installed")
async def test_dmypy_daemon_survives_scope_and_stop_succeeds(
    tmp_path: pathlib.Path,
) -> None:
    """`dmypy run` under the scope leaves the daemon alive; `stop` ends it."""
    target = tmp_path / "mod.py"
    target.write_text("x: int = 1\n", encoding="utf-8")
    runner = _runner()

    async with runner.run(["dmypy", "run", "--", str(target)], cwd=tmp_path) as proc:
        await proc.wait_for_end()
        assert proc.get_exit_code() == 0, proc.get_error_output()

    status = await runner.run_to_completion(["dmypy", "status"], cwd=tmp_path)
    assert status.get_exit_code() == 0
    assert "running" in status.get_output().lower()

    stop = await runner.run_to_completion(["dmypy", "stop"], cwd=tmp_path)
    assert stop.get_exit_code() == 0, stop.get_error_output()
