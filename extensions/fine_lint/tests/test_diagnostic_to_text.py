"""Tests for how a diagnostic's fixability reaches the person reading the report.

Knowing which entries a tool can fix on its own is what turns a wall of diagnostics into
a short list worth reading: the rest are one ``apply_lint_fixes`` away.
"""

from __future__ import annotations

from fine_lint.diagnostic_types import (
    Diagnostic,
    DiagnosticFilesRunResult,
    Position,
    Range,
)

_RANGE = Range(start=Position(line=0, character=0), end=Position(line=0, character=1))


def _rendered(diagnostic: Diagnostic) -> str:
    result = DiagnosticFilesRunResult(messages={"file:///ws/a.py": [diagnostic]})
    text = result.to_text()
    return "".join(
        part if isinstance(part, str) else str(part["text"]) for part in text.text_parts
    )


def test_a_fixable_diagnostic_says_so() -> None:
    rendered = _rendered(
        Diagnostic(range=_RANGE, message="unused import", code="F401", fixable=True)
    )

    assert "[fixable]" in rendered


def test_a_diagnostic_with_no_fix_is_not_marked() -> None:
    rendered = _rendered(
        Diagnostic(range=_RANGE, message="undefined name", code="F821", fixable=False)
    )

    assert "[fixable]" not in rendered


def test_a_tool_that_reports_no_fix_information_marks_nothing() -> None:
    # the default: claiming anything about a tool that does not report fixability would
    # be an invention either way
    rendered = _rendered(
        Diagnostic(
            range=_RANGE, message="needs a type annotation", code="var-annotated"
        )
    )

    assert "[fixable]" not in rendered
