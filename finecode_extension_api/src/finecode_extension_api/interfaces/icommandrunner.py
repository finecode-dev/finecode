import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, Protocol, TypeAlias

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


class CommandStartError(Exception):
    """A command could not be started; no process of it is running.

    The one exception a caller catches to turn an unstartable command into a
    structured failure. Its subtypes say why.
    """


class CommandNotLaunchableError(CommandStartError):
    """A command vector cannot be started as given, before any process runs.

    The two refusal subtypes are `UnsafeBatchArgumentError` (an argument a
    Windows batch shim would reinterpret) and `UnlaunchableProgramError` (a
    program file no OS spawner can start).
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


class ProgramStartError(CommandStartError):
    """The OS failed to start the program (for example, it does not exist).

    `errno` and `strerror` come from the spawn's `OSError`, which is also the
    `__cause__`. A missing program and a missing `cwd` share `ENOENT`, so the
    message names both rather than guessing which one is absent.
    """

    def __init__(self, program: str, cwd: Path | None, error: OSError) -> None:
        self.program: str = program
        self.cwd: Path | None = cwd
        self.errno: int | None = error.errno
        self.strerror: str | None = error.strerror
        detail = error.strerror or str(error)
        location = f" in {cwd}" if cwd is not None else ""
        super().__init__(f"could not start {program}{location}: {detail}")


class OwnedProcessNotStoppedError(RuntimeError):
    """A bounded scope exited while the owned process group was still alive."""

    def __init__(self, pids: tuple[int, ...]) -> None:
        self.pids: tuple[int, ...] = pids
        super().__init__(
            "Owned process group still alive: " + ", ".join(str(p) for p in pids)
        )


class ProcessOutputIncompleteError(RuntimeError):
    """Buffered output is incomplete after a drain failure or abandonment."""

    def __init__(self, stream: Literal["stdout", "stderr"], reason: str) -> None:
        self.stream: Literal["stdout", "stderr"] = stream
        super().__init__(f"Process {stream} output incomplete: {reason}")


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
    def run(
        self,
        cmd: Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
    ) -> contextlib.AbstractAsyncContextManager[IAsyncProcess]:
        """Spawn a command as a scoped unit of work; the scope owns the process.

        Entering the scope spawns the process; leaving it stops whatever is
        still alive on a bounded ladder (SIGTERM, then SIGKILL after a grace
        on the owned group, or one tree kill on Windows), waits a bounded
        time until the process or group is gone, releases the work slot and
        the local-cap permit, waits a bounded time for the WM release, and
        propagates under the body's exception precedence. Accepted costs: an
        escaped `setsid` descendant, a no-group descendant, Windows best
        effort, an unanswered WM release, unconfirmable survivors.

        Each element is passed to the program as exactly one argument, on any
        OS: no shell parses the command line, so nothing has to be quoted or
        escaped, and an element a shell would reinterpret arrives at the
        program verbatim. Pass paths as `str()`-converted strings; a `Path`
        element is rejected.

        No shell features exist here — no pipes, redirections, environment
        expansion or `~`. A command that genuinely needs a shell runs
        `["bash", "-c", ...]` explicitly and owns its quoting.

        `new_process_group` puts the command in a session of its own, which
        makes the stop reach everything it spawned rather than just the
        process itself.

        After exit from the scope, `get_exit_code` and
        the buffered outputs stay readable. The scope object is single-use:
        entering it twice raises `RuntimeError`. Exiting never suppresses an
        exception. Only call it on the ER's event loop. The scope, not the
        handler run, owns the process: background work that outlives a run
        holds the scope open in its own task, and the ER ends it at stop.

        Raises (from the scope's entry, never from this call):
            TypeError: `cmd` is a `str` (a shell command line is rejected on
                purpose), not a `list`/`tuple`, or has a non-`str` element.
            ValueError: `cmd` is empty.
            UnsafeBatchArgumentError: an argument to a Windows `.cmd`/`.bat`
                shim would be reinterpreted by cmd.exe.
            UnlaunchableProgramError: a bare program name on Windows resolves
                to a file the OS cannot launch.
            ProgramStartError: the OS could not start the program (for
                example, it does not exist).
            OwnedProcessNotStoppedError: the body completed normally but the
                owned group was still alive at the bound.
        """
        ...

    async def run_to_completion(
        self,
        cmd: Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> IAsyncProcess:
        """Run a command as one unit of work and return its finished process.

        Writes `input` to stdin when given, always closes stdin, waits for the
        end of the process (at most `timeout` seconds), and returns the
        finished process carrying its exit code and outputs. There is no
        teardown code to write: the scope owns the process while it runs.

        A nonzero exit is returned, not raised. `TimeoutError` (from
        `timeout`) and drain errors are raised after the scope exits, with the
        process already stopped. `ProcessOutputIncompleteError` is raised
        from output reads when a stream was truncated or abandoned.

        There is no grace parameter: the scope stops what is still alive on a
        fixed bounded ladder. Only call this on the ER's event loop.

        Raises:
            TypeError: `cmd` is a `str`, not a `list`/`tuple`, or has a
                non-`str` element.
            ValueError: `cmd` is empty.
            UnsafeBatchArgumentError: an argument to a Windows `.cmd`/`.bat`
                shim would be reinterpreted by cmd.exe.
            UnlaunchableProgramError: a bare program name on Windows resolves
                to a file the OS cannot launch.
            ProgramStartError: the OS could not start the program (for
                example, it does not exist).
        """
        ...

    def run_sync(
        self, cmd: Argv, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> ISyncProcess:
        """Spawn a command from an argv vector and block on it. See `run`.

        Same contract as `run` — one argv element per argument, no shell, the
        same refusals and `ProgramStartError` on an unstartable program —
        minus the process-group option.
        """
        ...

    async def start_long_running(
        self,
        cmd: Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> IAsyncProcess:
        """Spawn a command as a process whose lifetime is not a unit of work.

        For a server, an interactive agent, or any child that outlives the
        request that started it: spawns and returns immediately. Takes no
        work slot and no local-cap permit, and the caller owns the lifetime
        outright — nothing is stopped at any scope exit, and the slot and
        permit are never taken. CPU-heavy requests sent to it take
        IWorkSlots.acquire().

        Output keeps prefix semantics: `get_output` returns whatever was
        accumulated even after a drain failure, and never raises
        `ProcessOutputIncompleteError`.

        Raises the same argv errors and `CommandStartError` subtypes as
        `run_to_completion`.
        """
        ...
