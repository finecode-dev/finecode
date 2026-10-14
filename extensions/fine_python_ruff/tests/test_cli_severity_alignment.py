"""Tests that the ruff CLI path reports the severity the LSP path reports.

A finding rendered from the CLI path must fail a run exactly when the same finding
from the LSP path does. A severity derived from the rule-code prefix would make that
depend on the ``use_cli`` config alone, so a violation could gate one machine and not
the next.
"""

import pytest
from fine_lint.diagnostic_types import DiagnosticSeverity

from fine_python_ruff.lint_files_handler import map_ruff_violation_to_lint_message

_CODES = ["E501", "F401", "W291", "B904", "PLC0415", "UP006", "BLE001", "RUF012"]


def _violation(code: str) -> dict[str, object]:
    return {
        "code": code,
        "message": f"{code} message",
        "location": {"row": 1, "column": 1},
        "end_location": {"row": 1, "column": 2},
    }


@pytest.mark.parametrize("code", _CODES)
def test_every_code_maps_to_warning(code: str) -> None:
    diagnostic = map_ruff_violation_to_lint_message(_violation(code))

    assert diagnostic.severity is DiagnosticSeverity.WARNING
