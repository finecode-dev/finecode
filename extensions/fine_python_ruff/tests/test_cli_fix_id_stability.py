"""Tests for the stability of ruff CLI-path fix ids across unrelated file edits."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from fine_lint.get_lint_fixes_action import GetLintFixesRunPayload
from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import NoOpLogger

from fine_python_ruff.get_lint_fixes_handler import (
    RuffGetLintFixesHandler,
    RuffGetLintFixesHandlerConfig,
)

_FILE_PATH = Path("/tmp/subject.py")
_FILE_URI = path_to_resource_uri(_FILE_PATH)


class _StubProcess:
    def __init__(self, output: str) -> None:
        self._output = output

    def get_exit_code(self) -> int | None:
        return 0

    def get_output(self) -> str:
        return self._output

    def get_error_output(self) -> str:
        return ""

    def write_to_stdin(self, value: str) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self.get_output().splitlines():
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        for line in self.get_error_output().splitlines():
            yield line

    async def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _StubCommandRunner:
    """Answers every command with the same canned ruff CLI JSON output."""

    def __init__(self, output: str) -> None:
        self._output = output
        self.commands: list[list[str]] = []

    async def run(self, cmd: icommandrunner.Argv, cwd=None, env=None):
        icommandrunner.check_argv(cmd)
        self.commands.append(list(cmd))
        return _StubProcess(self._output)

    def run_sync(self, cmd: icommandrunner.Argv, cwd=None, env=None):
        raise NotImplementedError


def _make_handler(output: str) -> RuffGetLintFixesHandler:
    return RuffGetLintFixesHandler(
        config=RuffGetLintFixesHandlerConfig(use_cli=True),
        logger=NoOpLogger(),
        file_editor=None,  # type: ignore[arg-type]  # unused: _run_cli_fixes takes content directly
        command_runner=_StubCommandRunner(output),  # type: ignore[arg-type]
        project_info_provider=None,  # type: ignore[arg-type]  # unused by the CLI path
        action_runner=None,  # type: ignore[arg-type]  # only touched by target-version resolution
        lsp_service=None,  # type: ignore[arg-type]  # CLI path never touches the LSP service
    )


def _unused_import_violation(row: int, module: str) -> dict:
    """A ruff F401 violation with a real 'remove unused import' fix attached."""
    return {
        "code": "F401",
        "location": {"row": row, "column": 1},
        "end_location": {"row": row, "column": len(module) + 1},
        "fix": {
            "applicability": "safe",
            "message": f"Remove unused import: `{module}`",
            "edits": [
                {
                    "content": "",
                    "location": {"row": row, "column": 1},
                    "end_location": {"row": row + 1, "column": 1},
                }
            ],
        },
    }


async def test_fix_id_for_a_violation_is_unchanged_when_an_earlier_one_is_removed() -> (
    None
):
    """A fix's identity must not depend on which other fixes ruff also reported.

    ``fix_id`` is the key a later ``codeAction/resolve`` uses to recover a fix's
    edits by re-running the linter and matching on it. If removing or fixing an
    unrelated, earlier violation changed the id of a later one, resolve would
    recover the wrong fix's edits (or none at all) for every fix that was not first
    in the file.
    """
    payload = GetLintFixesRunPayload(file_path=_FILE_URI)

    both_violations = [
        _unused_import_violation(1, "os"),
        _unused_import_violation(2, "sys"),
    ]
    only_later_violation = [_unused_import_violation(2, "sys")]

    handler_with_both = _make_handler(json.dumps(both_violations))
    fixes_with_both = await handler_with_both._run_cli_fixes(
        _FILE_PATH, "import os\nimport sys\n", payload
    )

    handler_with_one = _make_handler(json.dumps(only_later_violation))
    fixes_with_one = await handler_with_one._run_cli_fixes(
        _FILE_PATH, "import sys\n", payload
    )

    later_fix_with_both = next(
        f for f in fixes_with_both if f.target_range.start.line == 1
    )
    later_fix_with_one = next(
        f for f in fixes_with_one if f.target_range.start.line == 1
    )

    assert later_fix_with_one.fix_id == later_fix_with_both.fix_id
