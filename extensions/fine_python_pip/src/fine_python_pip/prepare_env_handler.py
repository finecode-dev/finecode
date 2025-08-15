import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_envs as prepare_envs_action
from finecode_extension_api.interfaces import (icommandrunner, ilogger)


@dataclasses.dataclass
class PipPrepareEnvHandlerConfig(code_action.ActionHandlerConfig):
    ...


class PipPrepareEnvHandler(
    code_action.ActionHandler[prepare_envs_action.PrepareEnvsAction, PipPrepareEnvHandlerConfig]
):
    def __init__(self, command_runner: icommandrunner.ICommandRunner, logger: ilogger.ILogger) -> None:
        self.command_runner = command_runner
        self.logger = logger

    async def run(
        self,
        payload: prepare_envs_action.PrepareEnvsRunPayload,
        run_context: prepare_envs_action.PrepareEnvsRunContext,
    ) -> prepare_envs_action.PrepareEnvsRunResult:
        install_processes: list[icommandrunner.IAsyncProcess] = []
        for env_info in payload.envs:
            python_executable = env_info.venv_dir_path / 'bin' / 'python'
            project_def_path = run_context.project_def_path_by_venv_dir_path[env_info.venv_dir_path]
            pip_params = ''
            if env_info.name == 'runtime':
                pip_params += ' -e .'

            process = await self.command_runner.run(f'{python_executable} -m pip --disable-pip-version-check install {pip_params} --group="{env_info.name}"', cwd=project_def_path.parent)
            install_processes.append(process)

        async with asyncio.TaskGroup() as tg:
            for process in install_processes:
                tg.create_task(process.wait_for_end())

        errors: list[str] = []
        for idx, process in enumerate(install_processes):
            if process.get_exit_code() != 0:
                env_info = payload.envs[idx]
                project_def_path = run_context.project_def_path_by_venv_dir_path[env_info.venv_dir_path]
                errors.append(f'Installation of dependencies in env {env_info.name} from {project_def_path} failed:\nstdout: {process.get_output()}\nstderr: {process.get_error_output()}')

        return prepare_envs_action.PrepareEnvsRunResult(errors=errors)
