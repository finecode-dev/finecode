from __future__ import annotations

import collections.abc
import dataclasses
import pathlib
import re
import typing
import urllib.parse

from finecode_extension_api.code_action import ItemCoverage
from finecode_extension_api.resource_uri import ResourceUri

from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    FindingOrigin,
    PartitionReceipt,
    RatchetTarget,
    ReceiptTarget,
    StaleBaselineEntry,
    fails_run,
)

if typing.TYPE_CHECKING:
    from fine_inspect_code.ratchet_snapshot import Snapshot

__all__ = [
    "FindingIdentity",
    "RatchetOutcome",
    "RatchetScope",
    "RatchetTarget",
    "apply_snapshot",
    "identity_of",
    "normalize_message",
    "owner_project",
]

_LINE_REF_PATTERNS: dict[tuple[str, str], re.Pattern[str]] = {
    ("ruff", "F811"): re.compile(r" from line \d+"),
}


@dataclasses.dataclass(frozen=True)
class FindingIdentity:
    contributor: str
    path: str
    source: str
    code: str
    message: str


def normalize_message(source: str, code: str, message: str) -> str:
    first_line = message.split("\n", 1)[0].strip()
    pattern = _LINE_REF_PATTERNS.get((source.casefold(), code))
    if pattern is not None:
        first_line = pattern.sub("", first_line)
    return first_line


def _parses_as_int(code: str) -> bool:
    try:
        _ = int(code)
    except ValueError:
        return False
    return True


def identity_of(
    diagnostic: Diagnostic, contributor: str, path: str
) -> FindingIdentity:
    source = (diagnostic.source or "").casefold()
    code = diagnostic.code or ""
    if source == "pyrefly" and _parses_as_int(code) and diagnostic.code_description:
        code = diagnostic.code_description
    return FindingIdentity(
        contributor=contributor,
        path=path,
        source=source,
        code=code,
        message=normalize_message(source, code, diagnostic.message),
    )


@dataclasses.dataclass(frozen=True)
class RatchetScope:
    target: RatchetTarget
    files: tuple[ResourceUri, ...] = ()

    @classmethod
    def for_payload(
        cls, target: str, file_paths: collections.abc.Sequence[ResourceUri]
    ) -> RatchetScope:
        return cls(target=RatchetTarget(target), files=tuple(file_paths))

    @classmethod
    def mark_only(cls) -> RatchetScope:
        return cls(target=RatchetTarget.MARK_ONLY)


@dataclasses.dataclass
class RatchetOutcome:
    messages: dict[ResourceUri, list[Diagnostic]]
    stale: list[StaleBaselineEntry]
    receipts: list[PartitionReceipt]


def _uri_to_path(file_uri: ResourceUri) -> pathlib.Path | None:
    if not file_uri.startswith("file://"):
        return None
    return pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(file_uri).path))


def owner_project(
    file_uri: ResourceUri, projects: collections.abc.Sequence[pathlib.Path]
) -> pathlib.Path | None:
    path = _uri_to_path(file_uri)
    if path is None:
        return None
    best: pathlib.Path | None = None
    for project in projects:
        try:
            _ = path.relative_to(project)
        except ValueError:
            continue
        if best is None or len(project.parts) > len(best.parts):
            best = project
    return best


def _relative_posix(file_uri: ResourceUri, project: pathlib.Path) -> str | None:
    path = _uri_to_path(file_uri)
    if path is None:
        return None
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return None


