import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_runner_env as prepare_runner_env_action
from finecode_extension_api.actions.prepare_runner_envs import PrepareRunnerEnvsRunResult
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_builtin_handlers import dependency_config_utils


@dataclasses.dataclass
class PrepareRunnerEnvReadConfigHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareRunnerEnvReadConfigHandler(
    code_action.ActionHandler[
        prepare_runner_env_action.PrepareRunnerEnvAction,
        PrepareRunnerEnvReadConfigHandlerConfig,
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
        payload: prepare_runner_env_action.PrepareRunnerEnvRunPayload,
        run_context: prepare_runner_env_action.PrepareRunnerEnvRunContext,
    ) -> PrepareRunnerEnvsRunResult:
        project_raw_config = await self.project_info_provider.get_project_raw_config(
            payload.env.project_def_path
        )
        dependency_config_utils.make_project_config_pip_compatible(
            project_raw_config, payload.env.project_def_path
        )
        run_context.project_def = project_raw_config
        return PrepareRunnerEnvsRunResult(errors=[])
