import dataclasses

import virtualenv

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import create_env_action
from finecode_extension_api.actions.environments.create_envs_action import CreateEnvsRunResult
from finecode_extension_api.interfaces import ifilemanager, ilogger


@dataclasses.dataclass
class VirtualenvCreateEnvHandlerConfig(code_action.ActionHandlerConfig): ...


class VirtualenvCreateEnvHandler(
    code_action.ActionHandler[
        create_env_action.CreateEnvAction, VirtualenvCreateEnvHandlerConfig
    ]
):
    def __init__(
        self,
        config: VirtualenvCreateEnvHandlerConfig,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        self.config = config
        self.logger = logger
        self.file_manager = file_manager

    async def run(
        self,
        payload: create_env_action.CreateEnvRunPayload,
        run_context: create_env_action.CreateEnvRunContext,
    ) -> CreateEnvsRunResult:
        env_info = payload.env
        if payload.recreate and env_info.venv_dir_path.exists():
            self.logger.debug(f"Remove virtualenv dir {env_info.venv_dir_path}")
            await self.file_manager.remove_dir(env_info.venv_dir_path)

        self.logger.info(f"Creating virtualenv {env_info.venv_dir_path}")
        if not env_info.venv_dir_path.exists():
            try:
                virtualenv.cli_run(
                    [env_info.venv_dir_path.as_posix()],
                    options=None,
                    setup_logging=False,
                    env=None,
                )
            except Exception as exc:
                return CreateEnvsRunResult(
                    errors=[
                        f"Failed to create virtualenv {env_info.venv_dir_path}: {exc}"
                    ]
                )
        else:
            self.logger.info(f"Virtualenv in {env_info} exists already")

        return CreateEnvsRunResult(errors=[])
