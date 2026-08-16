from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol


class IProcess(Protocol):
    def get_exit_code(self) -> int | None: ...

    def get_output(self) -> str: ...

    def get_error_output(self) -> str: ...

    def write_to_stdin(self, value: str) -> None: ...

    def close_stdin(self) -> None: ...


class ISyncProcess(IProcess, Protocol):
    def wait_for_end(self, timeout: float | None = None) -> None: ...


class IAsyncProcess(IProcess, Protocol):
    async def wait_for_end(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool:
        """Whether the command, or anything it left behind, is still running.

        Not the same question as `get_exit_code() is None`. Commands are spawned
        through a shell, so the exit code belongs to the shell: a shell that
        forks rather than execs can exit -- with a code, and even a signalled
        one -- while the command it started is still running. A caller tearing a
        process down has to ask this instead, or it stops escalating at the
        moment its target looks dead and is not.

        When the process owns a group (`ICommandRunner.run(new_process_group=
        True)`) this reports on the whole group, which is what makes it usable
        as the teardown's stopping condition.
        """
        ...

    def terminate(self) -> None:
        """Ask the process to exit (SIGTERM), and return without waiting.

        A no-op once the process has exited, so a caller escalating after a
        grace period never has to race the exit it is waiting for. Whether the
        signal reaches only the process or its whole tree is decided at spawn
        time by `ICommandRunner.run(new_process_group=...)`.
        """
        ...

    def kill(self) -> None:
        """Stop the process outright (SIGKILL). See `terminate`.

        The escalation, not the first move: a killed process runs no cleanup of
        its own, so anything it spawned outlives it unless it was started in its
        own process group.
        """
        ...

    def stdout_lines(self) -> AsyncIterator[str]:
        """Consume stdout line by line as the process produces it.

        Yields lines with the trailing newline stripped, and finishes when the
        stream reaches EOF -- which may be before the process exits. Lines
        produced between spawn and this call are replayed first, so nothing is
        lost by subscribing late.

        Subscribing switches this stream out of accumulating mode:
        `get_output()` raises afterwards, and only one subscriber per stream is
        allowed. Streaming is offered only on the async flavour -- the sync one
        has no way to interleave reads with anything else.

        Raises `RuntimeError` if the stream ended in an unrecoverable error
        (a line past the reader's size limit, or output that is not UTF-8)
        rather than at a genuine EOF. Abandoning the iterator early is fine and
        drops the stream's remaining output; the process keeps being drained.
        """
        ...

    def stderr_lines(self) -> AsyncIterator[str]:
        """Consume stderr line by line. See `stdout_lines`.

        The two streams decide independently, so a caller may stream stdout
        while stderr keeps accumulating for a failure message.
        """
        ...


class ICommandRunner(Protocol):
    async def run(
        self,
        cmd: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> IAsyncProcess:
        """Spawn a command and return without waiting for it.

        `new_process_group` puts the command in a session of its own, which
        makes `terminate()`/`kill()` reach everything it spawned rather than
        just the shell that started it. Off by default because it also detaches
        the command from the terminal's signals: a caller that never tears a
        process down would only lose the Ctrl-C that used to reach it. Callers
        that own a subprocess tree for the length of a run -- an agent that runs
        tools of its own -- want it on.
        """
        ...

    def run_sync(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> ISyncProcess: ...
