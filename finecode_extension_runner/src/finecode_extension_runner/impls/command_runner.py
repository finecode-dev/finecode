from __future__ import annotations

import asyncio
import asyncio.subprocess
import contextlib
import dataclasses
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from finecode_extension_api.interfaces import icommandrunner, ilogger
from loguru import logger

from finecode_extension_runner.concurrency import ConcurrencyDecision
from finecode_extension_runner.owned_commands import (
    OwnedCommandEntry,
    OwnedCommandRegistry,
    get_owned_commands,
)
from finecode_extension_runner.process_slots import ProcessSlots

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

_WINDOWS = sys.platform == "win32"
_TERMINATE_SIGNAL = signal.SIGTERM
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
"""`SIGKILL` does not exist off POSIX. Falling back to `SIGTERM` there keeps the
escalation ladder callable everywhere; it just cannot promise as much."""

# Bounded waits for owned-scope cleanup. The drain and the
# release run concurrently after the group is gone, so the worst-case exit is
# grace + gone + max(drain, release) = 6.0 s, under the WM's 10 s stop wait.
_STOP_GRACE_SEC = 2.0
_GONE_WAIT_SEC = 2.0
_DRAIN_WAIT_SEC = 1.0
_RELEASE_WAIT_SEC = 2.0
_POLL_SEC = 0.05

_BATCH_SUFFIXES = (".cmd", ".bat")
_LAUNCHABLE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")
_BATCH_UNSAFE = frozenset('"%^&|<>!()\r\n')
_BATCH_UNSAFE_ALWAYS = frozenset('"%\r\n')


