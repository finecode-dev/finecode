from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, TypeAlias

Argv: TypeAlias = list[str] | tuple[str, ...]
"""A command as an argv vector: one element per argument, no shell parsing.

A bare `str` is deliberately not accepted — a shell command line is the thing
this API removes, and a `str` is neither a `list` nor a `tuple`, so the static
checker rejects it as well as `check_argv` at runtime.
"""


def check_argv(cmd: object) -> None:
    """Validate a command vector before it reaches a process spawner.

    Rejects the shell-shaped command line this API exists to remove: a `str`
    is a `TypeError`, and so is anything that is not a `list`/`tuple` of
    `str` — a `Path` element must be `str()`-converted by the caller.

    Raises:
        TypeError: `cmd` is a `str`, is not a `list`/`tuple`, or has a
            non-`str` element.
        ValueError: `cmd` is empty.
    """
    if isinstance(cmd, str):
        raise TypeError(
            "command must be an argv list (e.g. ['git', 'status']), not a "
            "shell command string; commands are executed without a shell"
        )
    if not isinstance(cmd, (list, tuple)):
        raise TypeError(
            f"command must be a list or tuple of str, got {type(cmd).__name__}"
        )
    if not cmd:
        raise ValueError("command argv must not be empty")
    for position, arg in enumerate(cmd):
        if not isinstance(arg, str):
            raise TypeError(
                f"command argv element {position} must be a str, got "
                f"{type(arg).__name__}; convert Paths with str()"
            )


class CommandNotLaunchableError(ValueError):
    """A command vector cannot be started as given, before any process runs.

    The two refusal subtypes are `UnsafeBatchArgumentError` (an argument a
    Windows batch shim would reinterpret) and `UnlaunchableProgramError` (a
    program file no OS spawner can start). Distinct from the `OSError` a
    missing or unstartable program raises at spawn time.
    """


class UnsafeBatchArgumentError(CommandNotLaunchableError):
    """An argument to a Windows `.cmd`/`.bat` shim would be reinterpreted.

    On Windows a resolved batch shim is executed by cmd.exe, which expands and
    interprets characters (`%`, `!`, `^`, `&`, `|`, ...) that
    `subprocess.list2cmdline` does not escape for it. The argument is refused
    rather than escaped: writing a cmd.exe quoter would reintroduce exactly
    the shell-parsing hazard this API removes.
    """


class UnlaunchableProgramError(CommandNotLaunchableError):
    """A resolved program file cannot be started by the OS process spawner.

    On Windows, CreateProcess launches only `.exe`, `.com`, `.cmd` and
    `.bat`. A bare program name that resolves to anything else (a `.js` or
    `.py` script, say) cannot be started without an interpreter in argv.
    """


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

        Not the same question as `get_exit_code() is None`. The command may be
        a launcher that starts further processes and exits — with a code, and
        even a signalled one — while what it started is still running. A
        caller tearing a process down has to ask this instead, or it stops
        escalating at the moment its target looks dead and is not.

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
        cmd: Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> IAsyncProcess:
        """Spawn a command from an argv vector; return without waiting for it.

        Each element is passed to the program as exactly one argument, on any
        OS: no shell parses the command line, so nothing has to be quoted or
        escaped, and an element a shell would reinterpret arrives at the
        program verbatim. Pass paths as `str()`-converted strings; a `Path`
        element is rejected.

        No shell features exist here — no pipes, redirections, environment
        expansion or `~`. A command that genuinely needs a shell runs
        `["bash", "-c", ...]` explicitly and owns its quoting.

        `new_process_group` puts the command in a session of its own, which
        makes `terminate()`/`kill()` reach everything it spawned rather than
        just the process itself. Off by default because it also detaches the
        command from the terminal's signals: a caller that never tears a
        process down would only lose the Ctrl-C that used to reach it. Callers
        that own a subprocess tree for the length of a run -- an agent that
        runs tools of its own -- want it on.

        Raises:
            TypeError: `cmd` is a `str` (a shell command line is rejected on
                purpose), not a `list`/`tuple`, or has a non-`str` element.
            ValueError: `cmd` is empty.
            UnsafeBatchArgumentError: an argument to a Windows `.cmd`/`.bat`
                shim would be reinterpreted by cmd.exe.
            UnlaunchableProgramError: a bare program name on Windows resolves
                to a file the OS cannot launch.
            OSError: the program could not be started (for example, it does
                not exist).
        """
        ...

    def run_sync(
        self, cmd: Argv, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> ISyncProcess:
        """Spawn a command from an argv vector and block on it. See `run`.

        Same contract as `run` — one argv element per argument, no shell, the
        same refusals and `OSError` on an unstartable program — minus the
        process-group option.
        """
        ...
