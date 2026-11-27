import dataclasses
from pathlib import Path

from fine_envs import create_env_action
from fine_envs.create_envs_action import CreateEnvsRunResult
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ifilemanager,
    ilogger,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from ._uv_common import get_uv_executable


@dataclasses.dataclass
class UvCreateEnvHandlerConfig(code_action.ActionHandlerConfig): ...


class UvCreateEnvHandler(
    code_action.ActionHandler[
        create_env_action.CreateEnvAction, UvCreateEnvHandlerConfig
    ]
):
    """Create a virtualenv with ``uv venv`` from a supplied definition.

    It needs a definition, and with none it fails and makes no dump. An unusable
    value is detected before an existing venv is removed, also under ``recreate``.
    A supplied project definition file must be named exactly ``pyproject.toml``.
    uv runs with that file's directory as its working directory; the handler
    never writes or removes anything there. From that directory uv also uses a
    ``.python-version`` file beside the supplied file, and a ``uv.toml`` file
    beside it or in any ancestor directory, unless the supplied file itself has
    a ``[tool.uv]`` table (measured with uv 0.13.0). Each project's subdirectory
    of the dispatcher's run directory holds only that project's dump.
    """

    def __init__(
        self,
        config: UvCreateEnvHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.logger = logger
        self.file_manager = file_manager

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

        check_cmd = [
            str(venv_python),
            "-c",
            "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)",
        ]
        self.logger.debug(f"Checking virtualenv validity: {check_cmd!r}")
        process = await self.command_runner.run_to_completion(check_cmd)
        if process.get_exit_code() != 0:
            self.logger.debug(
                f"Virtualenv probe failed for {venv_dir_path}: "
                f"{process.get_error_output() or process.get_output()}"
            )
            return False

        return True

    def _supplied_error(
        self, venv_dir_path: Path, supplied: ResourceUri, reason: str
    ) -> CreateEnvsRunResult:
        return CreateEnvsRunResult(
            errors=[
                (
                    f"Failed to create virtualenv {venv_dir_path}: supplied project "
                    f"definition file {supplied} {reason}; uv was not started."
                )
            ],
            created=False,
        )

    async def _run_uv_venv(
        self,
        *,
        venv_dir_path: Path,
        interpreter: str | None,
        cwd: Path,
        supplied: ResourceUri,
    ) -> CreateEnvsRunResult:
        uv_executable = get_uv_executable()
        # venv can exist but be invalid, use '--clear' to recreate it
        cmd: list[str] = [str(uv_executable), "venv", "--clear"]
        if interpreter:
            cmd.extend(["--python", str(interpreter)])
        cmd.append(str(venv_dir_path))
        self.logger.debug(f"Running uv: {cmd!r}")
        process = await self.command_runner.run_to_completion(cmd, cwd=cwd)
        if process.get_exit_code() != 0:
            error_output = process.get_error_output() or process.get_output()
            return CreateEnvsRunResult(
                errors=[
                    (
                        f"Failed to create virtualenv {venv_dir_path}: supplied project "
                        f"definition file {supplied} gave a failing uv run:\n"
                        f"{error_output}"
                    )
                ]
            )
        return CreateEnvsRunResult(errors=[], created=True)

    async def run(
        self,
        payload: create_env_action.CreateEnvRunPayload,
        run_context: create_env_action.CreateEnvRunContext,
    ) -> CreateEnvsRunResult:
        _ = run_context
        env_info = payload.env
        venv_dir_path = resource_uri_to_path(env_info.venv_dir_path)
        supplied = payload.resolved_project_def_path

        if payload.recreate:
            if supplied is None or not self._is_usable_value(supplied):
                return self._null_or_unusable_error(venv_dir_path, supplied)
            if venv_dir_path.exists():
                self.logger.debug(f"Remove virtualenv dir {venv_dir_path}")
                await self.file_manager.remove_dir(venv_dir_path, tolerant=True)
        else:
            venv_valid = await self._is_valid_virtualenv(venv_dir_path)
            if venv_valid:
                self.logger.info(f"Virtualenv in {env_info.name} exists already")
                return CreateEnvsRunResult(errors=[], created=False)
            if supplied is None or not self._is_usable_value(supplied):
                return self._null_or_unusable_error(venv_dir_path, supplied)

        assert supplied is not None
        try:
            supplied_path = resource_uri_to_path(supplied)
        except ValueError:
            # Re-checked after the recreate branch; kept for the race-free path.
            return self._supplied_error(venv_dir_path, supplied, "is not a file URI")
        try:
            return await self._run_uv_venv(
                venv_dir_path=venv_dir_path,
                interpreter=env_info.interpreter,
                cwd=supplied_path.parent,
                supplied=supplied,
            )
        except icommandrunner.ProgramStartError as exc:
            return CreateEnvsRunResult(
                errors=[
                    (
                        f"Failed to create virtualenv {venv_dir_path}: supplied project "
                        f"definition file {supplied} was removed before uv started "
                        f"({exc}); uv was not started."
                    )
                ],
                created=False,
            )

    def _is_usable_value(self, supplied: ResourceUri) -> bool:
        try:
            supplied_path = resource_uri_to_path(supplied)
        except ValueError:
            return False
        if supplied_path.name != "pyproject.toml":
            return False
        return supplied_path.is_file()

    def _null_or_unusable_error(
        self, venv_dir_path: Path, supplied: ResourceUri | None
    ) -> CreateEnvsRunResult:
        if supplied is None:
            return CreateEnvsRunResult(
                errors=[
                    (
                        f"Failed to create virtualenv {venv_dir_path}: no project "
                        f"definition was supplied in resolved_project_def_path; "
                        "uv was not started."
                    )
                ],
                created=False,
            )
        try:
            supplied_path = resource_uri_to_path(supplied)
        except ValueError:
            return self._supplied_error(venv_dir_path, supplied, "is not a file URI")
        if supplied_path.name != "pyproject.toml":
            return self._supplied_error(
                venv_dir_path, supplied, "is not a pyproject.toml file"
            )
        return self._supplied_error(venv_dir_path, supplied, "names no existing file")
