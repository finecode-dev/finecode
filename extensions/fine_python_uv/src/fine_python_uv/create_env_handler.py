import dataclasses
from pathlib import Path

from finecode_extension_api import code_action
from fine_envs import create_env_action
from fine_envs.create_envs_action import CreateEnvsRunResult
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

    async def _is_valid_virtualenv(self, venv_dir_path: Path) -> bool:
        # A valid venv must contain pyvenv.cfg and a runnable interpreter that
        # reports a virtualenv prefix relationship.
        #
        # NOTE: this probe does not check that the
        # existing venv's interpreter actually matches `env_info.interpreter`. It could
        # be extended to probe the venv python's `platform.python_implementation()` +
        # version and treat a mismatch as invalid, so that changing an env's
        # interpreter rebuilds a now-stale venv instead of silently keeping the old one.
        pyvenv_cfg = venv_dir_path / "pyvenv.cfg"
        if not pyvenv_cfg.exists():
            return False

        python_candidates = [
            venv_dir_path / "bin" / "python",
            venv_dir_path / "Scripts" / "python.exe",
            venv_dir_path / "Scripts" / "python",
        ]
        venv_python = next((p for p in python_candidates if p.exists()), None)
        if venv_python is None:
            return False

        check_cmd = (
            f'"{venv_python}" -c '
            '"import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)"'
        )
        self.logger.debug(f"Checking virtualenv validity: {check_cmd}")
        process = await self.command_runner.run(check_cmd)
        await process.wait_for_end()
        if process.get_exit_code() != 0:
            self.logger.debug(
                f"Virtualenv probe failed for {venv_dir_path}: "
                f"{process.get_error_output() or process.get_output()}"
            )
            return False

        return True

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

        venv_valid = await self._is_valid_virtualenv(venv_dir_path)
        if not venv_valid:
            self.logger.info(f"Creating virtualenv {venv_dir_path}")
            project_def_path = resource_uri_to_path(env_info.project_def_path)
            dump_dir = await dump_project_config(
                project_def_path=project_def_path,
                action_runner=self.action_runner,
                project_info_provider=self.project_info_provider,
                logger=self.logger,
                meta=run_context.meta,
            )

            uv_executable = get_uv_executable()
            # venv can exist but be invalid, use '--clear' to recreate it
            python_flag = (
                f' --python "{env_info.interpreter}"' if env_info.interpreter else ""
            )
            cmd = f'"{uv_executable}" venv --clear{python_flag} "{venv_dir_path}"'
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
