import asyncio
import dataclasses
import os
import pathlib

from fine_envs.check_toolchains_action import (
    CheckToolchainsAction,
    CheckToolchainsRunPayload,
    CheckToolchainsRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.workspace_utils import group_files_by_project

from fine_git_hooks import precommit_action


def _project_label(project_path: pathlib.Path) -> str:
    """Short, unique identification of a project for a report header.

    Relative to the working directory (precommit runs from the git root, per
    ADR-0031, so that reads as ``extensions/fine_python_uv``). Workspace projects are
    always nested under the workspace root, so this is always on the same drive as
    the cwd and `relpath` cannot raise.
    """
    return os.path.relpath(project_path)


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

        project_paths = actionable_project_paths(
            await self.workspace_info_provider.get_workspace_projects()
        )
        files_by_project = group_files_by_project(
            run_context.staged_files, project_paths
        )

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

        results_by_project: dict[pathlib.Path, CheckToolchainsRunResult] = {}
        for task in tasks:
            results_by_project.update(task.result())

        # Drift is signalled by CheckToolchainsRunResult.return_code (ERROR), not by an
        # exception, so the failure propagates through PrecommitRunResult.return_code,
        # which already fails if any entry does.
        #
        # The per-project results are deliberately NOT merged with update(). Per R-302
        # that method is a within-project merger, and `EnvToolchainAxis` is keyed by env
        # name -- unique inside a project, not across them. Merging would collapse two
        # projects' stale `testing` axes into one entry showing the first project's
        # versions for both. Cross-project aggregation belongs to callers above the
        # action layer (R-302), so it happens here, and it keeps the projects apart:
        # one action_results entry each, rendered under its own header.
        stale_by_project = {
            project_path: result
            for project_path, result in results_by_project.items()
            if result.stale_axes
        }
        if not stale_by_project:
            return precommit_action.PrecommitRunResult(
                action_results={"check_toolchains": CheckToolchainsRunResult()}
            )

        return precommit_action.PrecommitRunResult(
            action_results={
                f"check_toolchains ({_project_label(project_path)})": result
                for project_path, result in stale_by_project.items()
            }
        )
