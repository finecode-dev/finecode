"""Tests that only failing severities fail a diagnostics run.

A run gating on every diagnostic, including advice-level INFO and HINT entries,
keeps the gate red on code that needs no action. The gate must fail exactly on
findings that need attention, so advisory findings can land without blocking.
"""

from finecode_extension_api.code_action import RunReturnCode
from finecode_extension_api.resource_uri import ResourceUri

from fine_inspect_code.diagnostic_types import (
    Diagnostic,
    DiagnosticFilesRunResult,
    DiagnosticSeverity,
    Position,
    Range,
    fails_run,
)

_RANGE = Range(start=Position(line=0, character=0), end=Position(line=0, character=1))


def _diagnostic(severity: DiagnosticSeverity | None) -> Diagnostic:
    return Diagnostic(range=_RANGE, message="message", severity=severity)


def _result(*severities: DiagnosticSeverity | None) -> DiagnosticFilesRunResult:
    return DiagnosticFilesRunResult(
        messages={
            ResourceUri("file:///ws/a.py"): [
                _diagnostic(severity) for severity in severities
            ]
        }
    )


def test_error_fails_run() -> None:
    assert fails_run(_diagnostic(DiagnosticSeverity.ERROR)) is True
    assert _result(DiagnosticSeverity.ERROR).return_code is RunReturnCode.ERROR


def test_warning_fails_run() -> None:
    assert fails_run(_diagnostic(DiagnosticSeverity.WARNING)) is True
    assert _result(DiagnosticSeverity.WARNING).return_code is RunReturnCode.ERROR


def test_missing_severity_fails_run() -> None:
    assert fails_run(_diagnostic(None)) is True
    assert _result(None).return_code is RunReturnCode.ERROR


def test_info_does_not_fail_run() -> None:
    assert fails_run(_diagnostic(DiagnosticSeverity.INFO)) is False
    assert _result(DiagnosticSeverity.INFO).return_code is RunReturnCode.SUCCESS


def test_hint_does_not_fail_run() -> None:
    assert fails_run(_diagnostic(DiagnosticSeverity.HINT)) is False
    assert _result(DiagnosticSeverity.HINT).return_code is RunReturnCode.SUCCESS


def test_empty_result_passes() -> None:
    assert DiagnosticFilesRunResult(messages={}).return_code is RunReturnCode.SUCCESS
    assert (
        DiagnosticFilesRunResult(messages={ResourceUri("file:///ws/a.py"): []}).return_code
        is RunReturnCode.SUCCESS
    )


def test_mixed_info_and_hint_passes() -> None:
    assert (
        _result(DiagnosticSeverity.INFO, DiagnosticSeverity.HINT).return_code
        is RunReturnCode.SUCCESS
    )


def test_mixed_warning_and_info_fails() -> None:
    assert (
        _result(DiagnosticSeverity.WARNING, DiagnosticSeverity.INFO).return_code
        is RunReturnCode.ERROR
    )
