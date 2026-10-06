import dataclasses
import pathlib

from fine_envs import install_deps_in_env_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner, ilogger
from finecode_extension_api.resource_uri import resource_uri_to_path


@dataclasses.dataclass
class PipInstallDepsInEnvHandlerConfig(code_action.ActionHandlerConfig):
    find_links: list[str] | None = None
    editable_mode: str | None = None


class PipInstallDepsInEnvHandler(
    code_action.ActionHandler[
        install_deps_in_env_action.InstallDepsInEnvAction,
        PipInstallDepsInEnvHandlerConfig,
    ]
):
    def __init__(
        self,
        config: PipInstallDepsInEnvHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
    ) -> None:
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
        venv_dir_path = resource_uri_to_path(payload.venv_dir_path)
        project_dir_path = resource_uri_to_path(payload.project_dir_path)
        python_executable = venv_dir_path / "bin" / "python"

        cmd = self._construct_pip_install_cmd(
            python_executable=python_executable, dependencies=dependencies
        )
        error = await self._run_pip_cmd(
            cmd=cmd, env_name=env_name, project_dir_path=project_dir_path
        )
        if error is not None:
            errors = [error]
        else:
            errors = []

        return install_deps_in_env_action.InstallDepsInEnvRunResult(errors=errors)

    def _construct_pip_install_cmd(
        self,
        python_executable: pathlib.Path,
        dependencies: list[install_deps_in_env_action.Dependency],
    ) -> list[str]:
        cmd: list[str] = [
            str(python_executable),
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
        ]

        if self.config.find_links is not None:
            for link in self.config.find_links:
                cmd.append(f"--find-links={link}")

        if self.config.editable_mode is not None:
            cmd.append("--config-settings")
            cmd.append(f"editable_mode={self.config.editable_mode}")

        for dependency in dependencies:
            if dependency.editable:
                cmd.append("-e")

            extras_str = ""
            if dependency.extras:
                extras_str = "[" + ",".join(dependency.extras) + "]"

            if "@ file://" in dependency.version_or_source:
                # dependency is specified as '<name> @ file://' but pip CLI supports
                # only 'file://'
                start_idx_of_file_uri = dependency.version_or_source.index("file://")
                cmd.append(
                    f"{dependency.version_or_source[start_idx_of_file_uri:]}{extras_str}"
                )
            else:
                cmd.append(
                    f"{dependency.name}{extras_str}{dependency.version_or_source}"
                )
        return cmd

    async def _run_pip_cmd(
        self, cmd: list[str], env_name: str, project_dir_path: pathlib.Path
    ) -> str | None:
        self.logger.debug(f"Running pip: {cmd!r}")
        process = await self.command_runner.run(cmd, cwd=project_dir_path)
        await process.wait_for_end()
        process_stdout = process.get_output()
        process_stderr = process.get_error_output()
        if process_stdout:
            self.logger.trace(f"pip stdout:\n{process_stdout}")
        if process_stderr:
            self.logger.trace(f"pip stderr:\n{process_stderr}")
        if process.get_exit_code() != 0:
            logs = ""
            if process_stdout and process_stderr:
                logs = f"stdout: {process_stdout}\nstderr: {process_stderr}"
            elif process_stdout:
                logs = process_stdout
            else:
                logs = process_stderr

            error = f"Installation of dependencies in env {env_name} from {project_dir_path} failed (cmd: {cmd!r}):\n{logs}"
            self.logger.error(error)
            return error

        return None
