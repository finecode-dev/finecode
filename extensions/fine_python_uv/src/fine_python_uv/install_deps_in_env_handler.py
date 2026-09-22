import dataclasses
import pathlib
import shlex
import sys

from fine_envs import install_deps_in_env_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import resource_uri_to_path

from ._uv_common import get_uv_executable, temp_project_config_dump


@dataclasses.dataclass
class UvInstallDepsInEnvHandlerConfig(code_action.ActionHandlerConfig):
    find_links: list[str] | None = None
    editable_mode: str | None = None


def _quote_arg(arg: str) -> str:
    """Quote one argument for the shell `ICommandRunner.run` spawns through.

    On Windows that is cmd.exe, and uv.exe splits its command line by MSVC
    rules: `'` is not a quote there, and cmd treats an unquoted `>` in a
    version spec as a redirect. Double quotes are honoured by both. The args
    quoted here are PEP 508 requirements and config settings, where `"` can
    only be a marker-string quote, so it is swapped for the equivalent `'`
    rather than escaped (a `\\"` toggles cmd's own quote state).
    """
    if sys.platform == "win32":
        return '"' + arg.replace('"', "'") + '"'
    return shlex.quote(arg)


class UvInstallDepsInEnvHandler(
    code_action.ActionHandler[
        install_deps_in_env_action.InstallDepsInEnvAction,
        UvInstallDepsInEnvHandlerConfig,
    ]
):
    def __init__(
        self,
        config: UvInstallDepsInEnvHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.logger = logger
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: install_deps_in_env_action.InstallDepsInEnvRunPayload,
        run_context: install_deps_in_env_action.InstallDepsInEnvRunContext,
    ) -> install_deps_in_env_action.InstallDepsInEnvRunResult:
        env_name = payload.env_name
        dependencies = payload.dependencies
        venv_dir_path = resource_uri_to_path(payload.venv_dir_path)
        project_dir_path = resource_uri_to_path(payload.project_dir_path)

        project_def_path = project_dir_path / "pyproject.toml"
        async with temp_project_config_dump(
            project_def_path=project_def_path,
            action_runner=self.action_runner,
            project_info_provider=self.project_info_provider,
            logger=self.logger,
            meta=run_context.meta,
        ) as dump_dir:
            uv_executable = get_uv_executable()
            cmd = self._construct_uv_install_cmd(
                uv_executable=uv_executable,
                venv_dir_path=venv_dir_path,
                dependencies=dependencies,
            )
            error = await self._run_uv_cmd(
                cmd=cmd,
                env_name=env_name,
                cwd=dump_dir,
                project_dir_path=project_dir_path,
            )
            if error is not None:
                errors = [error]
            else:
                errors = []

        return install_deps_in_env_action.InstallDepsInEnvRunResult(errors=errors)

    def _construct_uv_install_cmd(
        self,
        uv_executable,
        venv_dir_path,
        dependencies: list[install_deps_in_env_action.Dependency],
    ) -> str:
        install_params: str = ""

        if self.config.find_links is not None:
            for link in self.config.find_links:
                install_params += f'--find-links="{link}" '

        if self.config.editable_mode is not None:
            install_params += (
                f"-C {_quote_arg(f'editable_mode={self.config.editable_mode}')} "
            )

        for dependency in dependencies:
            if dependency.editable:
                install_params += "-e "

            extras_str = ""
            if dependency.extras:
                extras_str = "[" + ",".join(dependency.extras) + "]"

            install_params += (
                _quote_arg(
                    f"{dependency.name}{extras_str}{dependency.version_or_source}"
                )
                + " "
            )

        cmd = f'"{uv_executable}" --no-config pip install --python "{venv_dir_path}" {install_params}'
        return cmd

    async def _run_uv_cmd(
        self,
        cmd: str,
        env_name: str,
        cwd: pathlib.Path,
        project_dir_path: pathlib.Path,
    ) -> str | None:
        self.logger.debug(f"Running uv: {cmd}")
        process = await self.command_runner.run(cmd, cwd=cwd)
        await process.wait_for_end()
        process_stdout = process.get_output()
        process_stderr = process.get_error_output()
        if process_stdout:
            self.logger.trace(f"uv stdout:\n{process_stdout}")
        if process_stderr:
            self.logger.trace(f"uv stderr:\n{process_stderr}")
        if process.get_exit_code() != 0:
            logs = ""
            if process_stdout and process_stderr:
                logs = f"stdout: {process_stdout}\nstderr: {process_stderr}"
            elif process_stdout:
                logs = process_stdout
            else:
                logs = process_stderr

            error = (
                f"Installation of dependencies in env {env_name} for project "
                f"{project_dir_path} failed (cmd: {cmd}):\n{logs}\n"
                "The config uv ran with was a temporary dump of the project; run "
                "`python -m finecode dump-config` for that project to inspect it."
            )
            self.logger.error(error)
            return error

        return None
