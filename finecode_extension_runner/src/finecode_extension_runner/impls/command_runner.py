import asyncio
import asyncio.subprocess
import dataclasses
import os
import shlex
import signal
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

from finecode_extension_api.interfaces import icommandrunner, ilogger

from finecode_extension_runner.concurrency import (
    ConcurrencyDecision,
    default_layered_concurrency,
    machine_subprocess_budget,
)

# Per-line ceiling for the subprocess stream readers, well above asyncio's 64 KiB
# default. A reader that overruns its limit does not merely raise -- it discards
# the buffered bytes first (`StreamReader.readline` turns `LimitOverrunError`
# into `ValueError` after clearing the buffer), so an undersized limit loses data
# rather than reporting a problem. Line-framed JSON protocols routinely carry
# payloads past 64 KiB, so the limit is raised to a size no realistic line hits.
# It is a high-water mark, not an allocation.
_STREAM_LINE_LIMIT = 8 * 1024 * 1024

_POSIX = os.name == "posix"
"""Process groups are a POSIX concept: `start_new_session`, `killpg` and
`getpgid` all exist only there, so `new_process_group` degrades to signalling
the process alone elsewhere rather than failing at spawn time."""

_TERMINATE_SIGNAL = signal.SIGTERM
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
"""`SIGKILL` does not exist off POSIX. Falling back to `SIGTERM` there keeps the
escalation ladder callable everywhere; it just cannot promise as much."""


def _strip_eol(line: str) -> str:
    # Only `\n` terminates a line here, because that is the only thing
    # `readline` splits on -- so a `\r` is part of the terminator only when it
    # is the one directly before that `\n`. A trailing `\r` with no `\n` after
    # it is data (a progress redraw, say), and stripping it would both lose a
    # byte and make subscribers disagree with `text()`, which keeps it verbatim.
    if line.endswith("\n"):
        return line[:-1].removesuffix("\r")
    return line


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """Mark a finished task's exception as retrieved, discarding it.

    The drain task can end in an exception nobody ever awaits: a streaming
    caller consumes `stdout_lines()` and has no reason to call `wait_for_end()`
    afterwards, and the iterator is already its error channel. Left unretrieved,
    asyncio logs "Task exception was never retrieved" with a full traceback when
    the task is collected, which looks like an unhandled crash.
    """
    if not task.cancelled():
        task.exception()


