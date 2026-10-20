"""Tests that snapshot matching marks findings new or known and reports staleness.

Matching decides what the gate does with every finding: a recorded finding
must pass as known however far it moved, one more copy than recorded must
fail as new, and a fixed finding must fail as stale until re-recorded. A
finding the snapshot cannot judge — another project's file, an unowned
anchor, an info-level note — must never be mistaken for known.
"""

from __future__ import annotations

import pathlib
import types

import pytest
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri

from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    DiagnosticSeverity,
    PartitionReceipt,
    Position,
    Range,
    ReceiptTarget,
)
from fine_inspect_code.ratchet_matching import (
    FindingIdentity,
    RatchetOutcome,
    RatchetScope,
    RatchetTarget,
    apply_snapshot,
    identity_of,
)
from fine_inspect_code.ratchet_snapshot import PartitionSnapshot, Snapshot

_CONTRIBUTOR = "lint"


class _Ws(types.SimpleNamespace):
    root: pathlib.Path
    proj: pathlib.Path
    nested: pathlib.Path
    other: pathlib.Path
    projects: list[pathlib.Path]
    snapshot_path: pathlib.Path


@pytest.fixture
def ws(tmp_path: pathlib.Path) -> _Ws:
    proj = tmp_path / "proj"
    nested = proj / "nested"
    other = tmp_path / "other"
    nested.mkdir(parents=True)
    other.mkdir()
    return _Ws(
        root=tmp_path,
        proj=proj,
        nested=nested,
        other=other,
        projects=[tmp_path, proj, nested, other],
        snapshot_path=proj / "finecode-diagnostics-baseline.json",
    )


def _uri(path: pathlib.Path) -> ResourceUri:
    return path_to_resource_uri(path)


def _diagnostic(
    message: str = "unused import",
    code: str | None = "F401",
    code_description: str | None = None,
    source: str | None = "ruff",
    severity: DiagnosticSeverity | None = DiagnosticSeverity.WARNING,
    line: int = 0,
) -> Diagnostic:
    return Diagnostic(
        range=Range(
            start=Position(line=line, character=0),
            end=Position(line=line, character=1),
        ),
        message=message,
        code=code,
        code_description=code_description,
        source=source,
        severity=severity,
    )


def _snap(
    contributor: str,
    platforms: list[str],
    counts: dict[tuple[str, str, str, str], dict[str, int]],
) -> Snapshot:
    by_platform: dict[str, dict[FindingIdentity, int]] = {
        platform: {} for platform in platforms
    }
    for (path, source, code, message), per_platform in counts.items():
        for platform, count in per_platform.items():
            by_platform[platform][
                FindingIdentity(
                    contributor=contributor,
                    path=path,
                    source=source,
                    code=code,
                    message=message,
                )
            ] = count
    return Snapshot(
        contributors={
            contributor: PartitionSnapshot(
                platforms=tuple(platforms), counts_by_platform=by_platform
            )
        }
    )


def _run(
    ws: _Ws,
    messages: dict[ResourceUri, list[Diagnostic]],
    *,
    contributor: str = _CONTRIBUTOR,
    snapshot: Snapshot | None = None,
    platform: str = "linux",
    scope: RatchetScope | None = None,
    unhandled: list[ItemCoverage] | None = None,
    checked: pathlib.Path | None = None,
) -> RatchetOutcome:
    return apply_snapshot(
        contributor=contributor,
        checked_project=checked if checked is not None else ws.proj,
        projects=ws.projects,
        snapshot_path=ws.snapshot_path,
        snapshot=snapshot,
        platform=platform,
        messages=messages,
        scope=scope if scope is not None else RatchetScope(RatchetTarget.PROJECT),
        unhandled=unhandled if unhandled is not None else [],
    )


def _marks(outcome: RatchetOutcome) -> list[BaselineStatus | None]:
    return [d.baseline for ds in outcome.messages.values() for d in ds]


def test_recorded_findings_are_known_and_nothing_is_stale(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outcome = _run(ws, {uri: [_diagnostic()]}, snapshot=snapshot)

    assert _marks(outcome) == [BaselineStatus.KNOWN]
    assert outcome.stale == []
    assert len(outcome.receipts) == 1


def test_duplicate_identical_finding_is_new_in_source_order(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outcome = _run(
        ws,
        {uri: [_diagnostic(line=5), _diagnostic(line=1)]},
        snapshot=snapshot,
    )

    assert [d.baseline for d in outcome.messages[uri]] == [
        BaselineStatus.NEW,
        BaselineStatus.KNOWN,
    ]


def test_fixed_finding_is_stale(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 2}}
    )

    outcome = _run(ws, {uri: [_diagnostic()]}, snapshot=snapshot)

    assert len(outcome.stale) == 1
    entry = outcome.stale[0]
    assert entry.contributor == _CONTRIBUTOR
    assert entry.snapshot_file == _uri(ws.snapshot_path)
    assert entry.file == uri
    assert (entry.source, entry.code, entry.message) == (
        "ruff",
        "F401",
        "unused import",
    )
    assert entry.stale_count == 1


