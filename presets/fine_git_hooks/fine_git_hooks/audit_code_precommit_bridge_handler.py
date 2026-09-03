import dataclasses

from fine_audit_code.audit_code_action import (
    AuditCodeAction,
    AuditCodeRunPayload,
    AuditCodeTarget,
)
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
class AuditCodePrecommitBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class AuditCodePrecommitBridgeHandler(
    code_action.ActionHandler[
        precommit_action.PrecommitAction, AuditCodePrecommitBridgeHandlerConfig
    ]
):
    """Bridge handler that runs audit_code on the staged files.

    Not registered by default (see preset.toml) — audit_code's handlers may be
    whole-project and slow (see ADR-0044), so running it on every commit is an
    opt-in choice for projects that want the thorough set at commit time, not
    a default alongside the fast lint/format bridges.
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
            self.logger.info("No staged files - skipping audit_code.")
            return precommit_action.PrecommitRunResult()

        project_paths = actionable_project_paths(
            await self.workspace_info_provider.get_workspace_projects()
        )
        files_by_project = group_files_by_project(
            run_context.staged_files, project_paths
        )

        if not files_by_project:
            self.logger.warning(
                "Staged files do not belong to any workspace project - skipping audit_code."
            )
            return precommit_action.PrecommitRunResult()

        try:
            results = await self.workspace_action_runner.run_action_per_project(
                action_type=AuditCodeAction,
                payload_by_project={
                    project_path: AuditCodeRunPayload(
                        target=AuditCodeTarget.FILES,
                        file_paths=[
                            path_to_resource_uri(p) for p in project_files
                        ],
                    )
                    for project_path, project_files in files_by_project.items()
                },
                meta=run_context.meta,
            )
        except iprojectactionrunner.ActionRunFailed as exc:
            raise code_action.ActionFailedException(
                "Audit code failed:\n  - " + exc.message
            ) from exc

        from fine_audit_code.audit_code_action import AuditCodeRunResult

        merged_result = AuditCodeRunResult(messages={})
        for project_result in results.values():
            merged_result.update(project_result)

        return precommit_action.PrecommitRunResult(
            action_results={"audit_code": merged_result}
        )