class _LineStream:
    """One of a process's output streams, pumped into memory as it arrives.

    Every stream is drained unconditionally from spawn, whether or not anyone
    reads it: leaving one undrained lets its OS pipe buffer fill and blocks the
    child forever, which is easy to hit when a caller streams stdout and ignores
    a chatty stderr.

    Drained lines accumulate until someone subscribes, at which point they are
    handed to the subscriber and accumulation stops. So a short-lived process
    nobody subscribes to behaves exactly as before -- full output available after
    it exits -- while a long-lived one being streamed does not grow a buffer that
    no one will ever read.

    Lines are held verbatim, newline included, so `text()` reproduces what the
    process actually wrote; only subscribers see them stripped.

    A stream that hits an unrecoverable error keeps draining and discarding
    instead of stopping. Stopping would leave the child blocked on a pipe
    nobody empties, so the failure of one stream would turn into a hang of the
    whole process; the error is recorded and reported once the stream really
    ends.
    """

    def __init__(self, reader: asyncio.StreamReader | None) -> None:
        self._reader = reader
        self._accumulated: list[str] = []
        self._queue: asyncio.Queue[str | None] | None = None
        self._subscribed = False
        self._done = reader is None
        self._failure_cause: Exception | None = None
        self._failure_message = ""

    async def pump(self) -> None:
        if self._reader is None:
            return

        try:
            while True:
                try:
                    raw_line = await self._reader.readline()
                except ValueError as error:
                    # Only raised on limit overrun, and the buffer is already
                    # gone by the time it surfaces, so the stream cannot be
                    # resynchronised. Recorded rather than swallowed: a
                    # truncated stream that simply stops looks exactly like a
                    # clean one to whoever is reading it.
                    self._fail(
                        f"Process output line exceeded {_STREAM_LINE_LIMIT} "
                        "bytes; the stream is truncated and cannot be recovered",
                        error,
                    )
                    continue

                if not raw_line:
                    return

                try:
                    # Split on bytes and decode after. `str.splitlines()` would
                    # also break on \x0b, \x0c, \x1c-\x1e, \x85, \u2028 and
                    # \u2029 -- all legal inside a JSON string, so splitting a
                    # decoded payload would silently cut lines in half.
                    line = raw_line.decode()
                except UnicodeDecodeError as error:
                    # Recorded for the same reason as an overrun, and easy to
                    # miss: `decode()` sits outside the `readline` guard, so
                    # without this the exception would escape to `_finish` and
                    # hand subscribers an ordinary end-of-stream.
                    self._fail(
                        "Process output is not valid UTF-8; the stream is "
                        "incomplete and cannot be recovered",
                        error,
                    )
                    continue

                self._emit(line)
        finally:
            self._finish()

    def _fail(self, message: str, cause: Exception) -> None:
        # First failure wins: it is the one that explains where the stream
        # stopped being trustworthy, and everything after it is a consequence.
        if self._failure_cause is None:
            self._failure_cause = cause
            self._failure_message = message

    def failure(self) -> RuntimeError | None:
        """The stream's unrecoverable error, as a fresh exception, or None.

        Fresh per call on purpose. Raising one shared instance from both the
        drain task and a subscriber lets `__traceback__` accumulate frames from
        both, producing a spliced traceback that reads as if the subscriber had
        caused the drain to fail.
        """
        if self._failure_cause is None:
            return None
        error = RuntimeError(self._failure_message)
        error.__cause__ = self._failure_cause
        return error

    def _emit(self, line: str) -> None:
        if self._failure_cause is not None:
            # Past the failure point the stream has lost sync -- what `readline`
            # returns next is the tail of whatever overran, not a line. Keep
            # draining so the child does not block, but hand the fragments to
            # nobody.
            return

        if self._queue is not None:
            self._queue.put_nowait(_strip_eol(line))
        elif not self._subscribed:
            self._accumulated.append(line)
        # else: there was a subscriber and it walked away. Accumulating again
        # would rebuild exactly the unbounded buffer that subscribing exists to
        # avoid, and `text()` refuses to serve it anyway.

    def _finish(self) -> None:
        self._done = True
        if self._queue is not None:
            self._queue.put_nowait(None)

    def subscribe(self) -> AsyncIterator[str]:
        if self._subscribed:
            raise RuntimeError(
                "Process stream is already being consumed; only one subscriber "
                "per stream is supported"
            )
        self._subscribed = True

        # Hand over whatever the pump collected before this call, then stop
        # accumulating. Without the replay, everything produced between spawn
        # and subscription would be dropped -- which for a line-framed protocol
        # means losing the opening messages.
        backlog = [_strip_eol(line) for line in self._accumulated]
        self._accumulated = []
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._queue = queue
        if self._done:
            # The pump already finished, so nothing else will ever close the
            # queue and the iterator would hang after replaying the backlog.
            queue.put_nowait(None)

        return self._iterate(backlog, queue)

    async def _iterate(
        self, backlog: list[str], queue: asyncio.Queue[str | None]
    ) -> AsyncIterator[str]:
        try:
            for line in backlog:
                yield line

            while True:
                line = await queue.get()
                if line is None:
                    failure = self.failure()
                    if failure is not None:
                        raise failure
                    return
                yield line
        finally:
            # Detach the queue however this ends -- including a consumer that
            # breaks out of the loop and lets the generator be closed. Without
            # this, `_emit` would keep filling a queue with no reader for the
            # rest of the process's life, and `text()` refuses to serve what
            # piled up, so it would be unreachable as well as unbounded.
            if self._queue is queue:
                self._queue = None

    def text(self) -> str:
        if self._subscribed:
            raise RuntimeError(
                "Process output was consumed by a subscriber; it is not "
                "accumulated once streaming starts"
            )
        return "".join(self._accumulated)


