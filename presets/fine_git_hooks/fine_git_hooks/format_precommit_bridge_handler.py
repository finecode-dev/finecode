import asyncio
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from fine_git_hooks import precommit_action
from fine_format import format_action
from finecode_extension_api.interfaces import iworkspaceactionrunner, iworkspaceinfoprovider, ilogger
from finecode_extension_api.interfaces.iworkspaceinfoprovider import actionable_project_paths
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_api.workspace_utils import group_files_by_project


@dataclasses.dataclass
class _FormatCheckResult(code_action.RunActionResult):
    files_needing_format: list[ResourceUri] = dataclasses.field(default_factory=list)

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if self.files_needing_format:
            for file_uri in self.files_needing_format:
                text.append_styled(str(file_uri), bold=True)
                text.append(": needs formatting\n")
        else:
            text.append("All files formatted correctly.\n")
        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        if self.files_needing_format:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


@dataclasses.dataclass
class FormatPrecommitBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class FormatPrecommitBridgeHandler(
    code_action.ActionHandler[
        precommit_action.PrecommitAction, FormatPrecommitBridgeHandlerConfig
    ]
):
    """Bridge handler that checks formatting of staged files without modifying them."""

    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.logger = logger

    async def run(
        self,
        payload: precommit_action.PrecommitRunPayload,
        run_context: precommit_action.PrecommitRunContext,
    ) -> precommit_action.PrecommitRunResult:
        if run_context.staged_files is None:
            raise code_action.ActionFailedException(
                "discovery handler must be registered before bridge handlers"
            )
        if not run_context.staged_files:
            self.logger.info("No staged files - skipping format check.")
            return precommit_action.PrecommitRunResult()

        project_paths = actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())
        files_by_project = group_files_by_project(run_context.staged_files, project_paths)

        if not files_by_project:
            self.logger.warning(
                "Staged files do not belong to any workspace project - skipping format check."
            )
            return precommit_action.PrecommitRunResult()

        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(
                    self.workspace_action_runner.run_action_in_projects(
                        action_type=format_action.FormatAction,
                        payload=format_action.FormatRunPayload(
                            target=format_action.FormatTarget.FILES,
                            file_paths=[path_to_resource_uri(p) for p in project_files],
                            save=False,
                        ),
                        meta=run_context.meta,
                        project_paths=[project_path],
                    )
                )
                for project_path, project_files in files_by_project.items()
            ]

        merged_format_result = format_action.FormatRunResult(result_by_file_path={})
        for task in tasks:
            for project_result in task.result().values():
                merged_format_result.update(project_result)

        files_needing_format = [
            file_uri
            for file_uri, file_result in merged_format_result.result_by_file_path.items()
            if file_result.changed
        ]
        if files_needing_format:
            self.logger.info(
                f"{len(files_needing_format)} file(s) need formatting: "
                + ", ".join(str(f) for f in files_needing_format)
            )
        check_result = _FormatCheckResult(files_needing_format=files_needing_format)
        return precommit_action.PrecommitRunResult(
            action_results={"format": check_result}
        )
