import dataclasses

from fine_lint import lint_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_api.workspace_utils import group_files_by_project

from fine_git_hooks import precommit_action


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

        project_paths = actionable_project_paths(
            await self.workspace_info_provider.get_workspace_projects()
        )
        files_by_project = group_files_by_project(
            run_context.staged_files, project_paths
        )

        if not files_by_project:
            self.logger.warning(
                "Staged files do not belong to any workspace project - skipping lint."
            )
            return precommit_action.PrecommitRunResult()

        payload_by_project = {
            project_path: lint_action.LintRunPayload(
                target=lint_action.LintTarget.FILES,
                file_paths=[path_to_resource_uri(p) for p in project_files],
            )
            for project_path, project_files in files_by_project.items()
        }

        try:
            results = await self.workspace_action_runner.run_action_per_project(
                action_type=lint_action.LintAction,
                payload_by_project=payload_by_project,
                meta=run_context.meta,
            )
        except iprojectactionrunner.ActionRunFailed as exc:
            raise code_action.ActionFailedException(
                "Lint failed:\n  - " + exc.message
            ) from exc

        merged_lint_result = lint_action.LintRunResult(messages={})
        for project_result in results.values():
            merged_lint_result.update(project_result)

        return precommit_action.PrecommitRunResult(
            action_results={"lint": merged_lint_result}
        )
