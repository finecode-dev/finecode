"""Tests that the check_imports bridge judges each project through the ratchet.

``check_imports`` always analyzes a project's whole import graph, so the
bridge cannot narrow *what* is checked — but each project's result must still
be judged once, as that project, with the payload's scope and its own
coverage. Otherwise a narrowed run would either let new findings in or report
recorded ones gone.
"""

from __future__ import annotations

import pathlib

from fine_audit_code.audit_code_action import (
    AuditCodeRunPayload,
    AuditCodeRunResult,
    AuditCodeTarget,
)
from fine_inspect_code.diagnostic_types import (
    Diagnostic,
    PartitionReceipt,
    RatchetTarget,
    StaleBaselineEntry,
)
from fine_inspect_code.ratchet_matching import RatchetOutcome, RatchetScope
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri

from fine_check_imports.check_imports_action import (
    CheckImportsRunPayload,
    CheckImportsRunResult,
)
from fine_check_imports.check_imports_audit_code_bridge_handler import (
    CheckImportsAuditCodeBridgeHandler,
)


class _FakeWorkspaceInfoProvider:
    def __init__(self, project_paths: list[pathlib.Path]) -> None:
        self._project_paths = project_paths

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in self._project_paths
        ]


class _FakeWorkspaceActionRunner:
    def __init__(
        self, unhandled: tuple[code_action.ItemCoverage, ...] = ()
    ) -> None:
        self.calls: list[CheckImportsRunPayload] = []
        self._unhandled = list(unhandled)

    async def run_action_in_projects(
        self,
        action_type: type,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, CheckImportsRunResult]:
        assert project_paths is not None
        assert isinstance(payload, CheckImportsRunPayload)
        self.calls.append(payload)
        return {
            path: CheckImportsRunResult(messages={}, coverage=list(self._unhandled))
            for path in project_paths
        }


class _RatchetCall:
    def __init__(
        self,
        contributor: str,
        checked_project: pathlib.Path,
        scope: RatchetScope,
        unhandled: list[code_action.ItemCoverage],
    ) -> None:
        self.contributor = contributor
        self.checked_project = checked_project
        self.scope = scope
        self.unhandled = unhandled


class _FakeRatchet:
    """Stands in for the ratchet: records each apply and returns fixed marks."""

    def __init__(
        self,
        stale: list[StaleBaselineEntry] | None = None,
        receipts: list[PartitionReceipt] | None = None,
    ) -> None:
        self.calls: list[_RatchetCall] = []
        self.stale = stale if stale is not None else []
        self.receipts = receipts if receipts is not None else []

    async def apply(
        self,
        *,
        contributor: str,
        checked_project: pathlib.Path,
        messages: dict[ResourceUri, list[Diagnostic]],
        scope: RatchetScope,
        unhandled: list[code_action.ItemCoverage],
    ) -> RatchetOutcome:
        self.calls.append(
            _RatchetCall(contributor, checked_project, scope, unhandled)
        )
        return RatchetOutcome(
            messages=messages,
            stale=self.stale,
            receipts=self.receipts,
        )


class _FakeLogger:
    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


class _CollectingPartialResultSender:
    def __init__(self) -> None:
        self.results: list[AuditCodeRunResult] = []

    async def send(self, result: AuditCodeRunResult) -> None:
        self.results.append(result)


def _run_context(sender: _CollectingPartialResultSender) -> object:
    class _RunContext:
        meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        partial_result_sender = sender

    return _RunContext()


async def _run(
    payload: AuditCodeRunPayload,
    known_project_paths: list[pathlib.Path],
    unhandled: tuple[code_action.ItemCoverage, ...] = (),
    ratchet: _FakeRatchet | None = None,
) -> tuple[_FakeWorkspaceActionRunner, _CollectingPartialResultSender, _FakeRatchet]:
    action_runner = _FakeWorkspaceActionRunner(unhandled)
    fake_ratchet = ratchet if ratchet is not None else _FakeRatchet()
    handler = CheckImportsAuditCodeBridgeHandler(
        workspace_action_runner=action_runner,  # type: ignore[arg-type]
        workspace_info_provider=_FakeWorkspaceInfoProvider(known_project_paths),
        logger=_FakeLogger(),  # type: ignore[arg-type]
        ratchet=fake_ratchet,  # type: ignore[arg-type]
    )
    sender = _CollectingPartialResultSender()
    await handler.run(payload, _run_context(sender))  # type: ignore[arg-type]
    return action_runner, sender, fake_ratchet


async def test_each_project_is_checked_in_full(tmp_path: pathlib.Path) -> None:
    """One whole-graph CheckImportsAction per project: the bridge never narrows
    what is analyzed, only which projects are asked."""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"

    action_runner, _, _ = await _run(AuditCodeRunPayload(), [project_a, project_b])

    assert len(action_runner.calls) == 2
    assert all(isinstance(call, CheckImportsRunPayload) for call in action_runner.calls)


async def test_findings_pass_through_the_ratchet_once_per_project(
    tmp_path: pathlib.Path,
) -> None:
    """Each project's result is judged once, as that project, with the
    payload's scope and its own coverage — and the partial carries the judged
    outcome, not the raw sub-result."""
    project_a = tmp_path / "a"
    miss = code_action.ItemCoverage(
        status=code_action.CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
        item=path_to_resource_uri(project_a / "pyproject.toml"),
    )

    _, sender, ratchet = await _run(
        AuditCodeRunPayload(
            target=AuditCodeTarget.FILES,
            file_paths=[path_to_resource_uri(project_a / "mod.py")],
            project_paths=[path_to_resource_uri(project_a)],
        ),
        [project_a],
        unhandled=(miss,),
        ratchet=_FakeRatchet(
            stale=[
                StaleBaselineEntry(
                    contributor="check_imports",
                    snapshot_file=ResourceUri(
                        "file:///ws/finecode-diagnostics-baseline.json"
                    ),
                    file=ResourceUri("file:///ws/pyproject.toml"),
                    source="import-linter",
                    code="contract",
                    message="broken contract",
                    stale_count=1,
                )
            ],
        ),
    )

    assert [call.checked_project for call in ratchet.calls] == [project_a]
    call = ratchet.calls[0]
    assert (
        call.contributor == CheckImportsAuditCodeBridgeHandler.RATCHET_CONTRIBUTOR
    )
    assert call.scope == RatchetScope(
        target=RatchetTarget.FILES,
        files=(path_to_resource_uri(project_a / "mod.py"),),
    )
    assert call.unhandled == [miss]
    assert len(sender.results) == 1
    assert sender.results[0].stale == ratchet.stale
    assert sender.results[0].receipts == ratchet.receipts
