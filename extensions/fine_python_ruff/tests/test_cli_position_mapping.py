"""Tests that the ruff CLI path reports positions where the LSP path reports them.

Ruff's JSON counts rows and columns from 1, LSP counts lines and characters from 0.
Both paths answer the same questions -- ``lint_files`` diagnostics and ``get_lint_fixes``
fixes -- and a caller cannot see which one produced an answer, so a range that shifts
with ``use_cli`` underlines the wrong span in an editor and slips past range filters
built from the other path's numbers.

The numbers below are ruff's own, measured on ``import os``: the CLI puts ``os`` at
columns 8..10, the server puts it at characters 7..9.
"""

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
from fine_python_ruff.lint_files_handler import map_ruff_violation_to_lint_message

_FILE_PATH = Path("/tmp/subject.py")
_FILE_URI = path_to_resource_uri(_FILE_PATH)

_UNUSED_IMPORT = {
    "code": "F401",
    "message": "`os` imported but unused",
    "location": {"row": 1, "column": 8},
    "end_location": {"row": 1, "column": 10},
    "fix": {
        "applicability": "safe",
        "message": "Remove unused import: `os`",
        "edits": [
            {
                "content": "",
                "location": {"row": 1, "column": 1},
                "end_location": {"row": 2, "column": 1},
            }
        ],
    },
}


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
    def __init__(self, output: str) -> None:
        self._output = output
        self.commands: list[list[str]] = []

    async def run(self, cmd: icommandrunner.Argv, cwd=None, env=None):
        icommandrunner.check_argv(cmd)
        self.commands.append(list(cmd))
        return _StubProcess(self._output)

    def run_sync(self, cmd: icommandrunner.Argv, cwd=None, env=None):
        raise NotImplementedError


def test_a_cli_diagnostic_starts_where_the_server_says_it_does() -> None:
    diagnostic = map_ruff_violation_to_lint_message(_UNUSED_IMPORT)

    assert (diagnostic.range.start.character, diagnostic.range.end.character) == (7, 9)
    assert (diagnostic.range.start.line, diagnostic.range.end.line) == (0, 0)


async def test_a_cli_fix_targets_the_range_the_server_would_have_given() -> None:
    handler = RuffGetLintFixesHandler(
        config=RuffGetLintFixesHandlerConfig(use_cli=True),
        logger=NoOpLogger(),
        file_editor=None,  # type: ignore[arg-type]  # _run_cli_fixes takes content directly
        command_runner=_StubCommandRunner(json.dumps([_UNUSED_IMPORT])),  # type: ignore[arg-type]
        project_info_provider=None,  # type: ignore[arg-type]  # unused by the CLI path
        action_runner=None,  # type: ignore[arg-type]  # only touched by target-version resolution
        lsp_service=None,  # type: ignore[arg-type]  # CLI path never touches the LSP service
    )

    fixes = await handler._run_cli_fixes(
        _FILE_PATH, "import os\n", GetLintFixesRunPayload(file_path=_FILE_URI)
    )

    (fix,) = fixes
    assert (fix.target_range.start.character, fix.target_range.end.character) == (7, 9)
    # the edit deletes the whole line, which ruff states as columns 1..1 of rows 1..2
    (edit,) = fix.edits[_FILE_URI]
    assert (edit.range.start.line, edit.range.start.character) == (0, 0)
    assert (edit.range.end.line, edit.range.end.character) == (1, 0)
