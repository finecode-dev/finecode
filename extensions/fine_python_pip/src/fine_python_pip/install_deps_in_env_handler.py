import asyncio
import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions import install_deps_in_env as install_deps_in_env_action
from finecode_extension_api.interfaces import (icommandrunner, ilogger)


@dataclasses.dataclass
class PipInstallDepsInEnvHandlerConfig(code_action.ActionHandlerConfig):
    find_links: list[str] | None = None


class PipInstallDepsInEnvHandler(
    code_action.ActionHandler[install_deps_in_env_action.InstallDepsInEnvAction, PipInstallDepsInEnvHandlerConfig]
):
    def __init__(self, config: PipInstallDepsInEnvHandlerConfig, command_runner: icommandrunner.ICommandRunner, logger: ilogger.ILogger) -> None:
        self.config = config
        self.command_runner = command_runner
        self.logger = logger

    async def run(
        self,
        payload: install_deps_in_env_action.InstallDepsInEnvRunPayload,
        run_context: install_deps_in_env_action.InstallDepsInEnvRunContext,
    ) -> install_deps_in_env_action.InstallDepsInEnvRunResult:
        env_name = payload.env_name
        dependencies = payload.dependencies
        venv_dir_path = payload.venv_dir_path
        project_dir_path = payload.project_dir_path
        python_executable = venv_dir_path / 'bin' / 'python'
        
        # split dependencies in editable and not editable because pip supports
        # installation of editable only with CLI flag '-e'
        editable_dependencies: list[install_deps_in_env_action.Dependency] = []
        non_editable_dependencies: list[install_deps_in_env_action.Dependency] = []
        for dependency in dependencies:
            if dependency.editable:
                editable_dependencies.append(dependency)
            else:
                non_editable_dependencies.append(dependency)

        errors: list[str] = []
        # run pip processes sequentially because they are executed in the same venv,
        # avoid potential concurrency problem in this way
        if len(non_editable_dependencies) > 0:
            cmd = self._construct_pip_install_cmd(python_executable=python_executable, dependencies=non_editable_dependencies, editable=False)
            error = await self._run_pip_cmd(cmd=cmd, env_name=env_name, project_dir_path=project_dir_path)
            if error is not None:
                errors.append(error)
        
        # install editable after non-editable, because non-editable can overwrite editable if there is the same dependency
        if len(editable_dependencies) > 0:
            cmd = self._construct_pip_install_cmd(python_executable=python_executable, dependencies=editable_dependencies, editable=True)
            error = await self._run_pip_cmd(cmd=cmd, env_name=env_name, project_dir_path=project_dir_path)
            if error is not None:
                errors.append(error)

        return install_deps_in_env_action.InstallDepsInEnvRunResult(errors=errors)

    def _construct_pip_install_cmd(self, python_executable: pathlib.Path, dependencies: list[install_deps_in_env_action.Dependency], editable: bool) -> str:
        install_params: str = ''
        if editable:
            install_params += '-e '
        
        if self.config.find_links is not None:
            for link in self.config.find_links:
                install_params += f' --find-links="{link}"'

        for dependency in dependencies:
            if '@ file://' in dependency.version_or_source:
                # dependency is specified as '<name> @ file://' but pip CLI supports
                # only 'file://'
                start_idx_of_file_uri = dependency.version_or_source.index('file://')
                # put in single quoutes to avoid problems in case of spaces in path
                # because in CLI commands single dependencies are splitted by space
                install_params += f"'{dependency.version_or_source[start_idx_of_file_uri:]}' "
            else:
                # put in single quoutes to avoid problems in case of spaces in version,
                # because in CLI commands single dependencies are splitted by space
                install_params += f"'{dependency.name}{dependency.version_or_source}' "
        cmd = f'{python_executable} -m pip --disable-pip-version-check install {install_params}'
        return cmd

    async def _run_pip_cmd(self, cmd: str, env_name: str, project_dir_path: pathlib.Path) -> str | None:
        process = await self.command_runner.run(cmd, cwd=project_dir_path)
        await process.wait_for_end()
        if process.get_exit_code() != 0:
            return f'Installation of dependencies "{cmd}" in env {env_name} from {project_dir_path} failed:\nstdout: {process.get_output()}\nstderr: {process.get_error_output()}'
        
        return None