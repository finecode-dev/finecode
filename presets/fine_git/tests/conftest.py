from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path


@dataclasses.dataclass
class FakeCommandResult:
    """Stands in for ``IAsyncProcess``: the handler reads exit code / output
    through these accessor methods, not raw attributes."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def get_exit_code(self) -> int | None:
        return self.exit_code

    def get_output(self) -> str:
        return self.stdout

    def get_error_output(self) -> str:
        return self.stderr

    def write_to_stdin(self, value: str) -> None:
        return None

    def close_stdin(self) -> None:
        return None

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self.get_output().splitlines():
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        for line in self.get_error_output().splitlines():
            yield line

    async def wait_for_end(self, timeout: float | None = None) -> None:
        return None


class FakeCommandRunner:
    def __init__(self, results: list[FakeCommandResult]) -> None:
        self._results = list(results)
        self.commands: list[str] = []

    async def run(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> FakeCommandResult:
        self.commands.append(cmd)
        return self._results.pop(0)