def test_line_shift_does_not_make_findings_new(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outcome = _run(ws, {uri: [_diagnostic(line=50)]}, snapshot=snapshot)

    assert _marks(outcome) == [BaselineStatus.KNOWN]
    assert outcome.stale == []


def test_no_snapshot_leaves_findings_unmarked_with_receipt(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")

    outcome = _run(ws, {uri: [_diagnostic()]})

    assert _marks(outcome) == [None]
    origin = outcome.messages[uri][0].origin
    assert origin is not None
    assert origin.contributor == _CONTRIBUTOR
    assert outcome.stale == []
    assert len(outcome.receipts) == 1


def test_files_scope_limits_staleness_to_requested_files(ws: _Ws) -> None:
    uri_a = _uri(ws.proj / "a.py")
    uri_b = _uri(ws.proj / "b.py")
    snapshot = _snap(
        _CONTRIBUTOR,
        ["linux"],
        {
            ("a.py", "ruff", "F401", "unused import"): {"linux": 1},
            ("b.py", "ruff", "F401", "unused import"): {"linux": 1},
        },
    )

    outcome = _run(
        ws,
        {uri_a: [_diagnostic(message="a new one")]},
        snapshot=snapshot,
        scope=RatchetScope.for_payload("files", [uri_a]),
    )

    assert outcome.messages[uri_a][0].baseline is BaselineStatus.NEW
    assert [entry.file for entry in outcome.stale] == [uri_a]
    assert uri_b not in [entry.file for entry in outcome.stale]


def test_mark_only_marks_but_judges_nothing(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outcome = _run(ws, {uri: [_diagnostic()]}, snapshot=snapshot, scope=RatchetScope.mark_only())

    assert _marks(outcome) == [BaselineStatus.KNOWN]
    assert outcome.stale == []
    assert outcome.receipts == []


def test_unknown_platform_marks_everything_new(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outcome = _run(ws, {uri: [_diagnostic()]}, snapshot=snapshot, platform="plan9")

    assert _marks(outcome) == [BaselineStatus.NEW]
    assert outcome.stale == []


def test_info_and_hint_are_stamped_but_never_marked(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 5}}
    )

    outcome = _run(
        ws,
        {
            uri: [
                _diagnostic(severity=DiagnosticSeverity.INFO),
                _diagnostic(severity=DiagnosticSeverity.HINT),
            ]
        },
        snapshot=snapshot,
    )

    assert _marks(outcome) == [None, None]
    assert all(d.origin is not None for d in outcome.messages[uri])


def test_source_casing_is_folded(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outcome = _run(ws, {uri: [_diagnostic(source="Ruff")]}, snapshot=snapshot)

    assert _marks(outcome) == [BaselineStatus.KNOWN]


def test_pyrefly_message_uses_first_line(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    full = "unannotated-return\n  12 | def f(x):\n            ^^^"
    snapshot = _snap(
        "type_check",
        ["linux"],
        {("a.py", "pyrefly", "unannotated-return", "unannotated-return"): {"linux": 1}},
    )

    outcome = _run(
        ws,
        {
            uri: [
                _diagnostic(
                    message=full, code="unannotated-return", source="Pyrefly"
                )
            ]
        },
        contributor="type_check",
        snapshot=snapshot,
    )

    assert _marks(outcome) == [BaselineStatus.KNOWN]


def test_pyrefly_cli_code_matches_lsp_code() -> None:
    cli_shaped = _diagnostic(
        message="x", code="-2", code_description="unannotated-return", source="pyrefly"
    )
    lsp_shaped = _diagnostic(
        message="x", code="unannotated-return", source="Pyrefly"
    )

    assert identity_of(cli_shaped, "type_check", "a.py") == identity_of(
        lsp_shaped, "type_check", "a.py"
    )


def test_f811_line_reference_is_stripped() -> None:
    diagnostic = _diagnostic(message="redefinition of unused `x` from line 3", code="F811")

    assert (
        identity_of(diagnostic, _CONTRIBUTOR, "a.py").message
        == "redefinition of unused `x`"
    )


def test_outside_finding_fails_and_is_never_matched(ws: _Ws) -> None:
    uri = _uri(ws.other / "a.py")
    snapshot = Snapshot(
        contributors={
            _CONTRIBUTOR: PartitionSnapshot(
                platforms=("linux",), counts_by_platform={"linux": {}}
            )
        }
    )

    outcome = _run(ws, {uri: [_diagnostic()]}, snapshot=snapshot)

    assert _marks(outcome) == [BaselineStatus.OUTSIDE]
    assert outcome.stale == []
    assert [receipt.project for receipt in outcome.receipts] == [_uri(ws.proj)]


def test_nested_project_owns_its_files(ws: _Ws) -> None:
    uri = _uri(ws.nested / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outer = _run(ws, {uri: [_diagnostic()]}, snapshot=snapshot)
    assert _marks(outer) == [BaselineStatus.OUTSIDE]

    inner = _run(
        ws,
        {uri: [_diagnostic()]},
        snapshot=snapshot,
        checked=ws.nested,
    )
    assert [d.baseline for d in inner.messages[uri]] == [BaselineStatus.KNOWN]


def test_whole_call_unhandled_yields_no_receipt_or_stale(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 1}}
    )

    outcome = _run(
        ws,
        {uri: [_diagnostic()]},
        snapshot=snapshot,
        unhandled=[
            ItemCoverage(status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, item=None)
        ],
    )

    assert outcome.stale == []
    assert outcome.receipts == []


def test_per_item_unhandled_keeps_only_its_path_unjudged(ws: _Ws) -> None:
    uri_toml = _uri(ws.proj / "pyproject.toml")
    uri_py = _uri(ws.proj / "a.py")
    lint_snapshot = _snap(
        "lint",
        ["linux"],
        {
            ("pyproject.toml", "ruff", "F401", "unused import"): {"linux": 1},
            ("a.py", "ruff", "F401", "unused import"): {"linux": 1},
        },
    )
    lint_outcome = _run(
        ws,
        {},
        contributor="lint",
        snapshot=lint_snapshot,
        unhandled=[
            ItemCoverage(
                status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, item=uri_toml
            )
        ],
    )
    assert [entry.file for entry in lint_outcome.stale] == [uri_py]
    assert lint_outcome.receipts[0].unjudged == (uri_toml,)

    toolchains_snapshot = _snap(
        "check_toolchains",
        ["linux"],
        {("pyproject.toml", "toml", "drift", "axis drifted"): {"linux": 1}},
    )
    toolchains_outcome = _run(
        ws,
        {},
        contributor="check_toolchains",
        snapshot=toolchains_snapshot,
    )
    assert [entry.file for entry in toolchains_outcome.stale] == [uri_toml]


def test_ownerless_findings_are_never_marked(ws: _Ws) -> None:
    snapshot = _snap(
        _CONTRIBUTOR, ["linux"], {("a.py", "ruff", "F401", "unused import"): {"linux": 9}}
    )

    outcome = _run(
        ws,
        {
            ResourceUri(""): [_diagnostic()],
            ResourceUri("https://example.invalid/x"): [_diagnostic()],
        },
        snapshot=snapshot,
    )

    assert _marks(outcome) == [None, None]


def test_empty_lists_are_kept(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")

    outcome = _run(ws, {uri: []})

    assert outcome.messages == {uri: []}


def test_scope_constructors() -> None:
    uri = ResourceUri("file:///ws/a.py")

    scope = RatchetScope.for_payload("files", [uri])

    assert scope.target is RatchetTarget.FILES
    assert scope.files == (uri,)
    assert RatchetScope.mark_only().target is RatchetTarget.MARK_ONLY
    assert RatchetScope.mark_only().files == ()


def test_receipt_target_has_no_mark_only() -> None:
    with pytest.raises(ValueError):
        _ = ReceiptTarget("mark_only")


def test_receipt_carries_contributor_own_unhandled(ws: _Ws) -> None:
    uri = _uri(ws.proj / "a.py")

    outcome = _run(
        ws,
        {uri: [_diagnostic()]},
        unhandled=[
            ItemCoverage(status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, item=uri)
        ],
    )

    receipt = outcome.receipts[0]
    assert isinstance(receipt, PartitionReceipt)
    assert receipt.target is ReceiptTarget.PROJECT
    assert receipt.platform == "linux"
    assert receipt.unjudged == (uri,)
