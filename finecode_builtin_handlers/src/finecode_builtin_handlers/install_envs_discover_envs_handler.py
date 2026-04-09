import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import install_envs_action
from finecode_extension_api.actions.environments.create_envs_action import EnvInfo
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri


@dataclasses.dataclass
class InstallEnvsDiscoverEnvsHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallEnvsDiscoverEnvsHandler(
    code_action.ActionHandler[
        install_envs_action.InstallEnvsAction,
        InstallEnvsDiscoverEnvsHandlerConfig,
    ]
):
    """Discover and populate run_context.envs from the current project's config.

    If payload.envs is provided (explicit caller), those envs are
    used as-is — the caller is responsible for any filtering.
    Otherwise envs are discovered from dependency-groups: every dependency group
    defined in the project definition is included as an env.
    payload.env_names filters the discovered list."""

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
        payload: install_envs_action.InstallEnvsRunPayload,
        run_context: install_envs_action.InstallEnvsRunContext,
    ) -> install_envs_action.InstallEnvsRunResult:
        if payload.envs is not None:
            envs = list(payload.envs)
        else:
            project_def_path = self.project_info_provider.get_current_project_def_path()
            project_raw_config = (
                await self.project_info_provider.get_current_project_raw_config()
            )
            deps_groups = project_raw_config.get("dependency-groups", {})

            envs = [
                EnvInfo(
                    name=env_name,
                    venv_dir_path=path_to_resource_uri(
                        self.runner_info_provider.get_venv_dir_path_of_env(env_name)
                    ),
                    project_def_path=path_to_resource_uri(project_def_path),
                )
                for env_name in deps_groups
            ]

            if payload.env_names is not None:
                envs = [e for e in envs if e.name in payload.env_names]

        self.logger.debug(f"Discovered handler envs: {[e.name for e in envs]}")
        run_context.envs = envs
        return install_envs_action.InstallEnvsRunResult(errors=[])
