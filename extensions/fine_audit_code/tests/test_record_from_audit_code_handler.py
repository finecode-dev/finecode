"""Tests that the audit half of recording behaves like the inspect half.

The audit handler runs the same shape over ``audit_code``: it pre-parses
snapshots before any analysis, replaces only receipted partitions, and sends
one row per project. Where ``audit_code`` is not declared at all it sends an
empty result instead of failing — recording must work in a workspace that
never had the audit umbrella.
"""

from __future__ import annotations

import pathlib

import pytest
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
from fine_inspect_code.ratchet import DiagnosticsRatchetSettings
from fine_inspect_code.ratchet_impl import DiagnosticsRatchet, running_platform
from fine_inspect_code.ratchet_record import RecordRefusedError
from fine_inspect_code.record_diagnostics_baseline_action import (
    RecordDiagnosticsBaselineRunPayload,
    RecordDiagnosticsBaselineRunResult,
    RecordStatus,
    Umbrella,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_runner.impls.file_manager import FileManager

from fine_audit_code.audit_code_action import AuditCodeRunResult
from fine_audit_code.record_from_audit_code_handler import RecordFromAuditCodeHandler


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
    def __init__(
        self,
        projects: list[pathlib.Path],
        declares_audit: bool = True,
        audit_projects: list[pathlib.Path] | None = None,
    ) -> None:
        self._projects = [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in projects
        ]
        if audit_projects is not None:
            self._audit_projects = set(audit_projects)
        elif declares_audit:
            self._audit_projects = set(projects)
        else:
            self._audit_projects = set()

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return self._projects

    async def init(self) -> None:
        return None

    async def get_workspace_root(self) -> pathlib.Path | None:
        return None

    async def project_declares_action(
        self, path: pathlib.Path, action_source: str
    ) -> bool:
        _ = action_source
        return path in self._audit_projects


class _FakeProjectInfoProvider:
    def __init__(self, root: pathlib.Path) -> None:
        self._root = root

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self._root


class _FakeWorkspaceActionRunner:
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
    ) -> dict[pathlib.Path, AuditCodeRunResult]:
        _ = (action_type, payload, meta, project_paths, concurrently)
        self.calls.append(payload)
        return {
            pathlib.Path("/ws"): AuditCodeRunResult(
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


def _diagnostic(contributor: str = "check_imports") -> Diagnostic:
    return Diagnostic(
        range=Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=1),
        ),
        message="broken contract",
        code="contract",
        source="import-linter",
        severity=DiagnosticSeverity.ERROR,
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
    declares_audit: bool = True,
    audit_projects: list[pathlib.Path] | None = None,
) -> RecordFromAuditCodeHandler:
    providers = _FakeWorkspaceInfoProvider(projects, declares_audit, audit_projects)
    return RecordFromAuditCodeHandler(
        workspace_action_runner=runner,  # type: ignore[arg-type]
        workspace_info_provider=providers,
        project_info_provider=_FakeProjectInfoProvider(root),  # type: ignore[arg-type]
        ratchet=DiagnosticsRatchet(
            config=DiagnosticsRatchetSettings(),
            file_manager=FileManager(logger=_FakeLogger()),
            workspace_info_provider=providers,
            logger=_FakeLogger(),
        ),
        file_manager=FileManager(logger=_FakeLogger()),
        logger=_FakeLogger(),
    )


async def test_audit_records_its_partitions(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    uri = path_to_resource_uri(project / "pyproject.toml")
    messages = {uri: [_diagnostic()]}
    receipts = [_receipt("check_imports", project)]
    handler = _handler(
        tmp_path, [project], _FakeWorkspaceActionRunner(messages, receipts)
    )
    sender = _Sender()
    await handler.run(
        RecordDiagnosticsBaselineRunPayload(),
        _run_context(sender),  # type: ignore[arg-type]
    )

    assert len(sender.results) == 1
    row = sender.results[0].snapshots[0]
    assert row.umbrella is Umbrella.AUDIT_CODE
    assert row.status is RecordStatus.WRITTEN
    assert row.partitions == 1


async def test_undeclared_audit_sends_empty_result(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    runner = _FakeWorkspaceActionRunner({}, [])
    handler = _handler(tmp_path, [project], runner, declares_audit=False)
    sender = _Sender()
    await handler.run(
        RecordDiagnosticsBaselineRunPayload(),
        _run_context(sender),  # type: ignore[arg-type]
    )

    assert len(sender.results) == 1
    assert sender.results[0].snapshots == []
    assert runner.calls == []


async def test_subproject_audit_records_without_root_declaration(
    tmp_path: pathlib.Path,
) -> None:
    """The workspace root is only the dispatch host: audit_code declared by a
    subproject alone is enough to record that subproject."""
    project = tmp_path / "pkg"
    uri = path_to_resource_uri(project / "pyproject.toml")
    messages = {uri: [_diagnostic()]}
    receipts = [_receipt("check_imports", project)]
    handler = _handler(
        tmp_path,
        [tmp_path, project],
        _FakeWorkspaceActionRunner(messages, receipts),
        audit_projects=[project],
    )
    sender = _Sender()
    await handler.run(
        RecordDiagnosticsBaselineRunPayload(),
        _run_context(sender),  # type: ignore[arg-type]
    )

    assert len(sender.results) == 1
    rows = sender.results[0].snapshots
    assert [row.project for row in rows] == [path_to_resource_uri(project)]
    assert rows[0].umbrella is Umbrella.AUDIT_CODE
    assert rows[0].status is RecordStatus.WRITTEN


async def test_broken_snapshot_refuses_before_analysis(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    _ = (project / "finecode-diagnostics-baseline.json").write_text(
        "not json", encoding="utf-8"
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
