from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from finecode_extension_api.interfaces.icommandrunner import ICommandRunner
from finecode_extension_runner.testing import run_handler

from fine_git.git_push_git_refs_handler import GitPushGitRefsHandler
from fine_git.push_git_refs_action import PushGitRefsAction, PushGitRefsRunPayload


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


@pytest.mark.asyncio
async def test_git_push_failure_is_reported_without_raising() -> None:
    """A failing `git push` is reported as an empty `pushed_refs` list with the stderr captured, never raised, so a network or permission fault while pushing a release tag can be logged and swallowed by the release sweep instead of crashing it."""
    command_runner = FakeCommandRunner(
        results=[FakeCommandResult(exit_code=1, stderr="remote: permission denied")]
    )
    payload = PushGitRefsRunPayload(refs=["pkg-a@1.0.0"])

    result = await run_handler(
        GitPushGitRefsHandler,
        payload,
        action_cls=PushGitRefsAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.pushed_refs == []
    assert result.error == "remote: permission denied"
    assert command_runner.commands
