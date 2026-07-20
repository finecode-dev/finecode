import asyncio
import dataclasses

from finecode_extension_api import code_action
from fine_envs.check_toolchains_action import (
    CheckToolchainsAction,
    CheckToolchainsRunPayload,
    CheckToolchainsRunResult,
)
from fine_git_hooks import precommit_action
from finecode_extension_api.interfaces import iworkspaceactionrunner, iworkspaceinfoprovider, ilogger
from finecode_extension_api.interfaces.iworkspaceinfoprovider import actionable_project_paths
from finecode_extension_api.workspace_utils import group_files_by_project


@dataclasses.dataclass
class CheckToolchainsPrecommitBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class CheckToolchainsPrecommitBridgeHandler(
    code_action.ActionHandler[
        precommit_action.PrecommitAction, CheckToolchainsPrecommitBridgeHandlerConfig
    ]
):
    """Bridge handler that checks each touched project's toolchain axis for drift.

    Not registered by default (see preset.toml) — the check re-derives the axis,
    which runs a package-manager subprocess (e.g. `uv python list`), so running it
    on every commit is an opt-in choice. CI's `check_toolchains` step is the
    baseline safety net (ADR-0053); this bridge only moves the catch earlier for
    projects that want it.

    Unlike the file-based bridges, `check_toolchains` is project-level: it compares
    a project's materialized `interpreters` axis against what `requires-python` now
    derives, per ADR-0053. So this runs the action once per project that has any
    staged file, not per file, and passes no file target.
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
            self.logger.info("No staged files - skipping toolchain check.")
            return precommit_action.PrecommitRunResult()

        project_paths = actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())
        files_by_project = group_files_by_project(run_context.staged_files, project_paths)

        if not files_by_project:
            self.logger.warning(
                "Staged files do not belong to any workspace project - skipping toolchain check."
            )
            return precommit_action.PrecommitRunResult()

        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(
                        self.workspace_action_runner.run_action_in_projects(
                            action_type=CheckToolchainsAction,
                            payload=CheckToolchainsRunPayload(),
                            meta=run_context.meta,
                            project_paths=[project_path],
                        )
                    )
                    for project_path in files_by_project
                ]
        except ExceptionGroup as eg:
            errors = [getattr(exc, "message", str(exc)) for exc in eg.exceptions]
            raise code_action.ActionFailedException(
                "Toolchain check failed:\n" + "\n".join(f"  - {e}" for e in errors)
            ) from eg

        # Drift is signalled by CheckToolchainsRunResult.return_code (ERROR), not by
        # an exception, so we merge results and let PrecommitRunResult.return_code
        # propagate the failure.
        merged_result = CheckToolchainsRunResult()
        for task in tasks:
            for project_result in task.result().values():
                merged_result.update(project_result)

        return precommit_action.PrecommitRunResult(
            action_results={"check_toolchains": merged_result}
        )
