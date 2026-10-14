"""Tests that merging derives platform counts from results files alone.

The merge runs no analysis: it rebases the CI runner's absolute paths onto
the local workspace, replaces exactly the receipted partitions on the
receipt's platform, and refuses anything it cannot route — a foreign root it
cannot map, a non-workspace scope, a failed run's empty results — instead of
recording a silent no-op that looks like success.
"""

from __future__ import annotations

import json
import pathlib

import cattrs
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
from fine_inspect_code.merge_diagnostics_baseline_action import (
    MergeDiagnosticsBaselineRunPayload,
    MergeDiagnosticsBaselineRunResult,
)
from fine_inspect_code.merge_diagnostics_baseline_handler import (
    MergeDiagnosticsBaselineHandler,
)
from fine_inspect_code.ratchet import DiagnosticsRatchetSettings
from fine_inspect_code.ratchet_impl import DiagnosticsRatchet
from fine_inspect_code.ratchet_matching import FindingIdentity
from fine_inspect_code.ratchet_record import RecordRefusedError
from fine_inspect_code.ratchet_snapshot import (
    PartitionSnapshot,
    Snapshot,
    render_snapshot,
)
from fine_inspect_code.record_diagnostics_baseline_action import (
    RecordStatus,
    Umbrella,
)

_FOREIGN_ROOT = "/home/runner/work/x/x"
_INSPECT_SOURCE = "fine_inspect_code.InspectCodeAction"


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


class _Sender:
    def __init__(self) -> None:
        self.results: list[MergeDiagnosticsBaselineRunResult] = []

    async def send(self, result: MergeDiagnosticsBaselineRunResult) -> None:
        self.results.append(result)


def _run_context(sender: _Sender) -> object:
    class _RunContext:
        meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        partial_result_sender = sender

    return _RunContext()


def _foreign(*parts: str) -> ResourceUri:
    return ResourceUri("file://" + _FOREIGN_ROOT + "/" + "/".join(parts))


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


def _receipt(
    contributor: str, project: str, platform: str = "darwin"
) -> PartitionReceipt:
    return PartitionReceipt(
        contributor=contributor,
        project=_foreign(project),
        platform=platform,
        target=ReceiptTarget.PROJECT,
        unjudged=(),
    )


def _envelope(
    entries: dict[str, object],
    source: str = _INSPECT_SOURCE,
    scope: str = "workspace",
) -> dict[str, object]:
    return {
        "finecode_results_version": 1,
        "actions": {source: {"scope": scope, "results": entries}},
    }


def _write_envelope(path: pathlib.Path, envelope: dict[str, object]) -> ResourceUri:
    _ = path.write_bytes(json.dumps(envelope).encode("utf-8"))
    return path_to_resource_uri(path)


def _structured_entry(
    messages: dict[ResourceUri, list[Diagnostic]],
    receipts: list[PartitionReceipt],
) -> dict[str, object]:
    unstructured = cattrs.Converter().unstructure(
        InspectCodeRunResult(messages=messages, receipts=receipts)
    )
    assert isinstance(unstructured, dict)
    return {"return_code": 1, "result": unstructured}


def _handler(
    root: pathlib.Path, projects: list[pathlib.Path]
) -> MergeDiagnosticsBaselineHandler:
    providers = _FakeWorkspaceInfoProvider(projects)
    return MergeDiagnosticsBaselineHandler(
        workspace_info_provider=providers,  # type: ignore[arg-type]
        project_info_provider=_FakeProjectInfoProvider(root),  # type: ignore[arg-type]
        ratchet=DiagnosticsRatchet(
            config=DiagnosticsRatchetSettings(),
            file_manager=FileManager(logger=_FakeLogger()),
            workspace_info_provider=providers,  # type: ignore[arg-type]
            logger=_FakeLogger(),
        ),
        file_manager=FileManager(logger=_FakeLogger()),
        logger=_FakeLogger(),  # type: ignore[arg-type]
    )


async def _merge(
    root: pathlib.Path,
    projects: list[pathlib.Path],
    files: list[ResourceUri],
    project_paths: list[ResourceUri] | None = None,
) -> MergeDiagnosticsBaselineRunResult:
    sender = _Sender()
    await _handler(root, projects).run(
        MergeDiagnosticsBaselineRunPayload(
            results_files=files, project_paths=project_paths
        ),
        _run_context(sender),  # type: ignore[arg-type]
    )
    assert len(sender.results) == 1
    return sender.results[0]


def _snapshot_path(project: pathlib.Path) -> pathlib.Path:
    return project / "finecode-diagnostics-baseline.json"


