from __future__ import annotations

import collections.abc
import dataclasses
import hashlib
import pathlib
import sys

from finecode_extension_api import service
from finecode_extension_api.code_action import ItemCoverage
from finecode_extension_api.interfaces import (
    ifilemanager,
    ilogger,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.resource_uri import ResourceUri

from fine_inspect_code.diagnostic_types import (
    Diagnostic,
    DiagnosticSeverity,
    FindingOrigin,
    Position,
    Range,
)
from fine_inspect_code.ratchet import DiagnosticsRatchetSettings
from fine_inspect_code.ratchet_matching import (
    RatchetOutcome,
    RatchetScope,
    apply_snapshot,
)
from fine_inspect_code.ratchet_snapshot import (
    BaselineSnapshotInvalidError,
    Snapshot,
    parse_snapshot,
)

__all__ = ["DiagnosticsRatchet", "running_platform"]


def running_platform() -> str:
    return sys.platform


class DiagnosticsRatchet(service.Service):
    def __init__(
        self,
        config: DiagnosticsRatchetSettings,
        file_manager: ifilemanager.IFileManager,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self._config = config
        self._file_manager = file_manager
        self._workspace_info_provider = workspace_info_provider
        self._logger = logger
        self._cache: dict[pathlib.Path, tuple[bytes, Snapshot]] = {}

    async def init(self) -> None:
        return None

    def snapshot_path(self, project_dir: pathlib.Path) -> pathlib.Path:
        return project_dir / self._config.snapshot_file

    async def apply(
        self,
        *,
        contributor: str,
        checked_project: pathlib.Path,
        messages: dict[ResourceUri, list[Diagnostic]],
        scope: RatchetScope,
        unhandled: collections.abc.Sequence[ItemCoverage],
    ) -> RatchetOutcome:
        projects = actionable_project_paths(
            await self._workspace_info_provider.get_workspace_projects()
        )
        path = self.snapshot_path(checked_project)
        try:
            snapshot = await self._load_snapshot(path)
        except BaselineSnapshotInvalidError as error:
            return _broken_snapshot_outcome(contributor, path, messages, error)
        return apply_snapshot(
            contributor=contributor,
            checked_project=checked_project,
            projects=projects,
            snapshot_path=path,
            snapshot=snapshot,
            platform=running_platform(),
            messages=messages,
            scope=scope,
            unhandled=unhandled,
        )

    async def _load_snapshot(self, path: pathlib.Path) -> Snapshot | None:
        try:
            content = await self._file_manager.get_content(path)
        except ifilemanager.FileNotFound:
            _ = self._cache.pop(path, None)
            return None
        except UnicodeDecodeError as error:
            raise BaselineSnapshotInvalidError(
                f"invalid diagnostics baseline snapshot {path}: {error}"
            ) from error
        digest = hashlib.sha256(content.encode("utf-8")).digest()
        cached = self._cache.get(path)
        if cached is not None and cached[0] == digest:
            return cached[1]
        snapshot = parse_snapshot(content, path)
        self._cache[path] = (digest, snapshot)
        return snapshot


def _broken_snapshot_outcome(
    contributor: str,
    path: pathlib.Path,
    messages: dict[ResourceUri, list[Diagnostic]],
    error: BaselineSnapshotInvalidError,
) -> RatchetOutcome:
    stamped: dict[ResourceUri, list[Diagnostic]] = {
        uri: [
            dataclasses.replace(diagnostic, origin=FindingOrigin(contributor=contributor))
            for diagnostic in diagnostics
        ]
        for uri, diagnostics in messages.items()
    }
    snapshot_uri = ResourceUri(path.as_uri())
    broken = Diagnostic(
        range=Range(start=Position(line=0, character=0), end=Position(line=0, character=0)),
        message=(
            f"invalid diagnostics baseline snapshot: {error}. Resolve the conflict "
            "by hand or take one side with git "
            f"(git checkout --theirs|--ours {path}), then re-record on Linux with "
            "record_diagnostics_baseline and re-merge other platforms with "
            "merge_diagnostics_baseline from the CI results files. Deleting the "
            "file and re-recording discards the baseline."
        ),
        code="baseline-snapshot-invalid",
        source="finecode",
        severity=DiagnosticSeverity.ERROR,
    )
    stamped.setdefault(snapshot_uri, []).append(broken)
    return RatchetOutcome(messages=stamped, stale=[], receipts=[])
