import asyncio
import dataclasses
import pathlib

from finecode_extension_api import code_action
from fine_audit_code.audit_code_action import (
    AuditCodeAction,
    AuditCodeRunPayload,
    AuditCodeRunContext,
    AuditCodeRunResult,
)
from fine_check_imports.check_imports_action import CheckImportsAction, CheckImportsRunPayload
from finecode_extension_api.interfaces import iworkspaceactionrunner, iworkspaceinfoprovider, ilogger
from finecode_extension_api.interfaces.iworkspaceinfoprovider import actionable_project_paths
from finecode_extension_api.resource_uri import resource_uri_to_path


@dataclasses.dataclass
class CheckImportsAuditCodeBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class CheckImportsAuditCodeBridgeHandler(
    code_action.ActionHandler[AuditCodeAction, CheckImportsAuditCodeBridgeHandlerConfig]
):
    """Bridge handler that runs check_imports when audit_code is invoked.

    Unlike the lint/type_check bridges onto inspect_code, this bridge ignores
    ``payload.target``/``payload.file_paths``: check_imports always analyzes a
    project's whole import graph (see CheckImportsAction docstring), so there
    is no meaningful per-file scoping to forward. Every requested project is
    checked in full regardless of target.
    """

    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.logger = logger

    async def _run_check_imports_for_project(
        self,
        project_path: pathlib.Path,
        run_meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        self.logger.debug(
            f"CheckImportsAuditCodeBridgeHandler: running CheckImportsAction for project={project_path}"
        )
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=CheckImportsAction,
            payload=CheckImportsRunPayload(),
            meta=run_meta,
            project_paths=[project_path],
        )
        for proj_path, result in results.items():
            await partial_result_sender.send(AuditCodeRunResult(messages=result.messages))

    async def run(
        self,
        payload: AuditCodeRunPayload,
        run_context: AuditCodeRunContext,
    ) -> None:
        if payload.project_paths is not None:
            project_paths = [resource_uri_to_path(uri) for uri in payload.project_paths]
        else:
            project_paths = actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())

        async with asyncio.TaskGroup() as tg:
            for project_path in project_paths:
                tg.create_task(
                    self._run_check_imports_for_project(
                        project_path,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
