import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import create_envs_action
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class CreateEnvsDiscoverEnvsHandlerConfig(code_action.ActionHandlerConfig): ...


class CreateEnvsDiscoverEnvsHandler(
    code_action.ActionHandler[
        create_envs_action.CreateEnvsAction, CreateEnvsDiscoverEnvsHandlerConfig
    ]
):
    """Discover and populate run_context.envs from the current project's config.

    If payload.envs is already non-empty (explicit caller), those envs are
    used as-is — the caller is responsible for any filtering.
    Otherwise all envs defined in ``dependency-groups`` are discovered.
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
        payload: create_envs_action.CreateEnvsRunPayload,
        run_context: create_envs_action.CreateEnvsRunContext,
    ) -> create_envs_action.CreateEnvsRunResult:
        if payload.envs:
            envs = list(payload.envs)
        else:
            project_def_path = self.project_info_provider.get_current_project_def_path()
            project_raw_config = (
                await self.project_info_provider.get_current_project_raw_config()
            )
            deps_groups = project_raw_config.get("dependency-groups", {})

            envs = [
                create_envs_action.EnvInfo(
                    name=env_name,
                    venv_dir_path=self.runner_info_provider.get_venv_dir_path_of_env(
                        env_name
                    ),
                    project_def_path=project_def_path,
                )
                for env_name in deps_groups
            ]

        self.logger.debug(f"Discovered envs for creation: {[e.name for e in envs]}")
        run_context.envs = envs
        return create_envs_action.CreateEnvsRunResult(errors=[])