async def test_merge_records_foreign_root(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    envelope = _envelope(
        {
            _FOREIGN_ROOT: _structured_entry(
                {_foreign("proj", "a.py"): [_diagnostic()]},
                [_receipt("lint", "proj")],
            )
        }
    )
    uri = _write_envelope(tmp_path / "inspect.json", envelope)

    result = await _merge(tmp_path, [tmp_path, project], [uri])

    assert len(result.snapshots) == 1
    row = result.snapshots[0]
    assert row.umbrella is Umbrella.INSPECT_CODE
    assert row.platform == "darwin"
    assert row.status is RecordStatus.WRITTEN
    assert row.partitions > 0
    text = _snapshot_path(project).read_text(encoding="utf-8")
    assert "/home/runner" not in text
    parsed = json.loads(text)
    assert parsed["contributors"]["lint"]["platforms"] == ["darwin"]
    assert parsed["contributors"]["lint"]["paths"]["a.py"] == [
        {"source": "ruff", "code": "F401", "message": "unused import", "count": 1}
    ]


async def test_merge_replaces_only_receipted_partition_and_platform(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    lint_identity = FindingIdentity(
        contributor="lint", path="a.py", source="ruff", code="F401",
        message="unused import",
    )
    toolchains_identity = FindingIdentity(
        contributor="type_check", path="b.py", source="pyrefly", code="x",
        message="m",
    )
    before = Snapshot(
        contributors={
            "lint": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {lint_identity: 2}},
            ),
            "type_check": PartitionSnapshot(
                platforms=("linux",),
                counts_by_platform={"linux": {toolchains_identity: 1}},
            ),
        }
    )
    _ = _snapshot_path(project).parent.mkdir(parents=True, exist_ok=True)
    _ = _snapshot_path(project).write_text(render_snapshot(before), encoding="utf-8")
    envelope = _envelope(
        {
            _FOREIGN_ROOT: _structured_entry(
                {_foreign("proj", "a.py"): [_diagnostic()]},
                [_receipt("lint", "proj")],
            )
        }
    )
    uri = _write_envelope(tmp_path / "inspect.json", envelope)

    await _merge(tmp_path, [tmp_path, project], [uri])

    after = json.loads(_snapshot_path(project).read_text(encoding="utf-8"))
    assert after["contributors"]["type_check"] == json.loads(
        render_snapshot(before)
    )["contributors"]["type_check"]
    assert after["contributors"]["lint"]["platforms"] == ["darwin", "linux"]
    assert after["contributors"]["lint"]["paths"]["a.py"] == [
        {
            "source": "ruff",
            "code": "F401",
            "message": "unused import",
            "counts": {"darwin": 1, "linux": 2},
        }
    ]


async def test_merge_leaves_absent_project_untouched(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "proj"
    other = tmp_path / "other"
    other_snapshot = Snapshot(
        contributors={
            "lint": PartitionSnapshot(platforms=("linux",), counts_by_platform={})
        }
    )
    other_text = render_snapshot(other_snapshot)
    other.mkdir(parents=True)
    _ = _snapshot_path(other).write_text(other_text, encoding="utf-8")
    envelope = _envelope(
        {
            _FOREIGN_ROOT: _structured_entry(
                {_foreign("proj", "a.py"): [_diagnostic()]},
                [_receipt("lint", "proj")],
            )
        }
    )
    uri = _write_envelope(tmp_path / "inspect.json", envelope)

    result = await _merge(tmp_path, [tmp_path, project, other], [uri])

    assert _snapshot_path(other).read_text(encoding="utf-8") == other_text
    assert [row.project for row in result.snapshots] == [
        path_to_resource_uri(project)
    ]


async def test_merge_twice_reports_unchanged(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    envelope = _envelope(
        {
            _FOREIGN_ROOT: _structured_entry(
                {_foreign("proj", "a.py"): [_diagnostic()]},
                [_receipt("lint", "proj")],
            )
        }
    )
    uri = _write_envelope(tmp_path / "inspect.json", envelope)

    first = await _merge(tmp_path, [tmp_path, project], [uri])
    second = await _merge(tmp_path, [tmp_path, project], [uri])

    assert first.snapshots[0].status is RecordStatus.WRITTEN
    assert second.snapshots[0].status is RecordStatus.UNCHANGED


@pytest.mark.parametrize(
    "envelope",
    [
        {"finecode_results_version": 1, "actions": {}},
        {"finecode_results_version": 1, "actions": []},
        _envelope({}, scope="project"),
        _envelope(
            {
                "/root-a": {"return_code": 0, "result": None},
                "/root-b": {"return_code": 0, "result": None},
            }
        ),
        _envelope(
            {
                _FOREIGN_ROOT: _structured_entry(
                    {ResourceUri("file:///elsewhere/x.py"): [_diagnostic()]},
                    [_receipt("lint", "proj")],
                )
            }
        ),
        _envelope(
            {
                _FOREIGN_ROOT: {
                    "return_code": 1,
                    "result": None,
                }
            }
        ),
        _envelope(
            {
                _FOREIGN_ROOT: _structured_entry(
                    {_foreign("proj3", "a.py"): [_diagnostic()]},
                    [_receipt("lint", "proj3")],
                )
            }
        ),
        _envelope(
            {"whatever": {"scope": "workspace", "results": {}}},
            source="fine_lint.LintAction",
        ),
    ],
    ids=[
        "empty-actions",
        "actions-list-from-failed-run",
        "non-workspace-scope",
        "two-hosting-roots",
        "uri-outside-source-root",
        "null-result-from-failed-run",
        "unknown-local-project",
        "unknown-action-source",
    ],
)
async def test_merge_refuses_unroutable_files(
    tmp_path: pathlib.Path, envelope: dict[str, object]
) -> None:
    project = tmp_path / "proj"
    uri = _write_envelope(tmp_path / "inspect.json", envelope)

    with pytest.raises(RecordRefusedError):
        await _merge(tmp_path, [tmp_path, project], [uri])