def _prepare_argv(
    cmd: icommandrunner.Argv,
    env: dict[str, str] | None,
    *,
    platform: str,
    which: Callable[..., str | None],
) -> list[str]:
    """Resolve a bare program name and refuse batch-target arguments cmd.exe
    would reinterpret (Windows only).

    CreateProcess appends only `.exe` and ignores `PATHEXT`, so a bare name
    like `npm` or `pi` (a `.cmd` shim) is not found without resolution; and a
    resolved `.cmd` is still run through cmd.exe, which expands and interprets
    characters that `subprocess.list2cmdline` does not escape for it. Such
    arguments are refused rather than escaped — a cmd.exe quoter would
    reintroduce the shell-parsing hazard (ADR-0099). POSIX is untouched:
    `execvpe` searches the environment's `PATH` directly.

    Raises:
        TypeError: `cmd` is not a list/tuple of `str`.
        ValueError: `cmd` is empty.
        UnsafeBatchArgumentError: a batch target would reinterpret an argument.
        UnlaunchableProgramError: a resolved program cannot be launched.
    """
    icommandrunner.check_argv(cmd)
    argv = list(cmd)
    if platform != "win32":
        return argv
    program = argv[0]
    if "\\" not in program and "/" not in program:
        source = env if env is not None else os.environ
        path = next((v for k, v in source.items() if k.upper() == "PATH"), None)
        resolved = which(program, path=path)
        if resolved is not None:
            if not resolved.lower().endswith(_LAUNCHABLE_SUFFIXES):
                raise icommandrunner.UnlaunchableProgramError(
                    f"{program} resolves to {resolved}, which CreateProcess "
                    f"cannot launch (expected one of {_LAUNCHABLE_SUFFIXES})"
                )
            argv[0] = program = resolved
    if program.lower().endswith(_BATCH_SUFFIXES):
        quoted = " " in program or "\t" in program  # list2cmdline quotes it
        path_unsafe = _BATCH_UNSAFE_ALWAYS if quoted else _BATCH_UNSAFE
        for index, arg in enumerate(argv):
            unsafe = path_unsafe if index == 0 else _BATCH_UNSAFE
            if unsafe.intersection(arg):
                raise icommandrunner.UnsafeBatchArgumentError(
                    f"argument {index} cannot be passed safely to batch file "
                    f"{program}: {arg!r}"
                )
    return argv


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

    A stream that overruns the reader limit keeps draining and discarding
    instead of stopping. Stopping would leave the child blocked on a pipe
    nobody empties, so the failure of one stream would turn into a hang of the
    whole process; the error is recorded and reported once the stream really
    ends. Undecodable bytes are not such an error: they decode with
    replacement, so a child writing in another encoding costs a few U+FFFD
    characters rather than the stream.
    """

    def __init__(
        self, reader: asyncio.StreamReader | None, *, stream_name: str = "stdout"
    ) -> None:
        self._reader = reader
        self._stream_name = stream_name
        self._accumulated: list[str] = []
        self._queue: asyncio.Queue[str | None] | None = None
        self._subscribed = False
        self._done = reader is None
        self._abandoned = False
        self._failure_cause: Exception | None = None
        self._failure_message = ""

    def abandon(self) -> None:
        """Stop serving this stream; subscribers end with an incomplete error."""
        self._abandoned = True
        if self._queue is not None:
            self._queue.put_nowait(None)

    @property
    def abandoned(self) -> bool:
        return self._abandoned

    def incomplete_error(self) -> icommandrunner.ProcessOutputIncompleteError:
        if self._failure_cause is not None:
            reason = self._failure_message
        else:
            reason = "the drain was abandoned before the stream ended"
        name = (
            self._stream_name if self._stream_name in ("stdout", "stderr") else "stdout"
        )
        return icommandrunner.ProcessOutputIncompleteError(name, reason)

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

                # Split on bytes and decode after. `str.splitlines()` would
                # also break on \x0b, \x0c, \x1c-\x1e, \x85, \u2028 and
                # \u2029 -- all legal inside a JSON string, so splitting a
                # decoded payload would silently cut lines in half.
                # Undecodable bytes decode with replacement rather than
                # failing the stream: no consumer derives a run's verdict
                # from raw stdout bytes, so killing the whole run over one
                # console-encoded line is pure collateral damage.
                line = raw_line.decode("utf-8", errors="replace")
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
                    if self._abandoned:
                        raise self.incomplete_error()
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
        strict_output: bool = False,
    ):
        self.async_subprocess = async_subprocess
        self._owns_process_group = owns_process_group
        # Recorded now rather than looked up later. `start_new_session` makes
        # the spawned process the group leader, so the group id is its pid --
        # and `os.getpgid()` would stop answering the moment that leader
        # exits, which is exactly when the surviving children still need
        # signalling. The command may itself be a launcher that forks -- npm,
        # a `.cmd` shim, a driver script -- so the group keeps existing while
        # those live.
        self._process_group_id = async_subprocess.pid
        self._strict_output = strict_output
        self._group_gone_latched = False

        self._stdout = _LineStream(async_subprocess.stdout, stream_name="stdout")
        self._stderr = _LineStream(async_subprocess.stderr, stream_name="stderr")
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

    async def wait_for_leader_exit(self) -> None:
        """Wait for the leader's exit event, independent of pipe EOF."""
        while self.async_subprocess.returncode is None:
            await asyncio.sleep(0.05)

    def group_survivors(self) -> tuple[int, ...]:
        """Live non-zombie members of the owned group; empty means gone."""
        if not self._owns_process_group or _WINDOWS:
            return () if self.async_subprocess.returncode is not None else ()
        if self._group_gone_latched:
            return ()
        try:
            os.killpg(self._process_group_id, 0)
        except ProcessLookupError:
            self._group_gone_latched = True
            return ()
        except PermissionError:
            pass
        try:
            import psutil
        except ImportError:
            pass
        else:
            try:
                survivors: list[int] = []
                for proc in psutil.process_iter(["pid", "status"]):
                    try:
                        if os.getpgid(proc.pid) != self._process_group_id:
                            continue
                    except (ProcessLookupError, PermissionError, OSError):
                        continue
                    try:
                        if proc.status() == psutil.STATUS_ZOMBIE:
                            continue
                    except (
                        psutil.NoSuchProcess,
                        psutil.ZombieProcess,
                        psutil.AccessDenied,
                    ):
                        continue
                    survivors.append(proc.pid)
                if survivors:
                    return tuple(survivors)
            except (OSError, psutil.Error) as error:
                logger.debug(
                    f"Group scan failed; falling back to killpg probe: {error}"
                )
        try:
            os.killpg(self._process_group_id, 0)
        except ProcessLookupError:
            self._group_gone_latched = True
            return ()
        except PermissionError:
            return (self._process_group_id,)
        # killpg succeeded but the scan found nothing (zombie-only group or a
        # scan miss): zombies need no signal, so the group counts as gone.
        self._group_gone_latched = True
        return ()

    def abandon_drain(self) -> None:
        """Stop pumping; later output reads report incompleteness."""
        self._stdout.abandon()
        self._stderr.abandon()
        if not self._drain.done():
            _ = self._drain.cancel()

    @property
    def drain_abandoned(self) -> bool:
        return self._stdout.abandoned or self._stderr.abandoned

    def _strict_check(self, stream: _LineStream) -> None:
        if not self._strict_output:
            return
        if stream.failure() is not None or stream.abandoned:
            raise stream.incomplete_error()

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
        try:
            await asyncio.wait_for(asyncio.shield(self._drain), timeout=timeout)
        except asyncio.CancelledError:
            if self.drain_abandoned:
                raise self._stdout.incomplete_error() from None
            raise

    def get_exit_code(self) -> int | None:
        return self.async_subprocess.returncode

    def get_output(self) -> str:
        self._strict_check(self._stdout)
        return self._stdout.text()

    def get_error_output(self) -> str:
        self._strict_check(self._stderr)
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
        if self._group_gone_latched:
            return False

        # The group, not the process's own exit code. The command may be a
        # launcher that forks rather than execs: it exits as soon as it is
        # signalled, reporting a returncode, while what it started -- which may
        # be ignoring that signal -- keeps running in the same group. Asking
        # the group is the only way to tell "gone" from "the wrapper is gone".
        try:
            os.killpg(self._process_group_id, 0)
        except ProcessLookupError:
            self._group_gone_latched = True
            return False
        except PermissionError:
            # Something in the group is alive, just not ours to signal.
            return True
        return True

    def terminate(self) -> None:
        self._signal(_TERMINATE_SIGNAL)

    def kill(self) -> None:
        self._signal(_KILL_SIGNAL)

    def _kill_tree(self) -> None:
        # Windows-only: there are no process groups, so a direct signal stops
        # the child but orphans everything it spawned.
        # The descendant snapshot is walked
        # leaves-first, root last; an already-exited target (`NoSuchProcess`) is
        # the state the caller is asking for, so it is success. Anything else
        # logs a warning and never raises: the handler ladders call
        # `terminate()`/`kill()` under `contextlib.suppress`, and a teardown
        # error must not replace the original outcome.
        #
        # Known limitation: `children(recursive=True)` is a snapshot, so
        # detached/doubly-forked grandchildren can survive -- the same class of
        # gap POSIX `killpg` has for `setsid` children.
        import psutil

        try:
            root = psutil.Process(self.async_subprocess.pid)
        except psutil.NoSuchProcess:
            return
        try:
            tree = [*reversed(root.children(recursive=True)), root]
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            # The root exited mid-walk; there is no tree left to walk from here.
            return
        for target in tree:
            try:
                target.kill()
            except psutil.NoSuchProcess:
                pass
            except Exception as error:
                logger.warning(
                    f"Failed to kill descendant process {target.pid}: {error}"
                )

    def _signal(self, sig: signal.Signals) -> None:
        # An already-gone target is the state the caller is asking for, so both
        # the early return and the `ProcessLookupError` below are successes
        # rather than problems: an escalation ladder (terminate, wait, kill)
        # necessarily races the exit it is hoping for, and losing that race is
        # the good outcome. A group that `is_alive()` reports as alive only
        # because of EPERM cannot be signalled, and that is also not a failure.
        if not self.is_alive():
            return

        if _WINDOWS:
            # Both rungs, not just kill: the handler ladders exit at terminate
            # on Windows (a Windows process cannot ignore `TerminateProcess`),
            # so a kill-only tree kill would never run -- and there is no
            # graceful/forced distinction to preserve there anyway.
            self._kill_tree()
            return

        try:
            if self._owns_process_group:
                os.killpg(self._process_group_id, sig)
            else:
                self.async_subprocess.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            return


