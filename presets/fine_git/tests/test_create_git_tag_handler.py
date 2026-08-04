from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fine_git.create_git_tag_action import (
    CreateGitTagAction,
    CreateGitTagRunPayload,
)
from fine_git.git_create_git_tag_handler import GitCreateGitTagHandler
from finecode_extension_api.interfaces.icommandrunner import ICommandRunner
from finecode_extension_runner.testing import run_handler


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
async def test_existing_tag_is_a_no_op_and_never_invokes_git_tag() -> None:
    """Creating a tag that already exists is a no-op rather than an error, so a retried release step is idempotent and never overwrites a tag another run already wrote."""
    command_runner = FakeCommandRunner(results=[FakeCommandResult(exit_code=0)])
    payload = CreateGitTagRunPayload(tag="pkg-a@1.0.0")

    result = await run_handler(
        GitCreateGitTagHandler,
        payload,
        action_cls=CreateGitTagAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.created is False
    assert result.error is None
    assert all(
        "tag" not in cmd or "rev-parse" in cmd for cmd in command_runner.commands
    )
    assert not any(
        "-a" in cmd and "rev-parse" not in cmd for cmd in command_runner.commands
    )


@pytest.mark.asyncio
async def test_git_tag_failure_is_reported_without_raising() -> None:
    """A failing `git tag` invocation is reported as `created=False` with the underlying stderr, never raised, so a git-level fault during tagging can be logged and swallowed by the release sweep."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=1, stderr="tag not found"),
            FakeCommandResult(exit_code=128, stderr="unable to create tag object"),
        ]
    )
    payload = CreateGitTagRunPayload(
        tag="pkg-a@1.0.0", message="Release pkg-a 1.0.0 (pypi)"
    )

    result = await run_handler(
        GitCreateGitTagHandler,
        payload,
        action_cls=CreateGitTagAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.created is False
    assert result.error == "unable to create tag object"
