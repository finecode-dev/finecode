# docs: docs/reference/actions.md
import asyncio
import dataclasses
import pathlib

from fine_audit_code.audit_code_action import (
    AuditCodeAction,
    AuditCodeRunContext,
    AuditCodeRunPayload,
    AuditCodeRunResult,
    AuditCodeTarget,
)
from fine_inspect_code.diagnostic_types import (
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iuser_messenger,
    iworkspaceactionrunner,
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
from finecode_extension_api.workspace_utils import group_files_by_project

from fine_envs.check_toolchains_action import (
    CheckToolchainsAction,
    CheckToolchainsRunPayload,
    CheckToolchainsRunResult,
)

_DIAGNOSTIC_SOURCE = "check_toolchains"
_DIAGNOSTIC_CODE = "stale-toolchain-axis"


def _axis_message(env_name: str, declared: list[str], derived: list[str]) -> str:
    return (
        f"Env '{env_name}': toolchain axis is out of date."
        f" Declared: {', '.join(declared) or '(none)'}."
        f" Derived: {', '.join(derived) or '(none)'}."
        " Run `sync_toolchains` to update."
    )


@dataclasses.dataclass
class CheckToolchainsAuditCodeBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class CheckToolchainsAuditCodeBridgeHandler(
    code_action.ActionHandler[
        AuditCodeAction, CheckToolchainsAuditCodeBridgeHandlerConfig
    ]
):
    """Bridge handler that reports toolchain axis drift when audit_code is invoked.

    A materialized toolchain axis (ADR-0053) goes stale the way a lock file does, and audit_code is the umbrella for catching that.

    Like the check_imports bridge, this one is project-level, so `payload.file_paths`
    never scopes *what* is checked. It does scope *which projects* are checked: with
    `target="files"` only the projects owning those files are checked, rather than every
    project in the workspace.

    `CheckToolchainsRunResult` is structured (declared/derived per env) while audit_code
    speaks diagnostics, so this handler flattens: one ERROR diagnostic per stale env,
    anchored at the project definition file that declares the axis, at position (0, 0)
    — the drift belongs to the file, not to a line in it. That matches how import-linter
    anchors whole-project contract violations at the config file.
    """

    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        user_messenger: iuser_messenger.IUserMessenger,
        logger: ilogger.ILogger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.user_messenger = user_messenger
        self.logger = logger

    def _report_no_targets(self, message: str, meta: code_action.RunActionMeta) -> None:
        # R-505: zero targets is only diagnosable when the caller asked for specific
        # items. Editors fire audit_code with trigger=SYSTEM for buffers outside any
        # known project, where zero targets is expected rather than a problem.
        if meta.trigger == code_action.RunActionTrigger.USER:
            self.user_messenger.warning(message)
        else:
            self.logger.debug(message)

    async def _target_project_paths(
        self, payload: AuditCodeRunPayload, meta: code_action.RunActionMeta
    ) -> list[pathlib.Path]:
        if payload.project_paths is not None:
            return [resource_uri_to_path(uri) for uri in payload.project_paths]

        if payload.target == AuditCodeTarget.FILES:
            # R-309: "no files requested" is decided here, before any fan-out, not
            # delegated to each project's ER.
            if not payload.file_paths:
                return []

            file_paths = [resource_uri_to_path(uri) for uri in payload.file_paths]
            files_by_project = group_files_by_project(
                file_paths,
                actionable_project_paths(
                    await self.workspace_info_provider.get_workspace_projects()
                ),
            )
            if not files_by_project:
                self._report_no_targets(
                    "Toolchain check skipped: requested files belong to no known"
                    f" workspace project: {', '.join(str(p) for p in file_paths)}",
                    meta,
                )
                return []
            return list(files_by_project)

        return actionable_project_paths(
            await self.workspace_info_provider.get_workspace_projects()
        )

    def _to_messages(
        self, project_path: pathlib.Path, result: CheckToolchainsRunResult
    ) -> dict[ResourceUri, list[Diagnostic]]:
        target_uri = result.project_def_path
        if target_uri is None:
            # fine_envs' own handler always reports it; a third-party one may not.
            # Anchoring at the project directory keeps the finding visible — dropping it
            # would hide a failure — and the warning says why the location is coarse.
            self.logger.warning(
                f"check_toolchains result for {project_path} carries no"
                " project_def_path; anchoring diagnostics at the project directory"
            )
            target_uri = path_to_resource_uri(project_path)

        # An empty list rather than an absent key: it renders as "<path>: OK" and tells
        # callers the project was checked and is clean, which absence cannot.
        return {
            target_uri: [
                Diagnostic(
                    range=Range(
                        start=Position(line=0, character=0),
                        end=Position(line=0, character=0),
                    ),
                    message=_axis_message(axis.env_name, axis.declared, axis.derived),
                    source=_DIAGNOSTIC_SOURCE,
                    code=_DIAGNOSTIC_CODE,
                    severity=DiagnosticSeverity.ERROR,
                )
                for axis in result.stale_axes
            ]
        }

    async def _check_project(
        self,
        project_path: pathlib.Path,
        run_meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        self.logger.debug(
            "CheckToolchainsAuditCodeBridgeHandler: running CheckToolchainsAction for"
            f" project={project_path}"
        )
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=CheckToolchainsAction,
            payload=CheckToolchainsRunPayload(),
            meta=run_meta,
            project_paths=[project_path],
        )
        for result_project_path, result in results.items():
            await partial_result_sender.send(
                AuditCodeRunResult(
                    messages=self._to_messages(result_project_path, result)
                )
            )

    async def run(
        self,
        payload: AuditCodeRunPayload,
        run_context: AuditCodeRunContext,
    ) -> None:
        project_paths = await self._target_project_paths(payload, run_context.meta)
        if not project_paths:
            return

        # One task per project, and the per-project results are never merged with
        # CheckToolchainsRunResult.update(): its axes are keyed by env name, unique
        # inside a project but not across them (R-302). Keying the diagnostics by each
        # project's own definition file is what keeps them apart here.
        async with asyncio.TaskGroup() as tg:
            for project_path in project_paths:
                tg.create_task(
                    self._check_project(
                        project_path,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
