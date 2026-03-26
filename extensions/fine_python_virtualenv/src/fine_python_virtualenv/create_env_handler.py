import dataclasses

import virtualenv

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import create_env_action
from finecode_extension_api.actions.environments.create_envs_action import CreateEnvsRunResult
from finecode_extension_api.interfaces import ifilemanager, ilogger
from finecode_extension_api.resource_uri import resource_uri_to_path


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
        venv_dir_path = resource_uri_to_path(env_info.venv_dir_path)
        if payload.recreate and venv_dir_path.exists():
            self.logger.debug(f"Remove virtualenv dir {venv_dir_path}")
            await self.file_manager.remove_dir(venv_dir_path)

        self.logger.info(f"Creating virtualenv {venv_dir_path}")
        # Check for pyvenv.cfg rather than the directory itself — the runner
        # may have already created a logs/ subdirectory inside this path before
        # the venv is set up, which would cause a directory-existence check to
        # incorrectly skip venv creation.
        venv_valid = (venv_dir_path / "pyvenv.cfg").exists()
        if not venv_valid:
            try:
                virtualenv.cli_run(
                    [venv_dir_path.as_posix()],
                    options=None,
                    setup_logging=False,
                    env=None,
                )
            except Exception as exc:
                return CreateEnvsRunResult(
                    errors=[
                        f"Failed to create virtualenv {venv_dir_path}: {exc}"
                    ]
                )
        else:
            self.logger.info(f"Virtualenv in {env_info} exists already")

        return CreateEnvsRunResult(errors=[])
