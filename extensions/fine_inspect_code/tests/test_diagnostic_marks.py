"""Tests that baseline marks and the stale list reach the reader of a report.

A run that gates on a committed snapshot must show which findings it already
knows and which are new: without the mark, a developer cannot tell whether a
reported finding needs action or just a re-record, and a fixed finding that
lingers in the snapshot must fail visibly until the snapshot is updated.
"""

from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.code_action import RunReturnCode
from finecode_extension_api.resource_uri import ResourceUri

from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    DiagnosticFilesRunResult,
    DiagnosticSeverity,
    FindingOrigin,
    PartitionReceipt,
    Position,
    Range,
    ReceiptTarget,
    StaleBaselineEntry,
)

_RANGE = Range(start=Position(line=0, character=0), end=Position(line=0, character=1))
_FILE = ResourceUri("file:///ws/a.py")
_SNAPSHOT = ResourceUri("file:///ws/finecode-diagnostics-baseline.json")


def _diagnostic(
    severity: DiagnosticSeverity | None = DiagnosticSeverity.WARNING,
    baseline: BaselineStatus | None = None,
) -> Diagnostic:
    return Diagnostic(
        range=_RANGE,
        message="unused import",
        code="F401",
        source="ruff",
        severity=severity,
        origin=FindingOrigin(contributor="lint"),
        baseline=baseline,
    )


def _rendered(result: DiagnosticFilesRunResult) -> str:
    text = result.to_text()
    parts = text.text_parts if hasattr(text, "text_parts") else [str(text)]
    return "".join(
        part if isinstance(part, str) else str(part["text"]) for part in parts
    )


def _stale_entry() -> StaleBaselineEntry:
    return StaleBaselineEntry(
        contributor="lint",
        snapshot_file=_SNAPSHOT,
        file=_FILE,
        source="ruff",
        code="F401",
        message="unused import",
        stale_count=2,
    )


def _receipt() -> PartitionReceipt:
    return PartitionReceipt(
        contributor="lint",
        project=ResourceUri("file:///ws"),
        platform="linux",
        target=ReceiptTarget.PROJECT,
        unjudged=(),
    )


def test_update_merges_stale_and_receipts() -> None:
    first = DiagnosticFilesRunResult(messages={_FILE: [_diagnostic()]})
    second = DiagnosticFilesRunResult(
        messages={_FILE: [_diagnostic()]}, stale=[_stale_entry()], receipts=[_receipt()]
    )

    first.update(second)

    assert len(first.messages[_FILE]) == 2
    assert first.stale == [_stale_entry()]
    assert first.receipts == [_receipt()]


@dataclasses.dataclass
class _Foreign(code_action.RunActionResult):
    def update(self, other: code_action.RunActionResult) -> None:
        raise AssertionError(f"must not be called: {other!r}")

    def to_text(self) -> str:
        return ""


def test_update_ignores_a_foreign_result_type() -> None:
    result = DiagnosticFilesRunResult(
        messages={_FILE: [_diagnostic()]}, stale=[_stale_entry()], receipts=[_receipt()]
    )

    result.update(_Foreign())

    assert len(result.messages[_FILE]) == 1
    assert result.stale == [_stale_entry()]
    assert result.receipts == [_receipt()]


def test_known_warning_passes_while_new_and_outside_fail() -> None:
    assert (
        DiagnosticFilesRunResult(
            messages={_FILE: [_diagnostic(baseline=BaselineStatus.KNOWN)]}
        ).return_code
        is RunReturnCode.SUCCESS
    )
    assert (
        DiagnosticFilesRunResult(
            messages={_FILE: [_diagnostic(baseline=BaselineStatus.NEW)]}
        ).return_code
        is RunReturnCode.ERROR
    )
    assert (
        DiagnosticFilesRunResult(
            messages={_FILE: [_diagnostic(baseline=BaselineStatus.OUTSIDE)]}
        ).return_code
        is RunReturnCode.ERROR
    )


def test_new_info_passes() -> None:
    assert (
        DiagnosticFilesRunResult(
            messages={
                _FILE: [
                    _diagnostic(
                        severity=DiagnosticSeverity.INFO,
                        baseline=BaselineStatus.NEW,
                    )
                ]
            }
        ).return_code
        is RunReturnCode.SUCCESS
    )


def test_stale_alone_fails_and_receipts_alone_pass() -> None:
    assert (
        DiagnosticFilesRunResult(messages={}, stale=[_stale_entry()]).return_code
        is RunReturnCode.ERROR
    )
    assert (
        DiagnosticFilesRunResult(messages={}, receipts=[_receipt()]).return_code
        is RunReturnCode.SUCCESS
    )


def test_to_text_marks_each_finding() -> None:
    result = DiagnosticFilesRunResult(
        messages={
            _FILE: [
                _diagnostic(baseline=BaselineStatus.NEW),
                _diagnostic(baseline=BaselineStatus.KNOWN),
                _diagnostic(baseline=BaselineStatus.OUTSIDE),
            ]
        }
    )

    rendered = _rendered(result)

    assert "[new]" in rendered
    assert "[known]" in rendered
    assert "[outside]" in rendered


def test_to_text_lists_stale_entries() -> None:
    result = DiagnosticFilesRunResult(messages={}, stale=[_stale_entry()])

    rendered = _rendered(result)

    assert "stale baseline entries" in rendered
    assert "(x2)" in rendered
    assert "file:///ws/finecode-diagnostics-baseline.json" in rendered


def test_unmarked_output_is_unchanged() -> None:
    result = DiagnosticFilesRunResult(
        messages={
            _FILE: [
                Diagnostic(
                    range=_RANGE, message="msg", code="E", source="src",
                )
            ]
        }
    )

    assert _rendered(result) == "file:///ws/a.py:1:1: E msg (src)\n"
