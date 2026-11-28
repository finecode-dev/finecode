"""Tests that the editor distinguishes known findings from new ones.

A known finding is advice the snapshot already records: it must read as
informational with a baseline note, so it does not demand action. A new or
unmarked finding keeps the severity its tool reported, so nothing about the
gate is hidden by the rendering.
"""

from __future__ import annotations

from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)
from lsprotocol import types

from finecode.lsp_server.endpoints.diagnostics import map_lint_message_to_diagnostic

_RANGE = Range(start=Position(line=0, character=0), end=Position(line=0, character=1))


def _diagnostic(
    severity: DiagnosticSeverity | None,
    baseline: BaselineStatus | None,
) -> Diagnostic:
    return Diagnostic(
        range=_RANGE, message="unused import", severity=severity, baseline=baseline
    )


def test_known_finding_renders_as_information_with_baseline_note() -> None:
    rendered = map_lint_message_to_diagnostic(
        _diagnostic(DiagnosticSeverity.WARNING, BaselineStatus.KNOWN)
    )

    assert rendered.severity is types.DiagnosticSeverity.Information
    assert rendered.message == "unused import (baseline)"


def test_new_finding_keeps_tool_severity() -> None:
    rendered = map_lint_message_to_diagnostic(
        _diagnostic(DiagnosticSeverity.WARNING, BaselineStatus.NEW)
    )

    assert rendered.severity is types.DiagnosticSeverity.Warning
    assert rendered.message == "unused import"


def test_outside_finding_keeps_tool_severity() -> None:
    rendered = map_lint_message_to_diagnostic(
        _diagnostic(DiagnosticSeverity.ERROR, BaselineStatus.OUTSIDE)
    )

    assert rendered.severity is types.DiagnosticSeverity.Error
    assert rendered.message == "unused import"


def test_unmarked_finding_is_unchanged() -> None:
    rendered = map_lint_message_to_diagnostic(
        _diagnostic(DiagnosticSeverity.INFO, None)
    )

    assert rendered.severity is types.DiagnosticSeverity.Information
    assert rendered.message == "unused import"
