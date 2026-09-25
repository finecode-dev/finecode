"""Tests for reporting which diagnostics ruff can fix automatically.

Reading a lint report, the first question is which entries need thought and which are one
command away.  Ruff knows the answer for every violation it reports; if it is dropped
here, everyone reading the report has to guess or re-run the tool with ``--fix`` to find
out.

``None`` is deliberately distinct from ``False``: a tool that says nothing about
fixability must not be reported as having no fixes.
"""

from __future__ import annotations

import pathlib
import typing

from fine_python_ruff.lint_files_handler import map_ruff_violation_to_lint_message
from fine_python_ruff.ruff_lsp_service import RuffLspService

_VIOLATION = {
    "code": "F401",
    "message": "`os` imported but unused",
    "location": {"row": 1, "column": 8},
    "end_location": {"row": 1, "column": 10},
    "url": "https://docs.astral.sh/ruff/rules/unused-import",
}


def test_a_violation_ruff_can_fix_is_reported_as_fixable() -> None:
    violation = {**_VIOLATION, "fix": {"applicability": "safe", "edits": []}}

    assert map_ruff_violation_to_lint_message(violation).fixable is True


def test_a_violation_ruff_cannot_fix_is_reported_as_not_fixable() -> None:
    # not None: ruff reports its fix inline with every violation, so silence here is an
    # answer -- there is no fix
    assert (
        map_ruff_violation_to_lint_message({**_VIOLATION, "fix": None}).fixable is False
    )


class _StubLspService:
    def __init__(self, diagnostics: list[dict[str, typing.Any]]) -> None:
        self._diagnostics = diagnostics

    async def check_file(
        self, file_path: pathlib.Path, timeout: float
    ) -> list[dict[str, typing.Any]]:
        return self._diagnostics


def _service(diagnostics: list[dict[str, typing.Any]]) -> RuffLspService:
    service = RuffLspService(
        lsp_client=typing.cast(typing.Any, object()),
        file_editor=typing.cast(typing.Any, object()),
        logger=typing.cast(typing.Any, object()),
    )
    service._lsp_service = typing.cast(typing.Any, _StubLspService(diagnostics))
    return service


def _lsp_diagnostic(data: typing.Any) -> dict[str, typing.Any]:
    return {
        "code": "F401",
        "message": "`os` imported but unused",
        "range": {
            "start": {"line": 0, "character": 7},
            "end": {"line": 0, "character": 9},
        },
        "severity": 2,
        "data": data,
    }


async def test_fixability_survives_the_lsp_path() -> None:
    # LSP diagnostics have no field for this, so it comes out of ruff's own `data`; the
    # LSP path is the default one, so losing it there loses it for almost every run
    fix = {"edits": [{"newText": "", "range": {}}]}
    no_fix = {"edits": []}
    service = _service([_lsp_diagnostic(fix), _lsp_diagnostic(no_fix)])

    diagnostics = await service.check_file(pathlib.Path("/ws/a.py"))

    assert [d.fixable for d in diagnostics] == [True, False]


async def test_a_server_that_reports_no_fix_information_leaves_fixability_unknown() -> (
    None
):
    # ruff always attaches `data`; a diagnostic without it came from somewhere that does
    # not report fixes, and claiming "not fixable" would be an invention
    service = _service([_lsp_diagnostic(None)])

    diagnostics = await service.check_file(pathlib.Path("/ws/a.py"))

    assert diagnostics[0].fixable is None
