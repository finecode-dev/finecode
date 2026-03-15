import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    prepare_runner_envs as prepare_runner_envs_action,
)
from finecode_extension_api.actions.create_envs import EnvInfo
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class PrepareRunnerEnvsDiscoverEnvsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareRunnerEnvsDiscoverEnvsHandler(
    code_action.ActionHandler[
        prepare_runner_envs_action.PrepareRunnerEnvsAction,
        PrepareRunnerEnvsDiscoverEnvsHandlerConfig,
    ]
):
    """Discover and populate run_context.envs from the current project's config.

    Every dependency group defined in the project definition is included as an
    env.
    """

    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.project_info_provider = project_info_provider
        self.runner_info_provider = runner_info_provider
        self.logger = logger

    async def run(
        self,
        payload: prepare_runner_envs_action.PrepareRunnerEnvsRunPayload,
        run_context: prepare_runner_envs_action.PrepareRunnerEnvsRunContext,
    ) -> prepare_runner_envs_action.PrepareRunnerEnvsRunResult:
        project_def_path = self.project_info_provider.get_current_project_def_path()
        project_raw_config = (
            await self.project_info_provider.get_current_project_raw_config()
        )
        deps_groups = project_raw_config.get("dependency-groups", {})

        envs = [
            EnvInfo(
                name=env_name,
                venv_dir_path=self.runner_info_provider.get_venv_dir_path_of_env(
                    env_name
                ),
                project_def_path=project_def_path,
            )
            for env_name in deps_groups
        ]

        self.logger.debug(f"Discovered runner envs: {[e.name for e in envs]}")
        run_context.envs = envs
        return prepare_runner_envs_action.PrepareRunnerEnvsRunResult(errors=[])
