"""Tests that recording writes exactly the receipted partitions.

Recording runs an umbrella and replaces, per project, only the partitions
the run holds receipts for: a second recording without changes writes
nothing, a broken snapshot refuses before any analysis runs, and a
contributor that never ran keeps its recorded counts byte-for-byte.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from finecode_extension_api import code_action
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
    FindingOrigin,
    PartitionReceipt,
    Position,
    Range,
    ReceiptTarget,
)
from fine_inspect_code.inspect_code_action import InspectCodeRunResult
from fine_inspect_code.ratchet import DiagnosticsRatchetSettings
from fine_inspect_code.ratchet_impl import DiagnosticsRatchet, running_platform
from fine_inspect_code.ratchet_matching import FindingIdentity
from fine_inspect_code.ratchet_record import RecordRefusedError
from fine_inspect_code.ratchet_snapshot import (
    PartitionSnapshot,
    Snapshot,
    render_snapshot,
)
from fine_inspect_code.record_diagnostics_baseline_action import (
    RecordDiagnosticsBaselineRunPayload,
    RecordDiagnosticsBaselineRunResult,
    RecordedSnapshot,
    RecordStatus,
    Umbrella,
)
from fine_inspect_code.record_from_inspect_code_handler import (
    RecordFromInspectCodeHandler,
)


class _FakeLogger:
    def exception(self, exception: Exception) -> None: ...

    def trace(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...

    def disable(self, package: str) -> None: ...

    def enable(self, package: str) -> None: ...


class _FakeWorkspaceInfoProvider:
    def __init__(self, projects: list[pathlib.Path]) -> None:
        self._projects = [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in projects
        ]

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


class _FakeProjectInfoProvider:
    def __init__(self, root: pathlib.Path) -> None:
        self._root = root

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self._root


class _FakeWorkspaceActionRunner:
    """Answers like post-ratchet bridges would: stamped findings plus receipts."""

    def __init__(
        self,
        messages: dict[ResourceUri, list[Diagnostic]],
        receipts: list[PartitionReceipt],
    ) -> None:
        self.calls: list[object] = []
        self._messages = messages
        self._receipts = receipts

    async def run_action_in_projects(
        self,
        action_type: type,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, InspectCodeRunResult]:
        _ = (action_type, payload, meta, project_paths, concurrently)
        self.calls.append(payload)
        return {
            pathlib.Path("/ws"): InspectCodeRunResult(
                messages=dict(self._messages), receipts=list(self._receipts)
            )
        }


class _Sender:
    def __init__(self) -> None:
        self.results: list[RecordDiagnosticsBaselineRunResult] = []

    async def send(self, result: RecordDiagnosticsBaselineRunResult) -> None:
        self.results.append(result)


def _run_context(sender: _Sender) -> object:
    class _RunContext:
        meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        partial_result_sender = sender

    return _RunContext()


def _diagnostic(contributor: str = "lint") -> Diagnostic:
    return Diagnostic(
        range=Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=1),
        ),
        message="unused import",
        code="F401",
        source="ruff",
        severity=DiagnosticSeverity.WARNING,
        origin=FindingOrigin(contributor=contributor),
        baseline=BaselineStatus.NEW,
    )


def _receipt(contributor: str, project: pathlib.Path) -> PartitionReceipt:
    return PartitionReceipt(
        contributor=contributor,
        project=path_to_resource_uri(project),
        platform=running_platform(),
        target=ReceiptTarget.PROJECT,
        unjudged=(),
    )


def _handler(
    root: pathlib.Path,
    projects: list[pathlib.Path],
    runner: _FakeWorkspaceActionRunner,
) -> RecordFromInspectCodeHandler:
    return RecordFromInspectCodeHandler(
        workspace_action_runner=runner,  # type: ignore[arg-type]
        workspace_info_provider=_FakeWorkspaceInfoProvider(projects),
        project_info_provider=_FakeProjectInfoProvider(root),
        ratchet=DiagnosticsRatchet(
            config=DiagnosticsRatchetSettings(),
            file_manager=FileManager(logger=_FakeLogger()),
            workspace_info_provider=_FakeWorkspaceInfoProvider(projects),
            logger=_FakeLogger(),
        ),
        file_manager=FileManager(logger=_FakeLogger()),
        logger=_FakeLogger(),  # type: ignore[arg-type]
    )


async def _run(
    root: pathlib.Path,
    projects: list[pathlib.Path],
    messages: dict[ResourceUri, list[Diagnostic]],
    receipts: list[PartitionReceipt],
    project_paths: list[ResourceUri] | None = None,
) -> RecordDiagnosticsBaselineRunResult:
    handler = _handler(
        root, projects, _FakeWorkspaceActionRunner(messages, receipts)
    )
    sender = _Sender()
    await handler.run(
        RecordDiagnosticsBaselineRunPayload(project_paths=project_paths),
        _run_context(sender),  # type: ignore[arg-type]
    )
    assert len(sender.results) == 1
    return sender.results[0]


async def test_record_writes_then_reports_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    uri = path_to_resource_uri(project / "a.py")
    messages = {uri: [_diagnostic()]}
    receipts = [_receipt("lint", project)]

    first = await _run(tmp_path, [project], messages, receipts)
    snapshot_file = project / "finecode-diagnostics-baseline.json"

    assert first.snapshots[0].status is RecordStatus.WRITTEN
    assert first.snapshots[0].umbrella is Umbrella.INSPECT_CODE
    assert first.snapshots[0].partitions == 1
    assert first.return_code is code_action.RunReturnCode.SUCCESS
    raw = snapshot_file.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")

    second = await _run(tmp_path, [project], messages, receipts)

    assert second.snapshots[0].status is RecordStatus.UNCHANGED
    assert snapshot_file.read_bytes() == raw


async def test_clean_project_writes_no_file(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"

    result = await _run(
        tmp_path, [project], {}, [_receipt("lint", project)]
    )

    assert result.snapshots[0].status is RecordStatus.SKIPPED
    assert not (project / "finecode-diagnostics-baseline.json").exists()


async def test_broken_snapshot_refuses_before_analysis(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    _ = (project / "finecode-diagnostics-baseline.json").write_text(
        "<<<<<<< HEAD\n", encoding="utf-8"
    )
    runner = _FakeWorkspaceActionRunner({}, [])
    handler = _handler(tmp_path, [project], runner)
    sender = _Sender()

    with pytest.raises(RecordRefusedError):
        await handler.run(
            RecordDiagnosticsBaselineRunPayload(),
            _run_context(sender),  # type: ignore[arg-type]
        )

    assert runner.calls == []
    assert sender.results == []


async def test_contributor_without_receipt_keeps_its_partition(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    recorded = FindingIdentity(
        contributor="audit_preset_deps",
        path="pyproject.toml",
        source="preset",
        code="dep",
        message="missing",
    )
    before = render_snapshot(
        Snapshot(
            contributors={
                "audit_preset_deps": PartitionSnapshot(
                    platforms=("linux",),
                    counts_by_platform={"linux": {recorded: 1}},
                )
            }
        )
    )
    project.mkdir(parents=True)
    snapshot_file = project / "finecode-diagnostics-baseline.json"
    _ = snapshot_file.write_text(before, encoding="utf-8")
    uri = path_to_resource_uri(project / "a.py")

    result = await _run(
        tmp_path, [project], {uri: [_diagnostic()]}, [_receipt("lint", project)]
    )

    assert result.snapshots[0].status is RecordStatus.WRITTEN
    after = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert after["contributors"]["audit_preset_deps"] == json.loads(before)[
        "contributors"
    ]["audit_preset_deps"]
    assert set(after["contributors"]) == {"audit_preset_deps", "lint"}


async def test_result_update_merges_rows(tmp_path: pathlib.Path) -> None:
    project = path_to_resource_uri(tmp_path / "proj")
    snapshot_file = path_to_resource_uri(
        tmp_path / "proj" / "finecode-diagnostics-baseline.json"
    )
    first = RecordDiagnosticsBaselineRunResult(snapshots=[])
    second = RecordDiagnosticsBaselineRunResult(
        snapshots=[
            RecordedSnapshot(
                project=project,
                umbrella=Umbrella.INSPECT_CODE,
                snapshot_file=snapshot_file,
                platform="linux",
                status=RecordStatus.WRITTEN,
                partitions=1,
            )
        ]
    )

    first.update(second)

    assert first.snapshots == second.snapshots


def test_result_to_text_groups_per_project(tmp_path: pathlib.Path) -> None:
    project = path_to_resource_uri(tmp_path / "proj")
    snapshot_file = path_to_resource_uri(
        tmp_path / "proj" / "finecode-diagnostics-baseline.json"
    )
    result = RecordDiagnosticsBaselineRunResult(
        snapshots=[
            RecordedSnapshot(
                project=project,
                umbrella=Umbrella.AUDIT_CODE,
                snapshot_file=snapshot_file,
                platform="linux",
                status=RecordStatus.UNCHANGED,
                partitions=2,
            ),
            RecordedSnapshot(
                project=project,
                umbrella=Umbrella.INSPECT_CODE,
                snapshot_file=snapshot_file,
                platform="linux",
                status=RecordStatus.WRITTEN,
                partitions=1,
            ),
        ]
    )

    assert result.to_text() == (
        f"{project}:\n"
        f"  audit_code: unchanged (2 partitions) [{snapshot_file}]\n"
        f"  inspect_code: written (1 partitions) [{snapshot_file}]\n"
    )
    assert RecordDiagnosticsBaselineRunResult(snapshots=[]).to_text() == (
        "no snapshots recorded\n"
    )
