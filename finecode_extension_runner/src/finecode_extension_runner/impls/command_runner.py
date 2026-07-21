import asyncio
import asyncio.subprocess
import dataclasses
import shlex
import subprocess
from pathlib import Path

from finecode_extension_api.interfaces import icommandrunner, ilogger
from finecode_extension_runner.concurrency import (
    ConcurrencyDecision,
    default_layered_concurrency,
    machine_subprocess_budget,
)


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

    def write_to_stdin(self, value: str) -> None:
        if self.async_subprocess.stdin is not None:
            self.async_subprocess.stdin.write(value.encode())
        else:
            raise RuntimeError("Process was not created with stdin pipe")

    def close_stdin(self) -> None:
        if self.async_subprocess.stdin is not None:
            self.async_subprocess.stdin.close()
        else:
            raise RuntimeError("Process was not created with stdin pipe")


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

    def write_to_stdin(self, value: str) -> None:
        if self.popen.stdin is not None:
            self.popen.stdin.write(value.encode())
            self.popen.stdin.flush()
        else:
            raise RuntimeError("Process was not created with stdin pipe")

    def close_stdin(self) -> None:
        if self.popen.stdin is not None:
            self.popen.stdin.close()
        else:
            raise RuntimeError("Process was not created with stdin pipe")


def resolve_command_runner_concurrency(
    configured_value: int | None,
) -> ConcurrencyDecision:
    """Effective cap on concurrent subprocesses for one ER's `CommandRunner`,
    with the reason it was picked (for logging — see `ConcurrencyDecision`).

    Priority: `config.max_concurrent_processes` (service config, if set and
    positive) > `default_layered_concurrency()`. Has no env var of its own —
    it's delivered as service config (see ADR-0056), which already has a
    machine-local override path via a personal `finecode-user.toml`.
    """
    if configured_value is not None:
        return ConcurrencyDecision(
            max(configured_value, 1), "service config max_concurrent_processes"
        )
    return ConcurrencyDecision(
        default_layered_concurrency(),
        f"computed default (machine budget {machine_subprocess_budget()}, sqrt-split)",
    )


@dataclasses.dataclass
class CommandRunnerConfig:
    max_concurrent_processes: int | None = None


class CommandRunner(icommandrunner.ICommandRunner):
    def __init__(self, logger: ilogger.ILogger, config: CommandRunnerConfig):
        self.logger = logger
        decision = resolve_command_runner_concurrency(config.max_concurrent_processes)
        logger.info(
            f"Capping concurrent subprocesses to {decision.value} ({decision.source})"
        )
        self._semaphore = asyncio.Semaphore(decision.value)

    async def run(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> icommandrunner.IAsyncProcess:
        log_msg = f"Async subprocess run: {cmd}"
        if cwd is not None:
            log_msg += f" in {cwd}"
        self.logger.debug(log_msg)
        # Acquire before spawning and release only when the process actually
        # exits (not when this method returns) — `run()` only spawns and
        # returns immediately, the caller awaits `wait_for_end()` separately,
        # so bounding just this method's body would release the semaphore
        # almost instantly and fail to bound how many subprocesses are alive
        # at once, which is what actually causes resource contention.
        await self._semaphore.acquire()
        try:
            # TODO: investigate why it works only with shell, not exec
            async_subprocess = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except BaseException:
            self._semaphore.release()
            raise
        asyncio.create_task(self._release_when_done(async_subprocess))
        return AsyncProcess(async_subprocess=async_subprocess)

    async def _release_when_done(self, proc: asyncio.subprocess.Process) -> None:
        try:
            await proc.wait()
        finally:
            self._semaphore.release()

    def run_sync(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> icommandrunner.ISyncProcess:
        cmd_parts = shlex.split(cmd)
        log_msg = f"Sync subprocess run: {cmd_parts}"
        if cwd is not None:
            log_msg += f" {cwd}"
        self.logger.debug(log_msg)
        async_subprocess = subprocess.Popen(
            cmd_parts,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        return SyncProcess(popen=async_subprocess)