def apply_snapshot(
    *,
    contributor: str,
    checked_project: pathlib.Path,
    projects: collections.abc.Sequence[pathlib.Path],
    snapshot_path: pathlib.Path,
    snapshot: Snapshot | None,
    platform: str,
    messages: dict[ResourceUri, list[Diagnostic]],
    scope: RatchetScope,
    unhandled: collections.abc.Sequence[ItemCoverage],
) -> RatchetOutcome:
    whole_call_unhandled = any(entry.item is None for entry in unhandled)
    unjudged_paths: set[str] = set()
    for entry in unhandled:
        if entry.item is None:
            continue
        if owner_project(entry.item, projects) == checked_project:
            relative = _relative_posix(entry.item, checked_project)
            if relative is not None:
                unjudged_paths.add(relative)

    stamped: dict[ResourceUri, list[Diagnostic]] = {}
    # Each home entry points at its copy in `stamped`: the identity, the uri
    # key, the index in that uri's list, and the input order for tie-breaking.
    home: list[tuple[FindingIdentity, ResourceUri, int, int]] = []
    order = 0
    for uri, diagnostics in messages.items():
        stamped_diagnostics: list[Diagnostic] = []
        for diagnostic in diagnostics:
            marked = dataclasses.replace(
                diagnostic, origin=FindingOrigin(contributor=contributor)
            )
            if fails_run(diagnostic):
                owner = owner_project(uri, projects)
                if owner is not None and owner != checked_project:
                    marked = dataclasses.replace(
                        marked, baseline=BaselineStatus.OUTSIDE
                    )
                elif owner is not None:
                    relative = _relative_posix(uri, checked_project)
                    if relative is not None:
                        home.append(
                            (
                                identity_of(diagnostic, contributor, relative),
                                uri,
                                len(stamped_diagnostics),
                                order,
                            )
                        )
                        order += 1
            stamped_diagnostics.append(marked)
        stamped[uri] = stamped_diagnostics

    partition = snapshot.contributors.get(contributor) if snapshot is not None else None
    can_judge = partition is not None and platform in partition.platforms
    recorded: dict[FindingIdentity, int] = (
        partition.counts_by_platform.get(platform, {})
        if can_judge and partition is not None
        else {}
    )

    indices_by_identity: dict[FindingIdentity, list[int]] = {}
    for index, (identity, _uri, _pos, _order) in enumerate(home):
        indices_by_identity.setdefault(identity, []).append(index)

    def _sort_key(index: int) -> tuple[int, int, int]:
        _identity, uri, pos, input_order = home[index]
        current = stamped[uri][pos]
        return (
            current.range.start.line,
            current.range.start.character,
            input_order,
        )

    for identity, indices in indices_by_identity.items():
        indices.sort(key=_sort_key)
        allowed = recorded.get(identity, 0)
        for rank, index in enumerate(indices):
            _identity, uri, pos, _input_order = home[index]
            if snapshot is None:
                continue
            stamped[uri][pos] = dataclasses.replace(
                stamped[uri][pos],
                baseline=BaselineStatus.KNOWN if rank < allowed else BaselineStatus.NEW,
            )

    judged = scope.target in (RatchetTarget.PROJECT, RatchetTarget.FILES)
    stale: list[StaleBaselineEntry] = []
    if judged and not whole_call_unhandled and can_judge:
        in_scope: set[str] = set()
        if scope.target is RatchetTarget.FILES:
            for uri in scope.files:
                if owner_project(uri, projects) == checked_project:
                    relative = _relative_posix(uri, checked_project)
                    if relative is not None:
                        in_scope.add(relative)
        for identity, count in recorded.items():
            found = len(indices_by_identity.get(identity, []))
            leftover = count - found
            if leftover <= 0:
                continue
            if scope.target is RatchetTarget.FILES and identity.path not in in_scope:
                continue
            if identity.path in unjudged_paths:
                continue
            stale.append(
                StaleBaselineEntry(
                    contributor=contributor,
                    snapshot_file=ResourceUri(snapshot_path.as_uri()),
                    file=ResourceUri((checked_project / identity.path).as_uri()),
                    source=identity.source,
                    code=identity.code,
                    message=identity.message,
                    stale_count=leftover,
                )
            )
        stale.sort(
            key=lambda entry: (entry.file, entry.source, entry.code, entry.message)
        )

    receipts: list[PartitionReceipt] = []
    if judged and not whole_call_unhandled:
        # The branch guarantees every unhandled item is non-None: a whole-call
        # entry yields no receipt at all.
        receipts.append(
            PartitionReceipt(
                contributor=contributor,
                project=ResourceUri(checked_project.as_uri()),
                platform=platform,
                target=ReceiptTarget(scope.target.value),
                unjudged=tuple(
                    entry.item for entry in unhandled if entry.item is not None
                ),
            )
        )

    return RatchetOutcome(messages=stamped, stale=stale, receipts=receipts)