class SyncProcess(icommandrunner.ISyncProcess):
    def __init__(self, popen: subprocess.Popen):
        self.popen = popen
        self._stdout: str | None = None
        self._stderr: str | None = None

    def wait_for_end(self, timeout: float | None = None) -> None:
        stdout, stderr = self.popen.communicate(timeout=timeout)
        self._stdout = stdout.decode("utf-8", errors="replace")
        self._stderr = stderr.decode("utf-8", errors="replace")

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
) -> ConcurrencyDecision | None:
    """Optional local ceiling for one ER's `CommandRunner`, with the reason it
    was picked (for logging — see `ConcurrencyDecision`).

    ``config.max_concurrent_processes`` (service config, if set and positive)
    is kept as an *additional* ceiling on top of the WM-leased process gate
    (ADR-0090); when unset there is no local cap and the shared gate is the
    only bound.  It has no env var of its own — it's delivered as service
    config (see ADR-0056), which already has a machine-local override path
    via a personal `finecode-user.toml`.
    """
    if configured_value is not None:
        return ConcurrencyDecision(
            max(configured_value, 1), "service config max_concurrent_processes"
        )
    return None


@dataclasses.dataclass
class CommandRunnerConfig:
    max_concurrent_processes: int | None = None


class _OwnedCommandScope:
    """The scope that owns one bounded process.

    Entered once: entry takes argv refusals, the loop check, the registry
    slot, the work slot, the local-cap permit and the spawn, in that order.
    Exit stops what is still alive, waits a bounded time,
    releases the slot and permit, and propagates.
    """

    def __init__(
        self,
        runner: CommandRunner,
        cmd: icommandrunner.Argv,
        cwd: Path | None,
        env: dict[str, str] | None,
        new_process_group: bool,
    ) -> None:
        self._runner = runner
        self._cmd = cmd
        self._cwd = cwd
        self._env = env
        self._new_process_group = new_process_group
        self._entered = False
        self._entry: OwnedCommandEntry | None = None
        self._process: AsyncProcess | None = None
        self._lease_id: str | None = None
        self._cleanup_cancelled = False

    async def __aenter__(self) -> AsyncProcess:
        if self._entered:
            raise RuntimeError("An owned command scope may only be entered once")
        self._entered = True
        runner = self._runner
        argv = _prepare_argv(
            self._cmd,
            self._env,
            platform=sys.platform,
            which=shutil.which,
        )
        log_msg = f"Async subprocess run: {argv!r}"
        if self._cwd is not None:
            log_msg += f" in {self._cwd}"
        runner.logger.debug(log_msg)
        runner._owned_commands.check_loop()
        holder = asyncio.current_task()
        assert holder is not None
        entry = runner._owned_commands.register(holder)
        self._entry = entry
        try:
            lease_id = await runner._process_slots.acquire()
        except BaseException:
            runner._owned_commands.complete(entry)
            raise
        self._lease_id = lease_id
        holds_cap = False
        try:
            if runner._local_cap is not None:
                await runner._local_cap.acquire()
                holds_cap = True
            process, cancelled = await self._spawn_guarded(argv, self._cwd, self._env)
        except BaseException:
            if holds_cap:
                assert runner._local_cap is not None
                runner._local_cap.release()
            runner._process_slots.release_in_background(lease_id)
            runner._owned_commands.complete(entry)
            raise
        entry.pid = process.async_subprocess.pid
        self._process = process
        if cancelled:
            await self._stop_after_spawn(process, entry, lease_id, holds_cap, runner)
            raise asyncio.CancelledError()
        return process

    async def _spawn_guarded(
        self, argv: list[str], cwd: Path | None, env: dict[str, str] | None
    ) -> tuple[AsyncProcess, bool]:
        """Spawn without letting an outer cancellation inside the spawn.

        `create_subprocess_exec`'s cancel path kills only the leader and then
        awaits pipes a grandchild may hold, and the pid never reaches the
        caller. So the spawn runs as its own task awaited through `shield`,
        re-awaited after each cancellation. Returns the process and whether
        the entry was cancelled while it was in flight.
        """
        runner = self._runner
        owns_group = self._new_process_group and _POSIX
        spawn_task: asyncio.Task[AsyncProcess] = asyncio.get_running_loop().create_task(
            runner._spawn(argv, cwd, env, owns_group, strict_output=True)
        )
        cancelled = False
        while not spawn_task.done():
            try:
                await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                cancelled = True
        if spawn_task.cancelled():
            raise asyncio.CancelledError()
        error = spawn_task.exception()
        if error is not None:
            if cancelled:
                raise asyncio.CancelledError() from error
            raise error
        return spawn_task.result(), cancelled

    async def _stop_after_spawn(
        self,
        process: AsyncProcess,
        entry: OwnedCommandEntry,
        lease_id: str | None,
        holds_cap: bool,
        runner: CommandRunner,
    ) -> None:
        """Stop a process whose spawn outlived an entry cancellation."""
        await self._stop_process(process)
        if runner._local_cap is not None and holds_cap:
            runner._local_cap.release()
        if await self._confirm_gone(process):
            runner._process_slots.release_in_background(lease_id)
        else:
            survivors = process.group_survivors()
            logger.warning(
                f"Owned command entry cancelled but group still alive: {survivors}"
            )
            watcher: asyncio.Task[None] = asyncio.get_running_loop().create_task(
                self._watch_and_release(process, lease_id, runner=runner)
            )
            runner._owned_commands.keep(
                watcher, f"entry-failure watcher pid={entry.pid}"
            )
        runner._owned_commands.complete(entry)

    async def __aexit__(
        self, exc_type: object, exc: BaseException | None, tb: object
    ) -> bool:
        runner = self._runner
        entry = self._entry
        assert entry is not None
        process = self._process
        assert process is not None
        cleanup_task: asyncio.Task[
            tuple[icommandrunner.OwnedProcessNotStoppedError | None, list[str]]
        ] = asyncio.get_running_loop().create_task(self._cleanup(process))
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                self._cleanup_cancelled = True
        if cleanup_task.cancelled():
            self._cleanup_cancelled = True
            exit_error: OwnedProcessNotStoppedError | None = None
            notes: list[str] = []
        else:
            error = cleanup_task.exception()
            if error is not None:
                raise error
            exit_error, notes = cleanup_task.result()
        runner._owned_commands.complete(entry)
        if self._cleanup_cancelled:
            if isinstance(exc, asyncio.CancelledError):
                for note in notes:
                    exc.add_note(note)
                raise exc
            propagated = asyncio.CancelledError()
            propagated.__context__ = exc if exc is not None else exit_error
            for note in notes:
                propagated.add_note(note)
            raise propagated
        if exc is not None:
            if exit_error is not None:
                logger.warning(f"Owned command group not stopped: {exit_error}")
                exc.add_note(str(exit_error))
            for note in notes:
                exc.add_note(note)
            return False
        if exit_error is not None:
            raise exit_error
        return False

    async def _cleanup(
        self, process: AsyncProcess
    ) -> tuple[OwnedProcessNotStoppedError | None, list[str]]:
        """Stop the process, drain and release; absorb cancellations, stay bounded."""
        runner = self._runner
        await self._stop_process(process)
        gone = await self._confirm_gone(process)
        with contextlib.suppress(RuntimeError):
            process.close_stdin()
        if runner._local_cap is not None:
            runner._local_cap.release()
        notes: list[str] = []
        if not gone:
            watcher: asyncio.Task[None] = asyncio.get_running_loop().create_task(
                self._watch_and_release(process, self._lease_id, runner=runner)
            )
            runner._owned_commands.keep(
                watcher, f"group watcher pid={process.async_subprocess.pid}"
            )
            return self._not_gone_result(process, notes)
        release_task: asyncio.Task[None] = asyncio.get_running_loop().create_task(
            runner._process_slots.release(self._lease_id)
        )
        loop = asyncio.get_running_loop()
        drain_deadline = loop.time() + _DRAIN_WAIT_SEC
        release_deadline = loop.time() + _RELEASE_WAIT_SEC
        release_kept = False
        drain_task = process._drain
        while True:
            if drain_task.done() and (release_task.done() or release_kept):
                break
            now = loop.time()
            if not drain_task.done() and now >= drain_deadline:
                process.abandon_drain()
            if not release_task.done() and not release_kept and now >= release_deadline:
                message = "Process budget release in flight; continuing in background"
                logger.warning(message)
                notes.append(message)
                runner._owned_commands.keep(
                    release_task, f"WM release pid={process.async_subprocess.pid}"
                )
                release_kept = True
            if drain_task.done() and (release_task.done() or release_kept):
                break
            try:
                await asyncio.sleep(_POLL_SEC)
            except asyncio.CancelledError:
                self._cleanup_cancelled = True
        return None, notes

    def _not_gone_result(
        self,
        process: AsyncProcess,
        notes: list[str],
    ) -> tuple[OwnedProcessNotStoppedError | None, list[str]]:
        survivors = process.group_survivors()
        message = f"Owned command group still alive after stop: {list(survivors)}"
        logger.warning(message)
        notes.append(message)
        return icommandrunner.OwnedProcessNotStoppedError(tuple(survivors)), notes

    async def _watch_and_release(
        self,
        process: AsyncProcess,
        lease_id: str | None,
        runner: CommandRunner,
    ) -> None:
        """Retry the stop ladder until the group is gone, then release."""
        while True:
            await self._stop_process(process)
            if await self._confirm_gone(process):
                break
            await asyncio.sleep(0.2)
        with contextlib.suppress(RuntimeError):
            process.close_stdin()
        try:
            await runner._process_slots.release(lease_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"Process budget release failed: {error}")

    async def _stop_process(self, process: AsyncProcess) -> None:
        """D5's ladder on the owned group (or leader), bounded, never shortened."""
        if not process.is_alive():
            return
        if _WINDOWS:
            process.terminate()
            await self._poll_until_gone(process, _GONE_WAIT_SEC)
            return
        process.terminate()
        if await self._poll_until_gone(process, _STOP_GRACE_SEC):
            return
        if not process.is_alive():
            return
        process.kill()
        await self._poll_until_gone(process, _GONE_WAIT_SEC)

    async def _poll_until_gone(self, process: AsyncProcess, bound: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + bound
        while True:
            if await self._confirm_gone(process):
                return True
            if loop.time() >= deadline:
                return False
            try:
                await asyncio.sleep(_POLL_SEC)
            except asyncio.CancelledError:
                self._cleanup_cancelled = True

    async def _confirm_gone(self, process: AsyncProcess) -> bool:
        if process._owns_process_group and _POSIX:
            return process.group_survivors() == ()
        if _WINDOWS:
            return process.async_subprocess.returncode is not None
        return not process.is_alive()


class CommandRunner(icommandrunner.ICommandRunner):
    def __init__(
        self,
        logger: ilogger.ILogger,
        config: CommandRunnerConfig,
        process_slots: ProcessSlots,
        owned_commands: OwnedCommandRegistry | None = None,
    ):
        self.logger = logger
        self._process_slots = process_slots
        self._owned_commands = (
            owned_commands if owned_commands is not None else get_owned_commands()
        )
        decision = resolve_command_runner_concurrency(config.max_concurrent_processes)
        if decision is not None:
            logger.info(
                f"Capping concurrent subprocesses to {decision.value} ({decision.source})"
            )
            self._local_cap = asyncio.Semaphore(decision.value)
        else:
            self._local_cap = None

    def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
    ) -> contextlib.AbstractAsyncContextManager[icommandrunner.IAsyncProcess]:
        """Spawn a command as a scoped unit of work; the scope owns the process.

        Entering the scope spawns the process; leaving it stops whatever is
        still alive on a bounded ladder (SIGTERM, then SIGKILL after a grace
        on the owned group, or one tree kill on Windows), waits a bounded
        time until the process or group is gone, releases the work slot and
        the local-cap permit, waits a bounded time for the WM release, and
        propagates under the body's exception precedence. Accepted costs: an
        escaped `setsid` descendant, a no-group descendant, Windows best
        effort, an unanswered WM release, unconfirmable survivors.

        Inside the scope, `is_alive`, `terminate`, `kill`, `wait_for_end` and
        the output reads behave as before; after exit, `get_exit_code` and
        the buffered outputs stay readable. The scope object is single-use:
        entering it twice raises `RuntimeError`. Exiting never suppresses an
        exception. Only call it on the ER's event loop. The scope, not the
        handler run, owns the process: background work that outlives a run
        holds the scope open in its own task, and the ER ends it at stop.

        `new_process_group` puts the command in a session of its own, so the
        stop reaches everything it spawned rather than just the leader.

        Raises (from the scope's entry, never from this call):
            TypeError: `cmd` is a `str`, not a `list`/`tuple`, or has a
                non-`str` element.
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
        return _OwnedCommandScope(self, cmd, cwd, env, new_process_group)

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> icommandrunner.IAsyncProcess:
        """Run a command as one unit of work and return its finished process."""
        async with self.run(
            cmd, cwd=cwd, env=env, new_process_group=new_process_group
        ) as process:
            if input is not None:
                process.write_to_stdin(input)
            process.close_stdin()
            await process.wait_for_end(timeout=timeout)
            return process

    async def start_long_running(
        self,
        cmd: icommandrunner.Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> icommandrunner.IAsyncProcess:
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
        """
        argv = _prepare_argv(
            cmd,
            env,
            platform=sys.platform,
            which=shutil.which,
        )
        log_msg = f"Async subprocess run: {argv!r}"
        if cwd is not None:
            log_msg += f" in {cwd}"
        self.logger.debug(log_msg)
        owns_process_group = new_process_group and _POSIX
        return await self._spawn(argv, cwd, env, owns_process_group)

    async def _spawn(
        self,
        argv: list[str],
        cwd: Path | None,
        env: dict[str, str] | None,
        owns_process_group: bool,
        strict_output: bool = False,
    ) -> AsyncProcess:
        try:
            async_subprocess = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=_STREAM_LINE_LIMIT,
                start_new_session=owns_process_group,
            )
        except OSError as error:
            raise icommandrunner.ProgramStartError(argv[0], cwd, error) from error
        return AsyncProcess(
            async_subprocess=async_subprocess,
            owns_process_group=owns_process_group,
            strict_output=strict_output,
        )

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> icommandrunner.ISyncProcess:
        argv = _prepare_argv(
            cmd,
            env,
            platform=sys.platform,
            which=shutil.which,
        )
        log_msg = f"Sync subprocess run: {argv!r}"
        if cwd is not None:
            log_msg += f" {cwd}"
        self.logger.debug(log_msg)
        try:
            async_subprocess = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except OSError as error:
            raise icommandrunner.ProgramStartError(argv[0], cwd, error) from error

        return SyncProcess(popen=async_subprocess)
