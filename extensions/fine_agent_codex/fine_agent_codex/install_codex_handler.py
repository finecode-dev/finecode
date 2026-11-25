import dataclasses
import shutil

from fine_agent import backend_support
from fine_system_setup.setup_system_action import (
    SetupSystemAction,
    SetupSystemRunContext,
    SetupSystemRunPayload,
    SetupSystemRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner, ilogger

_TOOL_NAME = "codex"
_NPM_PACKAGE = "@openai/codex"


@dataclasses.dataclass
class InstallCodexHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallCodexHandler(
    code_action.ActionHandler[
        SetupSystemAction,
        InstallCodexHandlerConfig,
    ]
):
    """Install the OpenAI Codex CLI via npm if not already present."""

    def __init__(
        self,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
    ) -> None:
        self.logger = logger
        self.command_runner = command_runner

    async def run(
        self,
        payload: SetupSystemRunPayload,
        run_context: SetupSystemRunContext,
    ) -> SetupSystemRunResult:
        if shutil.which("codex") is not None:
            self.logger.info("codex already installed, skipping")
            return SetupSystemRunResult(skipped=[_TOOL_NAME])

        if shutil.which("npm") is None:
            error = "npm not found in PATH, install Node.js first"
            self.logger.error(f"Install failed: {error}")
            return SetupSystemRunResult(failed=[f"{_TOOL_NAME}: {error}"])

        # --ignore-scripts: Codex declares no lifecycle scripts and ships its
        # native binary as an optional dependency, which --ignore-scripts still
        # installs, so skipping scripts avoids running arbitrary code from the
        # dependency tree without losing the binary
        cmd = ["npm", "install", "-g", "--ignore-scripts", _NPM_PACKAGE]

        async with run_context.progress("Installing codex", total=1) as progress:
            self.logger.info(f"Running installer: {cmd!r}")
            await progress.report("Running npm install")
            try:
                process = await self.command_runner.run_to_completion(cmd)
            except icommandrunner.CommandStartError as error:
                self.logger.error(f"Install failed: {error}")
                return SetupSystemRunResult(
                    failed=[
                        f"{_TOOL_NAME}: {backend_support.spawn_error(cmd[0], error)}"
                    ]
                )
            await progress.advance(1)

        exit_code = process.get_exit_code()
        if exit_code != 0:
            error = process.get_error_output().strip() or process.get_output().strip()
            self.logger.error(f"Install failed: {error}")
            return SetupSystemRunResult(failed=[f"{_TOOL_NAME}: {error}"])

        self.logger.info("codex installed successfully")
        return SetupSystemRunResult(installed=[_TOOL_NAME])
