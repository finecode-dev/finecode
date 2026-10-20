"""Tests the mypy severity lookup for configured but unknown severities.

A project can configure a severity name mypy does not define. The finding must still
be reported and must fail a run as an ERROR: silently dropping it to INFO would let a
misconfiguration hide every finding of that class from the gate.
"""

from fine_type_check.diagnostic_types import DiagnosticSeverity

from fine_python_mypy.output_parser import _get_severity


def test_configured_severity_is_used() -> None:
    assert (
        _get_severity("misc", "error", {"misc": "warning"})
        is DiagnosticSeverity.WARNING
    )


def test_code_type_is_the_fallback_key() -> None:
    assert _get_severity("", "note", {"note": "info"}) is DiagnosticSeverity.INFO


def test_a_name_not_in_diagnostic_severity_is_an_error() -> None:
    assert (
        _get_severity("misc", "error", {"misc": "remark"})
        is DiagnosticSeverity.ERROR
    )


def test_no_entry_falls_back_to_error() -> None:
    assert (
        _get_severity("misc", "error", {}) is DiagnosticSeverity.ERROR
    )
