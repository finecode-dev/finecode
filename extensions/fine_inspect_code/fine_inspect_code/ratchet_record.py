from __future__ import annotations

import collections.abc
import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    DiagnosticFilesRunResult,
    ReceiptTarget,
    fails_run,
)
from fine_inspect_code.ratchet_matching import (
    FindingIdentity,
    identity_of,
    owner_project,
)
from fine_inspect_code.ratchet_snapshot import Snapshot, replace_partitions

__all__ = [
    "PartitionKey",
    "RecordPlan",
    "RecordRefusedError",
    "apply_plan",
    "plan_from_results",
]


class RecordRefusedError(code_action.ActionFailedException):
    """A recording was refused before changing any snapshot.

    Raised for whole-project violations (a file-scoped receipt, receipts from
    more than one platform, no receipts at all) and for results that cannot be
    routed onto local projects. The message names the offending receipt or file.
    """


@dataclasses.dataclass(frozen=True)
class PartitionKey:
    project: pathlib.Path
    contributor: str


@dataclasses.dataclass
class RecordPlan:
    platform: str
    counts: dict[PartitionKey, dict[FindingIdentity, int]]


def _owned_relative(
    uri: ResourceUri,
    project: pathlib.Path,
    projects: collections.abc.Sequence[pathlib.Path],
) -> str | None:
    if owner_project(uri, projects) != project:
        return None
    try:
        return resource_uri_to_path(uri).relative_to(project).as_posix()
    except ValueError:
        return None


def plan_from_results(
    results: collections.abc.Sequence[DiagnosticFilesRunResult],
    projects: collections.abc.Sequence[pathlib.Path],
    existing: collections.abc.Callable[[pathlib.Path], Snapshot | None],
) -> RecordPlan:
    receipts = [
        receipt for result in results for receipt in result.receipts
    ]
    if not receipts:
        raise RecordRefusedError(
            "cannot record a diagnostics baseline: no partition receipt found; "
            "run the check first so every contributor judges its projects"
        )
    for receipt in receipts:
        if receipt.target is ReceiptTarget.FILES:
            raise RecordRefusedError(
                "cannot record a diagnostics baseline: recording is whole-project, "
                f"but receipt {receipt!r} is file-scoped; re-run without narrowing"
            )
    platforms = {receipt.platform for receipt in receipts}
    if len(platforms) != 1:
        raise RecordRefusedError(
            "cannot record a diagnostics baseline: receipts name more than one "
            f"platform ({sorted(platforms)}); record each platform separately"
        )
    platform = next(iter(platforms))

    counts: dict[PartitionKey, dict[FindingIdentity, int]] = {}
    for receipt in receipts:
        try:
            project = resource_uri_to_path(receipt.project)
        except ValueError as error:
            raise RecordRefusedError(
                "cannot record a diagnostics baseline: "
                f"receipt {receipt!r} names no local project"
            ) from error
        contributor = receipt.contributor
        unjudged: set[str] = set()
        for uri in receipt.unjudged:
            relative = _owned_relative(uri, project, projects)
            if relative is not None:
                unjudged.add(relative)
        known: dict[FindingIdentity, int] = {}
        current = existing(project)
        if current is not None:
            partition = current.contributors.get(contributor)
            if partition is not None:
                known = partition.counts_by_platform.get(platform, {})
        found: dict[FindingIdentity, int] = {}
        for result in results:
            for uri, diagnostics in result.messages.items():
                relative = _owned_relative(uri, project, projects)
                if relative is None:
                    continue
                for diagnostic in diagnostics:
                    if _recordable(diagnostic, contributor):
                        identity = identity_of(diagnostic, contributor, relative)
                        found[identity] = found.get(identity, 0) + 1
        fresh = {
            identity: count
            for identity, count in found.items()
            if identity.path not in unjudged
        }
        kept = {
            identity: count
            for identity, count in known.items()
            if identity.path in unjudged
        }
        counts[PartitionKey(project=project, contributor=contributor)] = fresh | kept
    return RecordPlan(platform=platform, counts=counts)


def _recordable(diagnostic: Diagnostic, contributor: str) -> bool:
    return (
        diagnostic.origin is not None
        and diagnostic.origin.contributor == contributor
        and fails_run(diagnostic)
        and diagnostic.baseline is not BaselineStatus.OUTSIDE
    )


def apply_plan(
    plan: RecordPlan,
    snapshots_by_project: dict[pathlib.Path, Snapshot | None],
) -> dict[pathlib.Path, Snapshot]:
    projects = list(snapshots_by_project) + [
        key.project for key in plan.counts if key.project not in snapshots_by_project
    ]
    return {
        project: replace_partitions(
            snapshots_by_project.get(project),
            plan.platform,
            {
                key.contributor: counts
                for key, counts in plan.counts.items()
                if key.project == project
            },
        )
        for project in projects
    }
