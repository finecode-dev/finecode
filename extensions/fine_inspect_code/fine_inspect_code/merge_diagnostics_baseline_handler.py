import asyncio
import dataclasses
import json
import pathlib

import cattrs
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ifilemanager,
    ilogger,
    iprojectinfoprovider,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_inspect_code._snapshot_writer import (
    read_snapshots_or_refuse,
    write_snapshot_text,
)
from fine_inspect_code.diagnostic_types import DiagnosticFilesRunResult
from fine_inspect_code.inspect_code_action import InspectCodeRunResult
from fine_inspect_code.merge_diagnostics_baseline_action import (
    MergeDiagnosticsBaselineAction,
    MergeDiagnosticsBaselineRunContext,
    MergeDiagnosticsBaselineRunPayload,
    MergeDiagnosticsBaselineRunResult,
)
from fine_inspect_code.ratchet import IDiagnosticsRatchet
from fine_inspect_code.ratchet_record import (
    RecordRefusedError,
    apply_plan,
    plan_from_results,
)
from fine_inspect_code.ratchet_snapshot import Snapshot, render_snapshot
from fine_inspect_code.record_diagnostics_baseline_action import (
    RecordedSnapshot,
    RecordStatus,
    Umbrella,
)

__all__ = [
    "MergeDiagnosticsBaselineHandler",
    "MergeDiagnosticsBaselineHandlerConfig",
]

_UMBRELLA_BY_SOURCE = {
    "fine_inspect_code.InspectCodeAction": Umbrella.INSPECT_CODE,
    "fine_audit_code.AuditCodeAction": Umbrella.AUDIT_CODE,
}


@dataclasses.dataclass
class MergeDiagnosticsBaselineHandlerConfig(code_action.ActionHandlerConfig): ...


