import asyncio
import dataclasses

from finecode_extension_api import code_action
from fine_lint import lint_action
from fine_git_hooks import precommit_action
from finecode_extension_api.interfaces import iworkspaceactionrunner, iworkspaceinfoprovider, ilogger
from finecode_extension_api.interfaces.iworkspaceinfoprovider import actionable_project_paths
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_api.workspace_utils import group_files_by_project


@dataclasses.dataclass
class LintPrecommitBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class LintPrecommitBridgeHandler(
    code_action.ActionHandler[
        precommit_action.PrecommitAction, LintPrecommitBridgeHandlerConfig
    ]
):
    """Bridge handler that runs lint on the staged files."""

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
            self.logger.info("No staged files - skipping lint.")
            return precommit_action.PrecommitRunResult()

        project_paths = actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())
        files_by_project = group_files_by_project(run_context.staged_files, project_paths)

        if not files_by_project:
            self.logger.warning(
                "Staged files do not belong to any workspace project - skipping lint."
            )
            return precommit_action.PrecommitRunResult()

        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(
                        self.workspace_action_runner.run_action_in_projects(
                            action_type=lint_action.LintAction,
                            payload=lint_action.LintRunPayload(
                                target=lint_action.LintTarget.FILES,
                                file_paths=[path_to_resource_uri(p) for p in project_files],
                            ),
                            meta=run_context.meta,
                            project_paths=[project_path],
                        )
                    )
                    for project_path, project_files in files_by_project.items()
                ]
        except ExceptionGroup as eg:
            errors = [getattr(exc, "message", str(exc)) for exc in eg.exceptions]
            raise code_action.ActionFailedException(
                "Lint failed:\n" + "\n".join(f"  - {e}" for e in errors)
            ) from eg

        merged_lint_result = lint_action.LintRunResult(messages={})
        for task in tasks:
            for project_result in task.result().values():
                merged_lint_result.update(project_result)

        return precommit_action.PrecommitRunResult(action_results={"lint": merged_lint_result})
