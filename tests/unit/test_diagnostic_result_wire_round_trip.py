"""Tests that marks, origins and receipts survive the runner round trip.

Marks ride on the shared result type across the ER boundary, the precommit
merge and the ``--results-file`` JSON: if any hop dropped them, a known
finding would fail the gate again on the next run, or a recording would lose
the receipt it needs to replace exactly one partition.
"""

from __future__ import annotations

import cattrs
from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    DiagnosticSeverity,
    FindingOrigin,
    PartitionReceipt,
    Position,
    Range,
    ReceiptTarget,
    StaleBaselineEntry,
)
from fine_inspect_code.inspect_code_action import InspectCodeRunResult
from finecode_extension_api.resource_uri import ResourceUri
from finecode_extension_runner._converter import converter as er_converter

_RESULT = InspectCodeRunResult(
    messages={
        ResourceUri("file:///ws/a.py"): [
            Diagnostic(
                range=Range(
                    start=Position(line=0, character=0),
                    end=Position(line=0, character=1),
                ),
                message="unused import",
                code="F401",
                source="ruff",
                severity=DiagnosticSeverity.WARNING,
                origin=FindingOrigin(contributor="lint"),
                baseline=BaselineStatus.KNOWN,
            )
        ]
    },
    stale=[
        StaleBaselineEntry(
            contributor="lint",
            snapshot_file=ResourceUri("file:///ws/finecode-diagnostics-baseline.json"),
            file=ResourceUri("file:///ws/a.py"),
            source="ruff",
            code="F401",
            message="unused import",
            stale_count=2,
        )
    ],
    receipts=[
        PartitionReceipt(
            contributor="lint",
            project=ResourceUri("file:///ws"),
            platform="linux",
            target=ReceiptTarget.PROJECT,
            unjudged=(),
        )
    ],
)


def _assert_wire_shape(unstructured: object) -> None:
    assert isinstance(unstructured, dict)
    finding = unstructured["messages"]["file:///ws/a.py"][0]
    assert finding["baseline"] == "known"
    assert finding["origin"] == {"contributor": "lint"}
    assert unstructured["receipts"] == [
        {
            "contributor": "lint",
            "project": "file:///ws",
            "platform": "linux",
            "target": "project",
            "unjudged": [],
        }
    ]


def test_plain_converter_round_trip() -> None:
    converter = cattrs.Converter()

    unstructured = converter.unstructure(_RESULT)
    _assert_wire_shape(unstructured)

    assert converter.structure(unstructured, InspectCodeRunResult) == _RESULT


def test_runner_converter_round_trip() -> None:
    unstructured = er_converter.unstructure(_RESULT)
    _assert_wire_shape(unstructured)

    assert er_converter.structure(unstructured, InspectCodeRunResult) == _RESULT
