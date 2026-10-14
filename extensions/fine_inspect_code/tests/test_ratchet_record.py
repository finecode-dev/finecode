"""Tests that recording replaces exactly the receipted partitions.

A recording turns one run's judged findings into snapshot counts: only
failing findings the contributor itself emitted and located in the receipt's
project may land in its partition, inputs the contributor could not judge
keep their recorded counts, and anything else — another contributor's
partition, another platform, a file-scoped receipt — refuses or stays
untouched rather than being guessed.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri

from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    DiagnosticSeverity,
    FindingOrigin,
    PartitionReceipt,
    Position,
    Range,
    ReceiptTarget,
)
from fine_inspect_code.inspect_code_action import InspectCodeRunResult
from fine_inspect_code.ratchet_matching import FindingIdentity
from fine_inspect_code.ratchet_record import (
    PartitionKey,
    RecordRefusedError,
    apply_plan,
    plan_from_results,
)
from fine_inspect_code.ratchet_snapshot import (
    PartitionSnapshot,
    Snapshot,
    parse_snapshot,
    render_snapshot,
)


def _uri(path: pathlib.Path) -> ResourceUri:
    return path_to_resource_uri(path)


def _diagnostic(
    contributor: str | None,
    message: str = "unused import",
    severity: DiagnosticSeverity | None = DiagnosticSeverity.WARNING,
    baseline: BaselineStatus | None = None,
) -> Diagnostic:
    return Diagnostic(
        range=Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=1),
        ),
        message=message,
        code="F401",
        source="ruff",
        severity=severity,
        origin=FindingOrigin(contributor=contributor) if contributor else None,
        baseline=baseline,
    )


def _receipt(
    contributor: str,
    project: pathlib.Path,
    platform: str = "linux",
    target: ReceiptTarget = ReceiptTarget.PROJECT,
    unjudged: tuple[ResourceUri, ...] = (),
) -> PartitionReceipt:
    return PartitionReceipt(
        contributor=contributor,
        project=_uri(project),
        platform=platform,
        target=target,
        unjudged=unjudged,
    )


def _result(
    messages: dict[ResourceUri, list[Diagnostic]],
    receipts: list[PartitionReceipt] | None = None,
) -> InspectCodeRunResult:
    return InspectCodeRunResult(
        messages=messages, receipts=receipts if receipts is not None else []
    )


def _identity(
    contributor: str, path: str = "a.py", message: str = "unused import"
) -> FindingIdentity:
    return FindingIdentity(
        contributor=contributor,
        path=path,
        source="ruff",
        code="F401",
        message=message,
    )


def test_found_counts_record_failing_findings(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    uri = _uri(project / "a.py")

    plan = plan_from_results(
        [_result({uri: [_diagnostic("lint"), _diagnostic("lint")]}, [_receipt("lint", project)])],
        [project],
        lambda _project: None,
    )

    assert plan.platform == "linux"
    assert plan.counts == {
        PartitionKey(project=project, contributor="lint"): {_identity("lint"): 2}
    }


def test_unstamped_info_outside_and_ownerless_are_never_recorded(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    other = tmp_path / "other"
    uri = _uri(project / "a.py")
    outside_uri = _uri(other / "a.py")

    plan = plan_from_results(
        [
            _result(
                {
                    uri: [
                        _diagnostic(None),
                        _diagnostic("lint", severity=DiagnosticSeverity.INFO),
                    ],
                    outside_uri: [_diagnostic("lint", baseline=BaselineStatus.OUTSIDE)],
                    ResourceUri(""): [_diagnostic("lint")],
                },
                [_receipt("lint", project)],
            )
        ],
        [project, other],
        lambda _project: None,
    )

    assert plan.counts == {PartitionKey(project=project, contributor="lint"): {}}


def test_nested_project_records_its_own_relative_path(
    tmp_path: pathlib.Path,
) -> None:
    nested = tmp_path / "proj" / "nested"
    uri = _uri(nested / "a.py")

    plan = plan_from_results(
        [_result({uri: [_diagnostic("lint")]}, [_receipt("lint", nested)])],
        [tmp_path, tmp_path / "proj", nested],
        lambda _project: None,
    )

    assert plan.counts == {
        PartitionKey(project=nested, contributor="lint"): {_identity("lint"): 1}
    }


def test_unjudged_keeps_existing_without_double_count(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    uri = _uri(project / "pyproject.toml")
    existing = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {_identity("lint", "pyproject.toml"): 5}},
            )
        }
    )

    plan = plan_from_results(
        [
            _result(
                {
                    uri: [
                        _diagnostic("lint", message="x"),
                        _diagnostic("lint", message="x"),
                    ]
                },
                [_receipt("lint", project, unjudged=(uri,))],
            )
        ],
        [project],
        lambda _project: existing,
    )

    assert plan.counts[PartitionKey(project=project, contributor="lint")] == {
        _identity("lint", "pyproject.toml"): 5
    }


def test_unjudged_existing_identity_keeps_its_count(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    uri = _uri(project / "a.py")
    identity = _identity("lint")
    existing = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {identity: 5}},
            )
        }
    )

    plan = plan_from_results(
        [
            _result(
                {uri: [_diagnostic("lint"), _diagnostic("lint")]},
                [_receipt("lint", project, unjudged=(uri,))],
            )
        ],
        [project],
        lambda _project: existing,
    )

    assert plan.counts[PartitionKey(project=project, contributor="lint")] == {
        identity: 5
    }


def test_lint_unhandled_does_not_keep_check_toolchains_entry(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    uri = _uri(project / "pyproject.toml")
    lint_identity = _identity("lint", "pyproject.toml")
    toolchains_identity = FindingIdentity(
        contributor="check_toolchains",
        path="pyproject.toml",
        source="toml",
        code="drift",
        message="axis drifted",
    )
    existing = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {lint_identity: 1}},
            ),
            "check_toolchains": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {toolchains_identity: 1}},
            ),
        }
    )
    before = render_snapshot(existing)

    plan = plan_from_results(
        [
            _result(
                {},
                [
                    _receipt("lint", project, unjudged=(uri,)),
                    _receipt("check_toolchains", project),
                ],
            )
        ],
        [project],
        lambda _project: existing,
    )

    assert plan.counts[PartitionKey(project=project, contributor="lint")] == {
        lint_identity: 1
    }
    assert plan.counts[PartitionKey(project=project, contributor="check_toolchains")] == {}
    after = render_snapshot(apply_plan(plan, {project: existing})[project])
    assert json.loads(after)["contributors"]["lint"] == json.loads(before)[
        "contributors"
    ]["lint"]
    assert json.loads(after)["contributors"]["check_toolchains"]["paths"] == {}


def test_partitions_without_receipt_stay_untouched(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    uri = _uri(project / "a.py")
    existing = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {_identity("lint"): 1}},
            ),
            "type_check": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={
                    "linux": {
                        FindingIdentity(
                            contributor="type_check",
                            path="a.py",
                            source="pyrefly",
                            code="x",
                            message="m",
                        ): 2
                    }
                },
            ),
        }
    )

    plan = plan_from_results(
        [_result({uri: [_diagnostic("lint")]}, [_receipt("lint", project)])],
        [project],
        lambda _project: existing,
    )
    updated = apply_plan(plan, {project: existing})

    assert updated[project].contributors["type_check"] is existing.contributors[
        "type_check"
    ]


def test_files_receipt_refuses(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"

    with pytest.raises(RecordRefusedError):
        plan_from_results(
            [
                _result(
                    {},
                    [_receipt("lint", project, target=ReceiptTarget.FILES)],
                )
            ],
            [project],
            lambda _project: None,
        )


def test_two_platforms_refuse(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"

    with pytest.raises(RecordRefusedError):
        plan_from_results(
            [
                _result(
                    {},
                    [
                        _receipt("lint", project, platform="linux"),
                        _receipt("lint", project, platform="darwin"),
                    ],
                )
            ],
            [project],
            lambda _project: None,
        )


def test_no_receipts_refuse(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"

    with pytest.raises(RecordRefusedError):
        plan_from_results(
            [_result({_uri(project / "a.py"): [_diagnostic("lint")]})],
            [project],
            lambda _project: None,
        )


def test_record_twice_yields_identical_text(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    uri = _uri(project / "a.py")

    def _round(existing: Snapshot | None) -> str:
        plan = plan_from_results(
            [_result({uri: [_diagnostic("lint")]}, [_receipt("lint", project)])],
            [project],
            lambda _project: existing,
        )
        return render_snapshot(apply_plan(plan, {project: existing})[project])

    first = _round(None)
    second = _round(parse_snapshot(first, project / "x.json"))

    assert first == second