class MergeDiagnosticsBaselineHandler(
    code_action.ActionHandler[
        MergeDiagnosticsBaselineAction, MergeDiagnosticsBaselineHandlerConfig
    ]
):
    """Derives platform counts from CI results files without running analysis."""

    def __init__(
        self,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        ratchet: IDiagnosticsRatchet,
        file_manager: ifilemanager.IFileManager,
        logger: ilogger.ILogger,
    ) -> None:
        self.workspace_info_provider = workspace_info_provider
        self.project_info_provider = project_info_provider
        self.ratchet = ratchet
        self.file_manager = file_manager
        self.logger = logger

    async def run(
        self,
        payload: MergeDiagnosticsBaselineRunPayload,
        run_context: MergeDiagnosticsBaselineRunContext,
    ) -> None:
        local_projects = actionable_project_paths(
            await self.workspace_info_provider.get_workspace_projects()
        )
        local_root = self.project_info_provider.get_current_project_dir_path()
        converter = cattrs.Converter()
        results: list[DiagnosticFilesRunResult] = []
        by_umbrella: dict[Umbrella, tuple[set[str], set[pathlib.Path]]] = {}
        for uri in payload.results_files:
            for umbrella, result in await self._load_results_file(
                uri, converter, local_root, local_projects
            ):
                results.append(result)
                known = by_umbrella.get(umbrella)
                if known is None:
                    by_umbrella[umbrella] = (set(), set())
                    known = by_umbrella[umbrella]
                known[0].update(
                    receipt.contributor for receipt in result.receipts
                )
                known[1].update(
                    resource_uri_to_path(receipt.project)
                    for receipt in result.receipts
                )
        def _receipted(project: pathlib.Path) -> bool:
            return any(
                project in receipt_projects
                for _, receipt_projects in by_umbrella.values()
            )

        if payload.project_paths is not None:
            wanted = {resource_uri_to_path(uri) for uri in payload.project_paths}
            targeted = [
                project
                for project in local_projects
                if project in wanted and _receipted(project)
            ]
        else:
            targeted = [
                project for project in local_projects if _receipted(project)
            ]
        snapshot_paths = {
            project: self.ratchet.snapshot_path(project) for project in targeted
        }
        parsed = await read_snapshots_or_refuse(
            self.file_manager, snapshot_paths.values(), "merge"
        )
        snapshots_by_project: dict[pathlib.Path, Snapshot | None] = {
            project: parsed[path] for project, path in snapshot_paths.items()
        }
        plan = plan_from_results(results, local_projects, snapshots_by_project.get)
        updated = apply_plan(plan, snapshots_by_project)
        rows: list[RecordedSnapshot] = []
        for project in targeted:
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
            for umbrella, (contributors, receipt_projects) in by_umbrella.items():
                if project not in receipt_projects:
                    continue
                rows.append(
                    RecordedSnapshot(
                        project=path_to_resource_uri(project),
                        umbrella=umbrella,
                        snapshot_file=path_to_resource_uri(path),
                        platform=plan.platform,
                        status=status,
                        partitions=len(
                            [
                                key
                                for key in plan.counts
                                if key.project == project
                                and key.contributor in contributors
                            ]
                        ),
                    )
                )
        await run_context.partial_result_sender.send(
            MergeDiagnosticsBaselineRunResult(snapshots=rows)
        )

    async def _load_results_file(
        self,
        uri: ResourceUri,
        converter: cattrs.Converter,
        local_root: pathlib.Path,
        local_projects: list[pathlib.Path],
    ) -> list[tuple[Umbrella, DiagnosticFilesRunResult]]:
        try:
            raw = await asyncio.to_thread(
                resource_uri_to_path(uri).read_bytes
            )
        except (OSError, ValueError) as error:
            raise RecordRefusedError(
                f"cannot merge diagnostics baseline from {uri}: {error}"
            ) from error
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise RecordRefusedError(
                f"cannot merge diagnostics baseline from {uri}: {error}"
            ) from error
        if not isinstance(data, dict):
            raise RecordRefusedError(
                f"cannot merge diagnostics baseline from {uri}: "
                "not a results file"
            )
        actions = data.get("actions")
        if not isinstance(actions, dict) or not actions:
            raise RecordRefusedError(
                f"cannot merge diagnostics baseline from {uri}: "
                "no action results (a failed run records nothing)"
            )
        file_results: list[tuple[Umbrella, DiagnosticFilesRunResult]] = []
        for source, entry in actions.items():
            umbrella = _UMBRELLA_BY_SOURCE.get(source)
            if umbrella is None:
                raise RecordRefusedError(
                    f"cannot merge diagnostics baseline from {uri}: "
                    f"unknown action {source!r}"
                )
            if not isinstance(entry, dict) or entry.get("scope") != "workspace":
                raise RecordRefusedError(
                    f"cannot merge diagnostics baseline from {uri}: "
                    f"action {source!r} is not workspace-scoped"
                )
            entries = entry.get("results")
            if not isinstance(entries, dict) or len(entries) != 1:
                raise RecordRefusedError(
                    f"cannot merge diagnostics baseline from {uri}: "
                    f"action {source!r} names no single hosting root"
                )
            source_root = pathlib.Path(next(iter(entries)))
            for project_entry in entries.values():
                if not isinstance(project_entry, dict):
                    raise RecordRefusedError(
                        f"cannot merge diagnostics baseline from {uri}: "
                        f"action {source!r} has a malformed project entry"
                    )
                result_data = project_entry.get("result")
                if result_data is None:
                    continue
                try:
                    result = converter.structure(result_data, InspectCodeRunResult)
                except (
                    cattrs.ClassValidationError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                ) as error:
                    raise RecordRefusedError(
                        f"cannot merge diagnostics baseline from {uri}: "
                        f"action {source!r} has an unreadable result ({error})"
                    ) from error
                file_results.append(
                    (
                        umbrella,
                        self._rebase_result(
                            result, uri, source_root, local_root, set(local_projects)
                        ),
                    )
                )
        if not any(result.receipts for _, result in file_results):
            raise RecordRefusedError(
                f"cannot merge diagnostics baseline from {uri}: "
                "no receipt maps to a local project"
            )
        return file_results

    def _rebase_result(
        self,
        result: InspectCodeRunResult,
        uri: ResourceUri,
        source_root: pathlib.Path,
        local_root: pathlib.Path,
        local_projects: set[pathlib.Path],
    ) -> InspectCodeRunResult:
        messages = {
            self._rebase_uri(key, uri, source_root, local_root): diagnostics
            for key, diagnostics in result.messages.items()
        }
        receipts = []
        for receipt in result.receipts:
            try:
                project = resource_uri_to_path(receipt.project)
            except ValueError as error:
                raise RecordRefusedError(
                    f"cannot merge diagnostics baseline from {uri}: "
                    f"receipt {receipt!r} names no local project"
                ) from error
            if not project.is_relative_to(source_root):
                raise RecordRefusedError(
                    f"cannot merge diagnostics baseline from {uri}: "
                    f"receipt {receipt!r} is outside the source root"
                )
            rebased = local_root / project.relative_to(source_root)
            if rebased not in local_projects:
                raise RecordRefusedError(
                    f"cannot merge diagnostics baseline from {uri}: "
                    f"receipt {receipt!r} names unknown local project {rebased}"
                )
            receipts.append(
                dataclasses.replace(
                    receipt,
                    project=ResourceUri(rebased.as_uri()),
                    unjudged=tuple(
                        self._rebase_uri(item, uri, source_root, local_root)
                        for item in receipt.unjudged
                    ),
                )
            )
        return dataclasses.replace(result, messages=messages, receipts=receipts)

    def _rebase_uri(
        self,
        uri: ResourceUri,
        file_uri: ResourceUri,
        source_root: pathlib.Path,
        local_root: pathlib.Path,
    ) -> ResourceUri:
        if not uri.startswith("file://"):
            return uri
        try:
            path = resource_uri_to_path(uri)
        except ValueError as error:
            raise RecordRefusedError(
                f"cannot merge diagnostics baseline from {file_uri}: "
                f"URI {uri!r} is not a local path"
            ) from error
        if not path.is_relative_to(source_root):
            raise RecordRefusedError(
                f"cannot merge diagnostics baseline from {file_uri}: "
                f"URI {uri!r} is outside the source root"
            )
        return ResourceUri((local_root / path.relative_to(source_root)).as_uri())
