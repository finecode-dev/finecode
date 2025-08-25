import dataclasses
import shutil

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_envs as prepare_envs_action
from finecode_extension_api.interfaces import (
    iactionrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_runner.action_handlers import dependency_config_utils


@dataclasses.dataclass
class PrepareEnvsReadConfigsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareEnvsReadConfigsHandler(
    code_action.ActionHandler[
        prepare_envs_action.PrepareEnvsAction, PrepareEnvsReadConfigsHandlerConfig
    ]
):
    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: prepare_envs_action.PrepareEnvsRunPayload,
        run_context: prepare_envs_action.PrepareEnvsRunContext,
    ) -> prepare_envs_action.PrepareEnvsRunResult:
        project_defs_pathes = set(
            [env_info.project_def_path for env_info in payload.envs]
        )
        if len(project_defs_pathes) != 1:
            raise code_action.ActionFailedException("PrepareEnvsReadConfigsHandler supports only reading config of envs from the current project")

        project_raw_config = await self.project_info_provider.get_project_raw_config()

        project_def_path = project_defs_pathes.pop()
        project_dir_path = project_def_path.parent

        dependency_config_utils.make_project_config_pip_compatible(
            project_raw_config, project_def_path
        )

        for env_info in payload.envs:
            run_context.project_def_path_by_venv_dir_path[env_info.venv_dir_path] = (
                project_def_path
            )
            run_context.project_def_by_venv_dir_path[env_info.venv_dir_path] = (
                project_raw_config
            )

        return prepare_envs_action.PrepareEnvsRunResult(errors=[])
