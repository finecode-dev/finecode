import dataclasses
import shlex
import shutil

from fine_system_setup.setup_system_action import (
    SetupSystemAction,
    SetupSystemRunContext,
    SetupSystemRunPayload,
    SetupSystemRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner, ilogger

_TOOL_NAME = "pi"
_NPM_PACKAGE = "@earendil-works/pi-coding-agent"


@dataclasses.dataclass
class InstallPiHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallPiHandler(
    code_action.ActionHandler[
        SetupSystemAction,
        InstallPiHandlerConfig,
    ]
):
    """Install the pi.dev coding agent CLI via npm if not already present."""

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
        if shutil.which("pi") is not None:
            self.logger.info("pi already installed, skipping")
            return SetupSystemRunResult(skipped=[_TOOL_NAME])

        if shutil.which("npm") is None:
            error = "npm not found in PATH, install Node.js first"
            self.logger.error(f"Install failed: {error}")
            return SetupSystemRunResult(failed=[f"{_TOOL_NAME}: {error}"])

        # --ignore-scripts: pi needs no lifecycle scripts, so skipping them avoids
        # running arbitrary code from the dependency tree
        cmd = shlex.join(["npm", "install", "-g", "--ignore-scripts", _NPM_PACKAGE])

        async with run_context.progress("Installing pi", total=1) as progress:
            self.logger.info(f"Running installer: {cmd}")
            await progress.report("Running npm install")
            process = await self.command_runner.run(cmd)
            await process.wait_for_end()
            await progress.advance(1)

        exit_code = process.get_exit_code()
        if exit_code != 0:
            error = process.get_error_output().strip() or process.get_output().strip()
            self.logger.error(f"Install failed: {error}")
            return SetupSystemRunResult(failed=[f"{_TOOL_NAME}: {error}"])

        self.logger.info("pi installed successfully")
        return SetupSystemRunResult(installed=[_TOOL_NAME])
