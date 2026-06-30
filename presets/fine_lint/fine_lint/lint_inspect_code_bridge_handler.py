import asyncio
import dataclasses
import pathlib

from loguru import logger as loguru_logger
from finecode_extension_api import code_action
from fine_inspect_code.inspect_code_action import (
    InspectCodeAction,
    InspectCodeRunPayload,
    InspectCodeRunContext,
    InspectCodeRunResult,
    InspectCodeTarget,
)
from fine_lint.lint_action import LintAction, LintRunPayload, LintTarget
from finecode_extension_api.interfaces import iworkspaceactionrunner, iworkspaceinfoprovider, ilogger, iuser_messenger
from finecode_extension_api.interfaces.iworkspaceinfoprovider import actionable_project_paths
from finecode_extension_api.resource_uri import resource_uri_to_path, path_to_resource_uri
from finecode_extension_api.workspace_utils import group_files_by_project


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
        user_messenger: iuser_messenger.IUserMessenger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.logger = logger
        self.user_messenger = user_messenger

    async def _run_lint_for_project(
        self,
        project_path: pathlib.Path,
        payload: InspectCodeRunPayload,
        run_meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        self.logger.debug(
            f"LintInspectCodeBridgeHandler: running LintAction for project={project_path}"
            f" file_paths={payload.file_paths}"
        )
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
        loguru_logger.debug(
            f"LintInspectCodeBridgeHandler: LintAction returned {len(results)} project results"
            f" projects={list(results.keys())}"
        )
        for proj_path, result in results.items():
            loguru_logger.debug(
                f"LintInspectCodeBridgeHandler: sending partial result for proj_path={proj_path}"
                f" messages keys={list(result.messages.keys())}"
            )
            await partial_result_sender.send(InspectCodeRunResult(messages=result.messages))

    async def run(
        self,
        payload: InspectCodeRunPayload,
        run_context: InspectCodeRunContext,
    ) -> None:
        if payload.project_paths is not None:
            project_paths = [resource_uri_to_path(uri) for uri in payload.project_paths]
        else:
            project_paths = actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())

        if payload.target == InspectCodeTarget.FILES:
            if not payload.file_paths:
                return
            file_abs_paths = [resource_uri_to_path(uri) for uri in payload.file_paths]
            project_to_files = group_files_by_project(file_abs_paths, project_paths)
            tasks = [
                (project_path, dataclasses.replace(payload, file_paths=[path_to_resource_uri(f) for f in files]))
                for project_path, files in project_to_files.items()
            ]
            if not tasks:
                self.user_messenger.warning(
                    f"LintInspectCodeBridgeHandler: none of the requested files matched a known "
                    f"project — no lint will run. file_paths={payload.file_paths}"
                )
        else:
            tasks = [(project_path, payload) for project_path in project_paths]
        async with asyncio.TaskGroup() as tg:
            for project_path, project_payload in tasks:
                tg.create_task(
                    self._run_lint_for_project(
                        project_path,
                        project_payload,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
