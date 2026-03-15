import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    prepare_handler_env as prepare_handler_env_action,
)
from finecode_extension_api.actions.prepare_handler_envs import (
    PrepareHandlerEnvsRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_builtin_handlers import dependency_config_utils


@dataclasses.dataclass
class PrepareHandlerEnvReadConfigHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareHandlerEnvReadConfigHandler(
    code_action.ActionHandler[
        prepare_handler_env_action.PrepareHandlerEnvAction,
        PrepareHandlerEnvReadConfigHandlerConfig,
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
        payload: prepare_handler_env_action.PrepareHandlerEnvRunPayload,
        run_context: prepare_handler_env_action.PrepareHandlerEnvRunContext,
    ) -> PrepareHandlerEnvsRunResult:
        project_raw_config = await self.project_info_provider.get_project_raw_config(
            payload.env.project_def_path
        )
        dependency_config_utils.make_project_config_pip_compatible(
            project_raw_config, payload.env.project_def_path
        )
        run_context.project_def = project_raw_config
        return PrepareHandlerEnvsRunResult(errors=[])
