import dataclasses
import shlex
import shutil
import sys
import tempfile

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner, ihttpclient, ilogger
from fine_system_setup.setup_system_action import (
    SetupSystemAction,
    SetupSystemRunContext,
    SetupSystemRunPayload,
    SetupSystemRunResult,
)

_TOOL_NAME = "claude-code"
_INSTALL_SH_URL = "https://claude.ai/install.sh"
_INSTALL_PS1_URL = "https://claude.ai/install.ps1"


@dataclasses.dataclass
class InstallClaudeCodeHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallClaudeCodeHandler(
    code_action.ActionHandler[
        SetupSystemAction,
        InstallClaudeCodeHandlerConfig,
    ]
):
    """Install the Claude Code CLI via the native installer if not already present."""

    def __init__(
        self,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
        http_client: ihttpclient.IHttpClient,
    ) -> None:
        self.logger = logger
        self.command_runner = command_runner
        self.http_client = http_client

    async def run(
        self,
        payload: SetupSystemRunPayload,
        run_context: SetupSystemRunContext,
    ) -> SetupSystemRunResult:
        if shutil.which("claude") is not None:
            self.logger.info("claude already installed, skipping")
            return SetupSystemRunResult(skipped=[_TOOL_NAME])

        if sys.platform == "win32":
            url = _INSTALL_PS1_URL
            suffix = ".ps1"
        else:
            url = _INSTALL_SH_URL
            suffix = ".sh"

        self.logger.info(f"Downloading installer from {url}")
        async with run_context.progress("Installing claude-code", total=2) as progress:
            await progress.report("Downloading installer")
            async with self.http_client.session() as session:
                response = await session.get(url)
                response.raise_for_status()
                script_content = response.text

            with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete_on_close=False) as f:
                f.write(script_content)
                script_path = f.name
                f.close()  # flush and release the file before the subprocess opens it

                if sys.platform == "win32":
                    cmd = shlex.join(
                        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path]
                    )
                else:
                    cmd = shlex.join(["bash", script_path])

                self.logger.info(f"Running installer: {cmd}")
                await progress.advance(1, "Running installer")
                process = await self.command_runner.run(cmd)
                await process.wait_for_end()
            await progress.advance(1)

        exit_code = process.get_exit_code()
        if exit_code != 0:
            error = process.get_error_output().strip() or process.get_output().strip()
            self.logger.error(f"Install failed: {error}")
            return SetupSystemRunResult(failed=[f"{_TOOL_NAME}: {error}"])

        self.logger.info("claude-code installed successfully")
        return SetupSystemRunResult(installed=[_TOOL_NAME])
