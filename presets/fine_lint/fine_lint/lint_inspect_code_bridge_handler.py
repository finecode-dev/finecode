import asyncio
import dataclasses
import pathlib

from finecode_extension_api import code_action
from fine_inspect_code.inspect_code_action import (
    InspectCodeAction,
    InspectCodeRunPayload,
    InspectCodeRunContext,
    InspectCodeRunResult,
)
from fine_lint.lint_action import LintAction, LintRunPayload, LintTarget
from finecode_extension_api.interfaces import iworkspaceactionrunner, iworkspaceinfoprovider, ilogger
from finecode_extension_api.interfaces.iworkspaceinfoprovider import actionable_project_paths
from finecode_extension_api.resource_uri import resource_uri_to_path


@dataclasses.dataclass
class LintInspectCodeBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class LintInspectCodeBridgeHandler(
    code_action.ActionHandler[InspectCodeAction, LintInspectCodeBridgeHandlerConfig]
):
    """Bridge handler that runs lint when inspect_code is invoked."""

    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.logger = logger

    async def _run_lint_for_project(
        self,
        project_path: pathlib.Path,
        payload: InspectCodeRunPayload,
        run_meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=LintAction,
            payload=LintRunPayload(
                target=LintTarget(payload.target.value),
                file_paths=payload.file_paths,
                project_paths=payload.project_paths,
            ),
            meta=run_meta,
            project_paths=[project_path],
        )
        for result in results.values():
            await partial_result_sender.send(InspectCodeRunResult(messages=result.messages))

    async def run(
        self,
        payload: InspectCodeRunPayload,
        run_context: InspectCodeRunContext,
    ) -> None:
        project_paths = (
            [resource_uri_to_path(uri) for uri in payload.project_paths]
            if payload.project_paths is not None
            else actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())
        )

        async with asyncio.TaskGroup() as tg:
            for project_path in project_paths:
                tg.create_task(
                    self._run_lint_for_project(
                        project_path,
                        payload,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
