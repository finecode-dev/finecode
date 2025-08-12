from finecode_extension_api.interfaces import ilogger
import virtualenv

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_envs as prepare_envs_action


class VirtualenvPrepareEnvHandlerConfig(code_action.ActionHandlerConfig):
    ...


class VirtualenvPrepareEnvHandler(
    code_action.ActionHandler[prepare_envs_action.PrepareEnvsAction, VirtualenvPrepareEnvHandlerConfig]
):
    def __init__(
        self,
        config: VirtualenvPrepareEnvHandlerConfig,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.logger = logger

    async def run(
        self,
        payload: prepare_envs_action.PrepareEnvsRunPayload,
        run_context: prepare_envs_action.PrepareEnvsRunContext,
    ) -> prepare_envs_action.PrepareEnvsRunResult:
        # create virtual envs
        
        # would it be faster parallel?
        for env_info in payload.envs:
            self.logger.info(f"Creating virtualenv {env_info.venv_dir_path}")
            if not env_info.venv_dir_path.exists():
                # TODO: '-p <identifier>'
                virtualenv.cli_run([env_info.venv_dir_path.as_posix()], options=None, setup_logging=False, env=None)
            else:
                self.logger.info(f"Virtualenv in {env_info} exists already")

        return prepare_envs_action.PrepareEnvsRunResult(results=[env_info.venv_dir_path for env_info in payload.envs])
