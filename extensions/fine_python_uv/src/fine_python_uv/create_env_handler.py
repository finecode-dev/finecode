import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import create_env_action
from finecode_extension_api.actions.environments.create_envs_action import CreateEnvsRunResult
from finecode_extension_api.interfaces import icommandrunner, ifilemanager, ilogger, iprojectactionrunner, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path

from ._uv_common import dump_project_config, get_uv_executable


@dataclasses.dataclass
class UvCreateEnvHandlerConfig(code_action.ActionHandlerConfig): ...


class UvCreateEnvHandler(
    code_action.ActionHandler[
        create_env_action.CreateEnvAction, UvCreateEnvHandlerConfig
    ]
):
    def __init__(
        self,
        config: UvCreateEnvHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.logger = logger
        self.file_manager = file_manager
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider

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
            project_def_path = resource_uri_to_path(env_info.project_def_path)
            dump_dir = await dump_project_config(
                project_def_path=project_def_path,
                action_runner=self.action_runner,
                project_info_provider=self.project_info_provider,
                logger=self.logger,
                meta=run_context.meta,
            )

            uv_executable = get_uv_executable()
            cmd = f'"{uv_executable}" venv "{venv_dir_path}"'
            self.logger.debug(f"Running uv: {cmd}")
            process = await self.command_runner.run(cmd, cwd=dump_dir)
            await process.wait_for_end()
            if process.get_exit_code() != 0:
                error_output = process.get_error_output() or process.get_output()
                return CreateEnvsRunResult(
                    errors=[f"Failed to create virtualenv {venv_dir_path}:\n{error_output}"]
                )
        else:
            self.logger.info(f"Virtualenv in {env_info.name} exists already")

        return CreateEnvsRunResult(errors=[])
