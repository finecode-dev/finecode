"""Tests that the ratchet service reads each snapshot once and contains damage.

The service sits on every contributor's emit path in a long-lived runner: it
must read the snapshot file once per call, reuse its parse while the bytes
are unchanged, and turn a broken file into a visible error on that project
alone — never an exception that cancels sibling projects, and never a write.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import ifilemanager
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_runner.impls.file_manager import FileManager

from fine_inspect_code.diagnostic_types import (
    BaselineStatus,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)
from fine_inspect_code.ratchet import DiagnosticsRatchetSettings
from fine_inspect_code.ratchet_impl import DiagnosticsRatchet, running_platform
from fine_inspect_code.ratchet_matching import (
    FindingIdentity,
    RatchetScope,
    RatchetTarget,
)
from fine_inspect_code.ratchet_snapshot import (
    PartitionSnapshot,
    Snapshot,
    render_snapshot,
)

_SNAPSHOT_FILE = "finecode-diagnostics-baseline.json"


class _FakeLogger:
    def exception(self, exception: Exception) -> None: ...

    def trace(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...

    def disable(self, package: str) -> None: ...

    def enable(self, package: str) -> None: ...


class _CountingFileManager(FileManager):
    def __init__(self, logger: _FakeLogger) -> None:
        super().__init__(logger=logger)
        self.get_content_calls = 0
        self.get_file_version_calls = 0

    async def get_content(self, file_path: pathlib.Path) -> str:
        self.get_content_calls += 1
        return await super().get_content(file_path)

    async def get_file_version(self, file_path: pathlib.Path) -> str:
        self.get_file_version_calls += 1
        return await super().get_file_version(file_path)


class _FakeWorkspaceInfoProvider:
    def __init__(self, projects: list[WorkspaceProject]) -> None:
        self._projects = projects

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return self._projects

    async def init(self) -> None:
        return None

    async def get_workspace_root(self) -> pathlib.Path | None:
        return None

    async def project_declares_action(
        self, path: pathlib.Path, action_source: str
    ) -> bool:
        _ = (path, action_source)
        return True


def _projects(root: pathlib.Path) -> list[WorkspaceProject]:
    return [
        WorkspaceProject(path=root, config_status=ProjectConfigStatus.VALID),
        WorkspaceProject(
            path=root / "proj", config_status=ProjectConfigStatus.VALID
        ),
    ]


def _ratchet(
    tmp_path: pathlib.Path,
    file_manager: _CountingFileManager | None = None,
    settings: DiagnosticsRatchetSettings | None = None,
) -> tuple[DiagnosticsRatchet, _CountingFileManager]:
    manager = file_manager if file_manager is not None else _CountingFileManager(
        _FakeLogger()
    )
    ratchet = DiagnosticsRatchet(
        config=settings if settings is not None else DiagnosticsRatchetSettings(),
        file_manager=manager,
        workspace_info_provider=_FakeWorkspaceInfoProvider(_projects(tmp_path)),
        logger=_FakeLogger(),
    )
    return ratchet, manager


def _diagnostic(message: str = "unused import") -> Diagnostic:
    return Diagnostic(
        range=Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=1),
        ),
        message=message,
        code="F401",
        source="ruff",
        severity=DiagnosticSeverity.WARNING,
    )


def _write_snapshot(project: pathlib.Path, text: str) -> pathlib.Path:
    project.mkdir(parents=True, exist_ok=True)
    path = project / _SNAPSHOT_FILE
    _ = path.write_text(text, encoding="utf-8")
    return path


def _snapshot_text(count: int) -> str:
    return render_snapshot(
        Snapshot(
            contributors={
                "lint": PartitionSnapshot(
                    platforms=("linux",),
                    counts_by_platform={
                        "linux": {
                            FindingIdentity(
                                contributor="lint",
                                path="a.py",
                                source="ruff",
                                code="F401",
                                message="unused import",
                            ): count
                        }
                    },
                )
            }
        )
    )


async def test_missing_file_leaves_findings_unmarked(tmp_path: pathlib.Path) -> None:
    ratchet, _manager = _ratchet(tmp_path)
    uri = path_to_resource_uri(tmp_path / "proj" / "a.py")

    outcome = await ratchet.apply(
        contributor="lint",
        checked_project=tmp_path / "proj",
        messages={uri: [_diagnostic()]},
        scope=RatchetScope(RatchetTarget.PROJECT),
        unhandled=[],
    )

    assert [d.baseline for d in outcome.messages[uri]] == [None]
    assert len(outcome.receipts) == 1


async def test_one_content_read_per_call_and_no_version_read(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    _ = _write_snapshot(project, _snapshot_text(1))
    ratchet, manager = _ratchet(tmp_path)
    uri = path_to_resource_uri(project / "a.py")
    messages = {uri: [_diagnostic()]}

    first = await ratchet.apply(
        contributor="lint",
        checked_project=project,
        messages=messages,
        scope=RatchetScope(RatchetTarget.PROJECT),
        unhandled=[],
    )
    second = await ratchet.apply(
        contributor="lint",
        checked_project=project,
        messages=messages,
        scope=RatchetScope(RatchetTarget.PROJECT),
        unhandled=[],
    )

    assert manager.get_content_calls == 2
    assert manager.get_file_version_calls == 0
    assert [d.baseline for d in first.messages[uri]] == [BaselineStatus.KNOWN]
    assert [d.baseline for d in second.messages[uri]] == [BaselineStatus.KNOWN]


async def test_changed_content_is_reparsed(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    path = _write_snapshot(project, _snapshot_text(1))
    ratchet, _manager = _ratchet(tmp_path)
    uri = path_to_resource_uri(project / "a.py")

    async def _marks() -> list[BaselineStatus | None]:
        outcome = await ratchet.apply(
            contributor="lint",
            checked_project=project,
            messages={uri: [_diagnostic()]},
            scope=RatchetScope(RatchetTarget.PROJECT),
            unhandled=[],
        )
        return [d.baseline for d in outcome.messages[uri]]

    assert await _marks() == [BaselineStatus.KNOWN]
    _ = path.write_text(_snapshot_text(0), encoding="utf-8")
    assert await _marks() == [BaselineStatus.NEW]


async def test_broken_snapshot_is_a_contained_error(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    _ = _write_snapshot(project, "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> other")
    ratchet, _manager = _ratchet(tmp_path)
    uri = path_to_resource_uri(project / "a.py")

    outcome = await ratchet.apply(
        contributor="lint",
        checked_project=project,
        messages={uri: [_diagnostic()]},
        scope=RatchetScope(RatchetTarget.PROJECT),
        unhandled=[],
    )

    assert [d.baseline for d in outcome.messages[uri]] == [None]
    snapshot_uri = ResourceUri((project / _SNAPSHOT_FILE).as_uri())
    assert snapshot_uri in outcome.messages
    errors = outcome.messages[snapshot_uri]
    assert len(errors) == 1
    assert errors[0].severity is DiagnosticSeverity.ERROR
    assert errors[0].code == "baseline-snapshot-invalid"
    assert errors[0].source == "finecode"
    assert outcome.stale == []
    assert outcome.receipts == []


async def test_undecodable_snapshot_is_a_contained_error(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    _ = (project / _SNAPSHOT_FILE).write_bytes(b"\xff\xfe\x00bad")
    ratchet, _manager = _ratchet(tmp_path)

    outcome = await ratchet.apply(
        contributor="lint",
        checked_project=project,
        messages={},
        scope=RatchetScope(RatchetTarget.PROJECT),
        unhandled=[],
    )

    snapshot_uri = ResourceUri((project / _SNAPSHOT_FILE).as_uri())
    assert outcome.messages[snapshot_uri][0].code == "baseline-snapshot-invalid"


async def test_settings_default_and_override(tmp_path: pathlib.Path) -> None:
    ratchet, _manager = _ratchet(tmp_path)

    assert ratchet.snapshot_path(tmp_path) == tmp_path / _SNAPSHOT_FILE

    custom, _manager = _ratchet(
        tmp_path, settings=DiagnosticsRatchetSettings(snapshot_file="custom.json")
    )
    assert custom.snapshot_path(tmp_path) == tmp_path / "custom.json"


async def test_init_does_nothing(tmp_path: pathlib.Path) -> None:
    ratchet, _manager = _ratchet(tmp_path)

    assert await ratchet.init() is None


def test_running_platform_is_sys_platform() -> None:
    assert running_platform() == sys.platform


async def test_unhandled_item_is_passed_through(tmp_path: pathlib.Path) -> None:
    ratchet, _manager = _ratchet(tmp_path)
    uri = path_to_resource_uri(tmp_path / "proj" / "a.py")

    outcome = await ratchet.apply(
        contributor="lint",
        checked_project=tmp_path / "proj",
        messages={uri: [_diagnostic()]},
        scope=RatchetScope(RatchetTarget.PROJECT),
        unhandled=[
            ItemCoverage(status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, item=uri)
        ],
    )

    assert outcome.receipts[0].unjudged == (uri,)


async def test_missing_file_read_raises_file_not_found(
    tmp_path: pathlib.Path,
) -> None:
    manager = _CountingFileManager(_FakeLogger())

    with pytest.raises(ifilemanager.FileNotFound):
        await manager.get_content(tmp_path / "nope.json")