class AsyncProcess(icommandrunner.IAsyncProcess):
    def __init__(
        self,
        async_subprocess: asyncio.subprocess.Process,
        *,
        owns_process_group: bool = False,
    ):
        self.async_subprocess = async_subprocess
        self._owns_process_group = owns_process_group
        # Recorded now rather than looked up later. `start_new_session` makes
        # the spawned shell the group leader, so the group id is its pid -- and
        # `os.getpgid()` would stop answering the moment that leader exits,
        # which is exactly when the surviving children still need signalling.
        self._process_group_id = async_subprocess.pid

        self._stdout = _LineStream(async_subprocess.stdout)
        self._stderr = _LineStream(async_subprocess.stderr)
        # Strong reference, for the same reason `CommandRunner._release_tasks`
        # holds them: the event loop keeps only weak ones, so a drain that
        # nobody references can be collected mid-run and the streams would just
        # stop being read.
        self._drain = asyncio.create_task(self._drain_until_exit())
        self._drain.add_done_callback(_consume_task_exception)

    async def _drain_until_exit(self) -> None:
        # Both streams unconditionally, then the process. Draining is not
        # conditional on anyone wanting the output: an unread pipe fills its OS
        # buffer and blocks the child.
        #
        # Neither pump raises -- a stream that fails records the error and
        # keeps draining -- so both are always complete by the time the failure
        # is reported here. That is what lets a handler read `get_error_output()`
        # for the diagnostic after a stdout failure: stopping at the first
        # exception would surface it while stderr was still mid-drain, and the
        # message it wanted would not have arrived yet.
        await asyncio.gather(self._stdout.pump(), self._stderr.pump())
        await self.async_subprocess.wait()

        failure = self._stdout.failure() or self._stderr.failure()
        if failure is not None:
            raise failure

    async def wait_for_end(self, timeout: float | None = None) -> None:
        # Deliberately not `communicate()`: it wants sole ownership of both
        # readers, which the pumps already have. Waiting for the drain rather
        # than just the process is what makes the output complete on return --
        # a process can exit while bytes are still buffered in its pipes.
        #
        # Shielded so a timeout here does not cancel the drain. `wait_for`
        # cancels what it is waiting on, which would stop the pumps for good and
        # leave a caller that retries or reads later with a silently truncated
        # stream.
        #
        # `communicate(input=None)` also leaves stdin open (asyncio takes its
        # `_noop()` path), so not closing it here preserves the previous
        # behaviour for children that read stdin.
        await asyncio.wait_for(asyncio.shield(self._drain), timeout=timeout)

    def get_exit_code(self) -> int | None:
        return self.async_subprocess.returncode

    def get_output(self) -> str:
        return self._stdout.text()

    def get_error_output(self) -> str:
        return self._stderr.text()

    def stdout_lines(self) -> AsyncIterator[str]:
        return self._stdout.subscribe()

    def stderr_lines(self) -> AsyncIterator[str]:
        return self._stderr.subscribe()

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

    def is_alive(self) -> bool:
        if not self._owns_process_group:
            return self.async_subprocess.returncode is None

        # The group, not the shell's exit code. A shell that forks rather than
        # execs exits as soon as it is signalled, reporting a returncode, while
        # the command it started -- which may be ignoring that signal -- keeps
        # running in the same group. Asking the group is the only way to tell
        # "gone" from "the wrapper is gone".
        try:
            os.killpg(self._process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Something in the group is alive, just not ours to signal.
            return True
        return True

    def terminate(self) -> None:
        self._signal(_TERMINATE_SIGNAL)

    def kill(self) -> None:
        self._signal(_KILL_SIGNAL)

    def _signal(self, sig: signal.Signals) -> None:
        # An already-gone target is the state the caller is asking for, so both
        # the early return and the `ProcessLookupError` below are successes
        # rather than problems: an escalation ladder (terminate, wait, kill)
        # necessarily races the exit it is hoping for, and losing that race is
        # the good outcome.
        if not self.is_alive():
            return

        try:
            if self._owns_process_group:
                os.killpg(self._process_group_id, sig)
            else:
                self.async_subprocess.send_signal(sig)
        except ProcessLookupError:
            return


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
        # Keyed off `_stdout` rather than `returncode`, which is set by any
        # `poll()`/`wait()` and so could report the process as finished while
        # `communicate()` has not run -- returning None from a `-> str` method.
        return self._stdout if self._stdout is not None else ""

    def get_error_output(self) -> str:
        return self._stderr if self._stderr is not None else ""

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
        # Strong references to the in-flight release tasks. Without them the
        # event loop keeps only a weak reference, so a release task can be
        # garbage-collected before the process exits — permanently losing one
        # semaphore slot, and eventually wedging the runner at the cap.
        self._release_tasks: set[asyncio.Task[None]] = set()

    async def run(
        self,
        cmd: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
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
        owns_process_group = new_process_group and _POSIX
        try:
            # TODO: investigate why it works only with shell, not exec
            async_subprocess = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=_STREAM_LINE_LIMIT,
                start_new_session=owns_process_group,
            )
        except BaseException:
            self._semaphore.release()
            raise
        release_task = asyncio.create_task(self._release_when_done(async_subprocess))
        self._release_tasks.add(release_task)
        release_task.add_done_callback(self._release_tasks.discard)
        return AsyncProcess(
            async_subprocess=async_subprocess, owns_process_group=owns_process_group
        )

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
