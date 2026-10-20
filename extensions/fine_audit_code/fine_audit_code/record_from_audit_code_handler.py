import dataclasses
import pathlib

from fine_inspect_code._snapshot_writer import (
    read_snapshots_or_refuse,
    write_snapshot_text,
)
from fine_inspect_code.ratchet import IDiagnosticsRatchet
from fine_inspect_code.ratchet_record import apply_plan, plan_from_results
from fine_inspect_code.ratchet_snapshot import Snapshot, render_snapshot
from fine_inspect_code.record_diagnostics_baseline_action import (
    RecordDiagnosticsBaselineAction,
    RecordDiagnosticsBaselineRunContext,
    RecordDiagnosticsBaselineRunPayload,
    RecordDiagnosticsBaselineRunResult,
    RecordedSnapshot,
    RecordStatus,
    Umbrella,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ifilemanager,
    ilogger,
    iprojectinfoprovider,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_audit_code.audit_code_action import (
    AuditCodeAction,
    AuditCodeRunPayload,
    AuditCodeTarget,
)

__all__ = [
    "RecordFromAuditCodeHandler",
    "RecordFromAuditCodeHandlerConfig",
]


@dataclasses.dataclass
class RecordFromAuditCodeHandlerConfig(code_action.ActionHandlerConfig): ...


class RecordFromAuditCodeHandler(
    code_action.ActionHandler[
        RecordDiagnosticsBaselineAction, RecordFromAuditCodeHandlerConfig
    ]
):
    """Records the audit_code partitions of the snapshots.

    Registered by fine_audit_code onto the action fine_inspect_code defines,
    so a workspace without audit_code records inspect_code alone. Runs after
    the inspect handler (SEQUENTIAL) over disjoint partitions.
    """

    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        ratchet: IDiagnosticsRatchet,
        file_manager: ifilemanager.IFileManager,
        logger: ilogger.ILogger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.project_info_provider = project_info_provider
        self.ratchet = ratchet
        self.file_manager = file_manager
        self.logger = logger

    async def run(
        self,
        payload: RecordDiagnosticsBaselineRunPayload,
        run_context: RecordDiagnosticsBaselineRunContext,
    ) -> None:
        if payload.project_paths is not None:
            projects = [resource_uri_to_path(uri) for uri in payload.project_paths]
        else:
            projects = actionable_project_paths(
                await self.workspace_info_provider.get_workspace_projects()
            )
        workspace_root = self.project_info_provider.get_current_project_dir_path()
        projects = [
            project
            for project in projects
            if await self.workspace_info_provider.project_declares_action(
                project, "fine_audit_code.AuditCodeAction"
            )
        ]
        if not projects:
            await run_context.partial_result_sender.send(
                RecordDiagnosticsBaselineRunResult(snapshots=[])
            )
            return
        snapshot_paths = {
            project: self.ratchet.snapshot_path(project) for project in projects
        }
        parsed = await read_snapshots_or_refuse(
            self.file_manager, snapshot_paths.values(), "record"
        )
        snapshots_by_project: dict[pathlib.Path, Snapshot | None] = {
            project: parsed[path] for project, path in snapshot_paths.items()
        }
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=AuditCodeAction,
            payload=AuditCodeRunPayload(
                target=AuditCodeTarget.PROJECT,
                project_paths=[path_to_resource_uri(project) for project in projects],
            ),
            meta=run_context.meta,
            project_paths=[workspace_root],
        )
        plan = plan_from_results(
            list(results.values()), projects, snapshots_by_project.get
        )
        updated = apply_plan(plan, snapshots_by_project)
        rows: list[RecordedSnapshot] = []
        for project in projects:
            path = self.ratchet.snapshot_path(project)
            text = render_snapshot(updated[project])
            non_empty = any(
                count > 0
                for key, counts in plan.counts.items()
                if key.project == project
                for count in counts.values()
            )
            if snapshots_by_project[project] is None and not non_empty:
                status = RecordStatus.SKIPPED
            elif await write_snapshot_text(path, text):
                status = RecordStatus.WRITTEN
            else:
                status = RecordStatus.UNCHANGED
            rows.append(
                RecordedSnapshot(
                    project=path_to_resource_uri(project),
                    umbrella=Umbrella.AUDIT_CODE,
                    snapshot_file=path_to_resource_uri(path),
                    platform=plan.platform,
                    status=status,
                    partitions=len(
                        [key for key in plan.counts if key.project == project]
                    ),
                )
            )
        await run_context.partial_result_sender.send(
            RecordDiagnosticsBaselineRunResult(snapshots=rows)
        )
