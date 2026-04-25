import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import install_env_action
from finecode_extension_api.actions.environments.install_envs_action import (
    InstallEnvsRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path


@dataclasses.dataclass
class InstallEnvReadConfigHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallEnvReadConfigHandler(
    code_action.ActionHandler[
        install_env_action.InstallEnvAction,
        InstallEnvReadConfigHandlerConfig,
    ]
):
    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: install_env_action.InstallEnvRunPayload,
        run_context: install_env_action.InstallEnvRunContext,
    ) -> InstallEnvsRunResult:
        project_def_path = resource_uri_to_path(payload.env.project_def_path)
        project_raw_config = await self.project_info_provider.get_project_raw_config(
            project_def_path
        )
        run_context.project_def = project_raw_config
        return InstallEnvsRunResult(errors=[])
