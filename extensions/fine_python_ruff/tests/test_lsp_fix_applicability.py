"""Tests for what the LSP path claims about applying a fix unattended.

Ruff sends no applicability over LSP -- a safe fix and an unsafe one arrive with the
same structure -- and it offers unsafe fixes as quickfixes whatever the configuration.
Whatever the handler labels them with is therefore what ``apply_lint_fixes`` acts on:
call an unsafe fix safe and a run that was not asked for unsafe fixes rewrites code
ruff itself would not touch without ``--unsafe-fixes``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fine_lint.get_lint_fixes_action import GetLintFixesRunPayload
from fine_lint.lint_fix import FixApplicability
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import NoOpLogger

from fine_python_ruff.get_lint_fixes_handler import (
    RuffGetLintFixesHandler,
    RuffGetLintFixesHandlerConfig,
)

_FILE_PATH = Path("/tmp/subject.py")
_FILE_URI = path_to_resource_uri(_FILE_PATH)
_CONTENT = "import os\n\n\ndef f():\n    x = 1\n    return 2\n"


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
        self.commands: list[Any] = []

    async def run(self, cmd, cwd=None, env=None):
        self.commands.append(cmd)
        return _StubProcess(self._output)

    def run_sync(self, cmd, cwd=None, env=None):
        raise NotImplementedError


class _StubLspService:
    """Answers with canned ruff code actions, as ruff 0.16 shapes them."""

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = actions

    async def ensure_started(self, root_uri: str, meta: Any) -> None:
        pass

    def add_settings_provider(self, provider: Any) -> None:
        pass

    async def get_code_actions(self, *args: Any, **kwargs: Any):
        return self._actions


class _StubProjectInfoProvider:
    def get_current_project_dir_path(self) -> Path:
        return _FILE_PATH.parent


def _code_action(title: str, code: str, line: int, character: int, new_text: str):
    return {
        "title": title,
        "kind": "quickfix",
        "data": str(_FILE_PATH),
        "diagnostics": [
            {
                "code": code,
                "range": {
                    "start": {"line": line, "character": character},
                    "end": {"line": line, "character": character + 2},
                },
            }
        ],
        "edit": {
            "changes": {
                _FILE_URI: [
                    {
                        "range": {
                            "start": {"line": line, "character": character},
                            "end": {"line": line + 1, "character": 0},
                        },
                        "newText": new_text,
                    }
                ]
            }
        },
    }


def _violation(code: str, row: int, column: int, applicability: str) -> dict[str, Any]:
    return {
        "code": code,
        "location": {"row": row, "column": column},
        "end_location": {"row": row, "column": column + 2},
        "fix": {
            "applicability": applicability,
            "message": f"Fix {code}",
            "edits": [
                {
                    "content": "",
                    "location": {"row": row, "column": 1},
                    "end_location": {"row": row + 1, "column": 1},
                }
            ],
        },
    }


def _make_handler(
    actions: list[dict[str, Any]], violations: list[dict[str, Any]]
) -> RuffGetLintFixesHandler:
    return RuffGetLintFixesHandler(
        config=RuffGetLintFixesHandlerConfig(),
        logger=NoOpLogger(),
        file_editor=None,  # type: ignore[arg-type]  # _run_lsp_fixes takes content directly
        command_runner=_StubCommandRunner(json.dumps(violations)),  # type: ignore[arg-type]
        project_info_provider=_StubProjectInfoProvider(),  # type: ignore[arg-type]
        action_runner=None,  # type: ignore[arg-type]  # target version comes from the settings provider
        lsp_service=_StubLspService(actions),  # type: ignore[arg-type]
    )


async def test_an_unsafe_fix_offered_over_lsp_is_labelled_unsafe() -> None:
    # ruff serves F841's fix as an ordinary quickfix, structurally identical to F401's
    # safe one; only `ruff check` says one of them is unsafe
    handler = _make_handler(
        actions=[
            _code_action("Ruff (F401): Remove unused import: `os`", "F401", 0, 7, ""),
            _code_action(
                "Ruff (F841): Remove assignment to unused variable `x`",
                "F841",
                4,
                4,
                "",
            ),
        ],
        violations=[
            _violation("F401", row=1, column=8, applicability="safe"),
            _violation("F841", row=5, column=5, applicability="unsafe"),
        ],
    )

    fixes = await handler._run_lsp_fixes(
        _FILE_PATH, _CONTENT, GetLintFixesRunPayload(file_path=_FILE_URI), meta=None
    )

    assert {f.target_codes[0]: f.applicability for f in fixes} == {
        "F401": FixApplicability.SAFE,
        "F841": FixApplicability.UNSAFE,
    }


async def test_disabling_a_rule_for_a_line_is_never_applied_by_a_batch() -> None:
    # a noqa comment suppresses the diagnostic instead of fixing it, and ruff writes
    # none itself when fixing -- so it stays an offer, whatever include_unsafe says
    handler = _make_handler(
        actions=[
            _code_action(
                "Ruff (F401): Disable for this line", "F401", 0, 7, "  # noqa: F401\n"
            )
        ],
        violations=[_violation("F401", row=1, column=8, applicability="safe")],
    )

    fixes = await handler._run_lsp_fixes(
        _FILE_PATH, _CONTENT, GetLintFixesRunPayload(file_path=_FILE_URI), meta=None
    )

    assert [f.applicability for f in fixes] == [FixApplicability.DISPLAY_ONLY]


async def test_ruff_is_invoked_the_way_the_command_runner_accepts() -> None:
    # ICommandRunner.run runs one shell string; handed a list it raises "cmd must be a
    # string", which a stub runner accepting anything hides until a real run
    handler = _make_handler(
        actions=[_code_action("Ruff (F401): Remove unused import", "F401", 0, 7, "")],
        violations=[_violation("F401", row=1, column=8, applicability="safe")],
    )

    await handler._run_lsp_fixes(
        _FILE_PATH, _CONTENT, GetLintFixesRunPayload(file_path=_FILE_URI), meta=None
    )

    assert [isinstance(cmd, str) for cmd in handler.command_runner.commands] == [True]  # type: ignore[attr-defined]


async def test_a_fix_ruff_check_does_not_report_is_not_assumed_safe() -> None:
    # nothing states this fix's applicability, and "nothing says it is safe" must not
    # come out as "safe": that is the reading that applies fixes nobody vouched for
    handler = _make_handler(
        actions=[_code_action("Ruff (B006): Replace default", "B006", 2, 0, "()")],
        violations=[],
    )

    fixes = await handler._run_lsp_fixes(
        _FILE_PATH, _CONTENT, GetLintFixesRunPayload(file_path=_FILE_URI), meta=None
    )

    assert [f.applicability for f in fixes] == [FixApplicability.UNSAFE]
