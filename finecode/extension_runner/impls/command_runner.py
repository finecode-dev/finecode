import asyncio
import asyncio.subprocess
import shlex
import subprocess
from pathlib import Path

from finecode_extension_api.interfaces import icommandrunner, ilogger


class AsyncProcess(icommandrunner.IAsyncProcess):
    def __init__(self, async_subprocess: asyncio.subprocess.Process):
        self.async_subprocess = async_subprocess

        self._stdout: str | None = None
        self._stderr: str | None = None

    async def wait_for_end(self, timeout: float | None = None) -> None:
        stdout, stderr = await asyncio.wait_for(
            self.async_subprocess.communicate(), timeout=timeout
        )
        self._stdout = stdout.decode()
        self._stderr = stderr.decode()

    def get_exit_code(self) -> int | None:
        return self.async_subprocess.returncode

    def get_output(self) -> str:
        if self._stdout is None:
            # TODO: live output?
            return ""
        else:
            return self._stdout

    def get_error_output(self) -> str:
        if self._stderr is None:
            # TODO: live output?
            return ""
        else:
            return self._stderr


class SyncProcess(icommandrunner.ISyncProcess):
    def __init__(self, popen: subprocess.Popen):
        self.popen = popen
        self._stdout: str | None = None
        self._stderr: str | None = None

    def wait_for_end(self, timeout: float | None = None) -> None:
        stdout, stderr = self.popen.communicate(timeout=timeout)
        self._stdout = stdout.decode()
        self._stderr = stderr.decode()

    def get_exit_code(self) -> int | None:
        return self.popen.returncode

    def get_output(self) -> str:
        if self.popen.returncode is None:
            # TODO: live output?
            return ""
        else:
            return self._stdout

    def get_error_output(self) -> str:
        if self.popen.returncode is None:
            # TODO: live output?
            return ""
        else:
            return self._stderr


class CommandRunner(icommandrunner.ICommandRunner):
    def __init__(self, logger: ilogger.ILogger):
        self.logger = logger

    async def run(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> icommandrunner.IAsyncProcess:
        self.logger.debug(f"Async subprocess run: {cmd} in {cwd}")
        # TODO: investigate why it works only with shell, not exec
        async_subprocess = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        return AsyncProcess(async_subprocess=async_subprocess)

    def run_sync(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> icommandrunner.ISyncProcess:
        cmd_parts = shlex.split(cmd)
        self.logger.debug(f"Sync subprocess run: {cmd_parts}")
        async_subprocess = subprocess.Popen(
            cmd_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        return SyncProcess(popen=async_subprocess)
