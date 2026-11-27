from __future__ import annotations

import asyncio
import collections.abc
import concurrent.futures
import contextlib
import dataclasses
import enum
import functools
import inspect
import json
import os
import re
import sys
import threading
import time
import traceback
import typing
import uuid
from pathlib import Path

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

import cattrs
import culsans
from loguru import logger

from finecode_jsonrpc import _io_thread, _spawn, error_codes
from finecode_jsonrpc._converter import converter as _converter
from finecode_jsonrpc._loop_event import LoopAwareEvent
from finecode_jsonrpc._proc_kill import force_kill_process_group
from finecode_jsonrpc._proc_snapshot import describe_spawned_processes

from ._frames import OutgoingFrame, ReceivedFrame
from ._hop_guard import call_guarded, guarded_handler_scope
from .cancel_reply import (
    AwaitReplyAfterCancel,
    ReplyAfterCancel,
    ReplyAfterCancelKind,
)
from .tracing import (
    CallerOutcome,
    HandlerOutcome,
    HopCloseReason,
    HopIdentity,
    HopStage,
    HopToken,
    IHopObserver,
    ITracingHooks,
    StageStatus,
    StageTime,
    capture_stage_time,
)


class QueueEnd:
    # just object() would not support multiprocessing, use class and compare by it
    @override
    def __eq__(self, other: object) -> bool:
        return self.__class__ == other.__class__


QUEUE_END = QueueEnd()


# JSON-RPC 2.0 Standard Error Codes
# See: https://www.jsonrpc.org/specification#error_object
class JsonRpcErrorCode:
    """Standard JSON-RPC 2.0 error codes.

    Values live in `error_codes`, which is their single home in this package.
    """

    PARSE_ERROR = error_codes.PARSE_ERROR
    INVALID_REQUEST = error_codes.INVALID_REQUEST
    METHOD_NOT_FOUND = error_codes.METHOD_NOT_FOUND
    INVALID_PARAMS = error_codes.INVALID_PARAMS
    INTERNAL_ERROR = error_codes.INTERNAL_ERROR


_P = typing.ParamSpec("_P")


@dataclasses.dataclass
class ResponseCompletion:
    """Per-request hook run once when the matching response is dispatched.

    ``capture`` freezes the subscribed prefix before the request future is
    resolved. A fault inside ``capture`` is recorded on ``error`` without
    replacing the received runner response/error.
    """

    capture: collections.abc.Callable[[], None]
    error: Exception | None = None


@dataclasses.dataclass
class OutputDrain:
    """Internal outgoing-queue marker proving writer drain of prior bytes.

    Never crosses a socket; resolved by the IO-loop sender after the
    preceding bytes have been written and drained.
    """

    completion: concurrent.futures.Future[None]


class WriterFromQueue:
    def __init__(self, out_queue: culsans.SyncQueue[typing.Any]) -> None:
        self._out_queue: typing.Final = out_queue

    def close(self) -> None:
        self._out_queue.put(QUEUE_END)

    def write(self, data: bytes | OutgoingFrame) -> None:
        self._out_queue.put(data)


class ServerFailedToStart(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: typing.Final = message


class ServerExitedBeforePort(Exception):
    def __init__(self, return_code: int | None) -> None:
        super().__init__()
        self.return_code: typing.Final = return_code


@dataclasses.dataclass
class StartupTimeline:
    """Monotonic timestamps of the milestones of a server's startup.

    Every field is ``time.monotonic()`` at the moment the milestone occurred, or
    ``None`` if it never did. Kept as raw timestamps rather than durations so a
    failure diagnostic can say how long ago the spawn was even after the wait
    that failed has ended.
    """

    spawned_at: float | None = None
    first_output_at: float | None = None
    port_line_at: float | None = None
    connected_at: float | None = None

    def describe(self, now: float) -> str:
        if self.spawned_at is None:
            return "not spawned"
        parts = [f"spawned {now - self.spawned_at:.1f}s ago"]
        if self.first_output_at is None:
            parts.append("no output from the server")
        else:
            parts.append(
                f"first output after {self.first_output_at - self.spawned_at:.1f}s"
            )
        if self.port_line_at is None:
            parts.append("no port line")
        else:
            parts.append(f"port line after {self.port_line_at - self.spawned_at:.1f}s")
        return "; ".join(parts)


class BaseRunnerRequestException(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: typing.Final = message


class NoResponse(BaseRunnerRequestException): ...


class InvalidResponse(BaseRunnerRequestException): ...


class ResponseTimeout(BaseRunnerRequestException): ...


@dataclasses.dataclass
class ResponseError:
    """https://www.jsonrpc.org/specification#error_object"""

    code: int
    """A number indicating the error type that occurred."""
    message: str
    """A string providing a short description of the error."""
    data: typing.Any | None = None
    """A primitive or structured value that contains additional information
    about the error. Can be omitted."""


class ErrorOnRequest(BaseRunnerRequestException):
    def __init__(self, error: ResponseError) -> None:
        super().__init__(message=f"Got error {error.code}: {error.message}")
        self.error = error


class CommunicationType(enum.Enum):
    TCP = enum.auto()
    STDIO = enum.auto()


def task_done_log_callback(future: asyncio.Future[typing.Any], task_id: str = ""):
    if future.cancelled():
        logger.debug(f"task cancelled: {task_id}")
    else:
        exc = future.exception()
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            logger.error(
                f"exception in task: {task_id} {type(exc)} {exc.message if hasattr(exc, 'message') else ''}"
            )
            logger.exception(exc)
            logger.error(tb)
        else:
            logger.trace(f"{task_id} done")


@dataclasses.dataclass
class _InboundRequest:
    """An incoming request being served, tracked for ``$/cancelRequest``."""

    method: str
    task: asyncio.Task[typing.Any]
    started: bool = False
    cancelled_before_start: bool = False
    cancelled_by_connection_loss: bool = False


class RequestCancelledError(asyncio.CancelledError):
    def __init__(
        self,
        request_id: int | str,
        *,
        reply: ReplyAfterCancel | None = None,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.reply = reply


class ServerStoppedError(BaseRunnerRequestException):
    """Raised on pending requests when the server process exits."""


@dataclasses.dataclass
class _ReaderHopState:
    """Hop context for the IO-thread reader: observer plus sync-request hops.

    The sync-hop map is shared with the sending side; entries are removed by
    the sync future's done-callback, so the reader only peeks.
    """

    observer: IHopObserver
    sync_hops: dict[str, HopToken]


class JsonRpcClient:
    CHARSET: typing.Final[str] = "utf-8"
    CONTENT_TYPE: typing.Final[str] = "application/vscode-jsonrpc"
    VERSION: typing.Final[str] = "2.0"

    def __init__(
        self,
        message_types: dict[str, typing.Any],
        readable_id: str,
        communication_type: CommunicationType = CommunicationType.TCP,
        tracing: ITracingHooks | None = None,
        request_cancelled_code: int = error_codes.DEFAULT_REQUEST_CANCELLED,
        hop_observer: IHopObserver | None = None,
        kill_descendant_groups: bool = False,
    ) -> None:
        self.server_process_stopped: typing.Final = LoopAwareEvent()
        # One-way latch: set by the first send_request timeout or by the inbound
        # queue closing. Once a channel that should answer within its timeout
        # has not, every later request fails immediately instead of waiting out
        # another timeout, and the recovery path can key on this to reap a
        # provably-dead runner.
        self.channel_failed: bool = False
        # Set as soon as the OS process is spawned (before the port handshake),
        # so a start attempt that later times out still has a handle to kill.
        self.pid: int | None = None
        # Set by a force_kill() that arrives before the spawned pid is known;
        # the spawn side checks it and kills the process itself then.
        self._kill_requested: bool = False
        # WM-side ER clients kill descendant groups too: an ER's owned
        # commands and start_long_running children run in sessions of their
        # own. Every other client keeps the default.
        self._kill_descendant_groups = kill_descendant_groups
        self.startup_timeline = StartupTimeline()
        self.server_exit_callback: (
            collections.abc.Callable[[], collections.abc.Coroutine] | None
        ) = None
        self.in_message_queue: typing.Final = culsans.Queue()
        self.out_message_queue: typing.Final = culsans.Queue()
        self.writer = WriterFromQueue(out_queue=self.out_message_queue.sync_q)
        self.message_types = message_types
        self.readable_id: str = readable_id
        self.communication_type = communication_type
        # Which error code an incoming request gets answered with when its
        # handler is cancelled. See error_codes.DEFAULT_REQUEST_CANCELLED for
        # why this is a parameter rather than a constant.
        self._request_cancelled_code = request_cancelled_code

        self._async_tasks: list[asyncio.Task[typing.Any]] = []
        self._stop_event: typing.Final = LoopAwareEvent()
        self._sync_request_futures: dict[str, concurrent.futures.Future] = {}
        self._async_request_futures: dict[str, asyncio.Future] = {}
        # Ids of requests whose awaiting task was cancelled. A reply arriving
        # for one of them was still sent by the peer, so it is dropped with a
        # DEBUG log instead of the ERROR for ids that were never awaited.
        # Entries are released when their reply arrives or when the channel
        # fails, so the set cannot grow without bound across a connection.
        self._cancelled_request_ids: set[str] = set()
        self._stderr_buffer: list[str] = []
        # The server writes its own diagnostics to stdout; before the RPC channel
        # exists (startup), er/logRecords forwarding cannot run, so we retain the
        # startup-window stdout here to surface it in a ServerFailedToStart diagnostic.
        self._stdout_buffer: list[str] = []
        self._expected_result_type_by_msg_id: dict[str, typing.Any] = {}

        self._tracing = tracing
        self._hop_observer = hop_observer
        self._connection_id: str = uuid.uuid4().hex
        # Open async caller hops by wire id. An entry outlives its future
        # after a timeout or cancellation so a late response can still be
        # matched; resolved entries are popped in handle_message, the rest
        # in _fail_pending_requests.
        self._async_request_hops: dict[str, tuple[HopToken, str]] = {}
        # Open sync caller hops by wire id. The reader only peeks; entries
        # are removed by the sync future's done-callback.
        self._sync_request_hops: dict[str, HopToken] = {}
        self.feature_impls: dict[str, collections.abc.Callable] = {}
        self._peer_cancel_hooks: dict[
            str, collections.abc.Callable[[asyncio.Task[typing.Any]], None]
        ] = {}
        # Method-specific ``error.data`` for a request cancelled before its
        # handler started (see feature).
        self._not_started_cancel_data: dict[str, dict[str, object]] = {}
        # Request methods cancelled once when the channel fails (see feature).
        self._cancel_on_connection_loss: set[str] = set()
        # Incoming requests being served, by request id; removed when the
        # serving task finishes.
        self._inbound_requests: dict[str | int, _InboundRequest] = {}
        self._sync_feature_impls: dict[
            str, collections.abc.Callable[[typing.Any], None]
        ] = {}
        self._response_completions: dict[str, ResponseCompletion] = {}
        self._pending_drains: set[concurrent.futures.Future[None]] = set()
        self._pending_drains_lock: typing.Final = threading.Lock()

        # NOTE: reader and writer can be accessed only in IO thread
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._async_tasks_in_io_thread: list[asyncio.Task[typing.Any]] = []
        self._tcp_port_future: asyncio.Future[int] | None = None

    def feature(
        self,
        name: str,
        impl: collections.abc.Callable,
        *,
        on_peer_cancel: (
            collections.abc.Callable[[asyncio.Task[typing.Any]], None] | None
        ) = None,
        not_started_cancel_data: collections.abc.Mapping[str, object] | None = None,
        cancel_on_connection_loss: bool = False,
    ) -> None:
        """Register *impl* for incoming ``name`` requests or notifications.

        *on_peer_cancel* opts a request method into acting on the peer's
        ``$/cancelRequest``: the hook is called synchronously with the task
        serving the request, then the task is cancelled and the request is
        answered once with the cancelled error code. Without it, a peer cancel
        is ignored and the request keeps being served.

        With *not_started_cancel_data*, a request cancelled before its handler
        started is answered cancelled carrying that mapping as ``error.data``.

        With *cancel_on_connection_loss*, the request is cancelled once when
        the channel fails (inbound queue closed or server stopped).
        """
        # NOTE: registering a feature here is necessary but NOT sufficient. Incoming
        # requests and notifications are also gated and deserialized against the
        # client's `message_types` table: `_handle_incoming_*` drops any method missing
        # from `message_types` with only a warning (see the notification path below),
        # so the impl never fires. Every `feature(method, ...)` must have a matching
        # entry in the `message_types` map passed to this client.
        if name in self._sync_feature_impls:
            raise ValueError(
                f"Method {name!r} already has a synchronous feature registration"
            )
        self.feature_impls[name] = impl
        if on_peer_cancel is not None:
            self._peer_cancel_hooks[name] = on_peer_cancel
        if not_started_cancel_data is not None:
            self._not_started_cancel_data[name] = dict(not_started_cancel_data)
        if cancel_on_connection_loss:
            self._cancel_on_connection_loss.add(name)

    def feature_sync(self, name: str, impl: collections.abc.Callable[_P, None]) -> None:
        """Register an opt-in synchronous notification handler.

        Runs inline on the dispatching loop without suspension, task
        scheduling, or user callbacks off-loop. Rejects coroutine and
        async-generator functions (including callable objects and partials
        wrapping them) and overlapping ordinary registrations.
        """
        if name not in self.message_types:
            raise ValueError(f"Type for method {name} not found")
        if name in self.feature_impls:
            raise ValueError(
                f"Method {name!r} already has an ordinary feature registration"
            )
        if name in self._sync_feature_impls:
            raise ValueError(
                f"Method {name!r} already has a synchronous feature registration"
            )
        target: typing.Any = impl
        if isinstance(impl, functools.partial):
            target = impl.func
        if (
            callable(target)
            and not isinstance(target, type)
            and not inspect.isfunction(target)
            and not inspect.ismethod(target)
            and not isinstance(impl, functools.partial)
        ):
            try:
                candidate = type(target).__call__
            except AttributeError:
                candidate = None
            if inspect.iscoroutinefunction(candidate) or inspect.isasyncgenfunction(
                candidate
            ):
                raise TypeError(f"Synchronous feature {name!r} must not be async")
        if inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target):
            raise TypeError(f"Synchronous feature {name!r} must not be async")
        if inspect.iscoroutinefunction(impl) or inspect.isasyncgenfunction(impl):
            raise TypeError(f"Synchronous feature {name!r} must not be async")
        self._sync_feature_impls[name] = impl  # type: ignore[assignment]

    def _run_response_capture(self, completion: ResponseCompletion) -> None:
        """Run a response capture without suspending and record its fault."""
        try:
            result = completion.capture()
        except Exception as error:
            completion.error = error
            return
        if inspect.isawaitable(result):
            with contextlib.suppress(Exception):
                if inspect.iscoroutine(result):
                    result.close()
                elif inspect.isasyncgen(result):
                    result.aclose()
                else:
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
            completion.error = TypeError(
                "Response capture must not return an awaitable"
            )

    def _fail_drain(
        self, future: concurrent.futures.Future[None], error: Exception
    ) -> None:
        with self._pending_drains_lock:
            registered = future in self._pending_drains
            if registered:
                self._pending_drains.discard(future)
        if registered and not future.done():
            with contextlib.suppress(Exception):
                future.set_exception(error)

    def _fail_all_drains(self, error: Exception) -> None:
        with self._pending_drains_lock:
            pending = list(self._pending_drains)
            self._pending_drains.clear()
        for future in pending:
            if not future.done():
                with contextlib.suppress(Exception):
                    future.set_exception(error)

    def _resolve_drain(self, marker: OutputDrain) -> None:
        with self._pending_drains_lock:
            registered = marker.completion in self._pending_drains
            if registered:
                self._pending_drains.discard(marker.completion)
        if registered and not marker.completion.done():
            with contextlib.suppress(Exception):
                marker.completion.set_result(None)

    def _hop_stage(
        self,
        hop: HopToken | None,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus = "observed",
    ) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(observer, lambda: observer.stage(hop, stage, at, status))

    def _hop_close(self, hop: HopToken | None, reason: HopCloseReason) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(
            observer,
            lambda: observer.close_receiver(hop, reason, capture_stage_time()),
        )

    def _finish_caller(self, hop: HopToken | None, outcome: CallerOutcome) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        now = capture_stage_time()
        call_guarded(observer, lambda: observer.stage(hop, "caller_completed", now))
        call_guarded(observer, lambda: observer.close_caller(hop, outcome, now))

    def _record_handler_outcome(
        self, hop: HopToken | None, outcome: HandlerOutcome
    ) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(observer, lambda: observer.handler_outcome(hop, outcome))

    def _open_caller(self, msg_id: int | str, method: str) -> HopToken | None:
        observer = self._hop_observer
        if observer is None:
            return None
        identity = HopIdentity(
            connection_id=self._connection_id,
            role="caller",
            request_id=msg_id,
            method=method,
        )
        return call_guarded(
            observer,
            lambda: observer.open_caller(
                identity, self.readable_id, capture_stage_time()
            ),
        )

    async def _send_outgoing_loop(self, writer: asyncio.StreamWriter) -> None:
        """IO-thread sender: write bytes and resolve drain markers in order."""
        logger.debug("Start sending messages from queue")
        queue = self.out_message_queue.async_q
        try:
            while True:
                message = await queue.get()
                try:
                    if isinstance(message, OutputDrain):
                        # Preceding bytes were already written and drained, so
                        # the marker resolves now; later items cannot delay it.
                        self._resolve_drain(message)
                        continue
                    if message == QUEUE_END:
                        writer.close()
                        logger.debug("Queue was closed, stop sending")
                        break
                    if isinstance(message, OutgoingFrame):
                        hop: HopToken | None = message.hop
                        data: bytes = message.data
                    else:
                        hop = None
                        data = message
                    if hop is not None:
                        self._hop_stage(hop, "dequeued", capture_stage_time())
                    try:
                        writer.write(data)
                    except Exception:
                        if hop is not None:
                            self._hop_stage(
                                hop, "write_returned", capture_stage_time(), "failed"
                            )
                            self._hop_close(hop, "write_failed")
                        raise
                    if hop is not None:
                        self._hop_stage(hop, "write_returned", capture_stage_time())
                        self._hop_stage(hop, "drain_started", capture_stage_time())
                    try:
                        await writer.drain()
                    except Exception:
                        if hop is not None:
                            self._hop_stage(
                                hop, "drain_returned", capture_stage_time(), "failed"
                            )
                            self._hop_close(hop, "write_failed")
                        raise
                    if hop is not None:
                        self._hop_stage(hop, "drain_returned", capture_stage_time())
                        self._hop_close(hop, "completed")
                finally:
                    with contextlib.suppress(Exception):
                        queue.task_done()
        except asyncio.CancelledError:
            self._fail_all_drains(ServerStoppedError("sender cancelled"))
            raise
        except Exception as error:
            self._fail_all_drains(error)
            raise
        else:
            self._fail_all_drains(ServerStoppedError("sender closed"))
        finally:
            with contextlib.suppress(Exception):
                queue.shutdown()
            logger.debug("End sending messages from queue")

    async def start(
        self,
        server_cmd: _spawn.SpawnCommand,
        working_dir_path: Path,
        io_thread: _io_thread.AsyncIOThread,
        debug_port_future: concurrent.futures.Future[int] | None,
        connect: bool = True,
    ) -> None:
        # The working directory and environment are passed to the subprocess
        # explicitly rather than by mutating this process's cwd/environ around the
        # spawn. Several clients start concurrently and the spawn itself happens on
        # the io thread, so process-global mutation is a race: a server could
        # inherit whichever directory another client left current. Anything the
        # server derives from its cwd (isort's first-party detection, for one)
        # would then vary from run to run.
        # VIRTUAL_ENV is dropped so that the server does not start in the wrong venv
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}

        await self._start_server(
            full_cmd=server_cmd,
            io_thread=io_thread,
            debug_port_future=debug_port_future,
            cwd=working_dir_path,
            env=env,
            stderr_buffer=self._stderr_buffer,
            stdout_buffer=self._stdout_buffer,
        )
        if connect:
            await self.connect_to_server(io_thread=io_thread)

    async def _spawn_and_record(self, **start_server_kwargs):
        """Spawn the server and publish its pid on the IO thread.

        ``pid`` must be set before ``start()`` returns control to the main
        loop, so a cancellation or a force-kill landing in that window still
        has a process to kill. A kill requested before the pid existed is
        applied here, once it does.
        """
        result = await start_server(**start_server_kwargs)
        self.pid = result[3]
        if self._kill_requested:
            self.force_kill()
        return result

    async def _start_server(
        self,
        full_cmd: _spawn.SpawnCommand,
        io_thread: _io_thread.AsyncIOThread,
        debug_port_future: concurrent.futures.Future[int] | None,
        cwd: Path,
        env: dict[str, str],
        stderr_buffer: list[str] | None = None,
        stdout_buffer: list[str] | None = None,
    ) -> None:
        server_future = io_thread.run_coroutine(
            self._spawn_and_record(
                cmd=full_cmd,
                communication_type=self.communication_type,
                out_message_queue=self.out_message_queue,
                stop_event=self._stop_event,
                server_stopped_event=self.server_process_stopped,
                server_id=self.readable_id,
                async_tasks=self._async_tasks_in_io_thread,
                debug_port_future=debug_port_future,
                cwd=cwd,
                env=env,
                stderr_buffer=stderr_buffer,
                stdout_buffer=stdout_buffer,
                timeline=self.startup_timeline,
            )
        )

        # add done callback to catch exceptions if coroutine fails
        server_future.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"server_future|{self.readable_id}"
            )
        )

        await asyncio.wrap_future(server_future)
        server_start_exception = server_future.exception()
        if server_start_exception is not None:
            # there are no active tasks yet, no need to stop, just interrupt starting
            # the server
            raise server_start_exception

        self._reader, self._writer, self._tcp_port_future, self.pid = (
            server_future.result()
        )

        notify_exit = asyncio.create_task(self._server_process_stop_handler())
        notify_exit.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"notify_exit|{self.readable_id}"
            )
        )

        self._async_tasks.extend([notify_exit])
        logger.debug(f"End of start for {full_cmd}")

    async def connect_to_server(
        self, io_thread: _io_thread.AsyncIOThread, timeout: float | None = 30
    ):
        connect_to_server_future = io_thread.run_coroutine(
            self._connect_to_server_io(timeout=timeout)
        )

        # add done callback to catch exceptions if coroutine fails
        connect_to_server_future.add_done_callback(
            functools.partial(
                task_done_log_callback,
                task_id=f"connect_to_server_future|{self.readable_id}",
            )
        )

        await asyncio.wrap_future(connect_to_server_future)
        connect_to_server_future_exception = connect_to_server_future.exception()
        if connect_to_server_future_exception is not None:
            raise connect_to_server_future_exception

        # message processor task ends automatically after getting QUEUE_END message,
        # no need to save it in `_async_tasks` for explicit stop.
        message_processor_task = asyncio.create_task(self.process_incoming_messages())
        message_processor_task.add_done_callback(
            functools.partial(
                task_done_log_callback,
                task_id=f"process_incoming_messages|{self.readable_id}",
            )
        )

    def _fail_pending_requests(self, reason: str) -> None:
        """Fail every in-flight request future because its channel can no longer answer.

        Called when the inbound queue closes or the server process exits. It is
        a request-liveness signal only: ``server_process_stopped`` and
        ``server_exit_callback`` stay owned by the process watcher, because a
        closed socket can precede the OS process exiting.
        """
        self.channel_failed = True
        self._async_request_hops.clear()
        self._sync_request_hops.clear()
        for _request_id, inbound in list(self._inbound_requests.items()):
            if inbound.cancelled_by_connection_loss:
                continue
            if inbound.method not in self._cancel_on_connection_loss:
                continue
            inbound.cancelled_by_connection_loss = True
            hook = self._peer_cancel_hooks.get(inbound.method)
            if hook is not None:
                try:
                    hook(inbound.task)
                except Exception as hook_error:
                    logger.warning(
                        f"on_peer_cancel hook of {inbound.method} failed:"
                        f" {hook_error} | {self.readable_id}"
                    )
            self._cancel_inbound_task(inbound)
        for msg_id, fut in list(self._sync_request_futures.items()) + list(
            self._async_request_futures.items()
        ):
            if not fut.done():
                fut.set_exception(ServerStoppedError(reason))
                self._expected_result_type_by_msg_id.pop(msg_id, None)
                logger.debug(f"Cancelled pending request '{msg_id}': {reason}")
        # Completions are never run for EOF/timeout/cancellation: drop them.
        self._response_completions.clear()
        self._cancelled_request_ids.clear()
        self._fail_all_drains(ServerStoppedError(reason))

    async def _server_process_stop_handler(self):
        """Cleanup handler that runs when the server process managed by the client exits"""
        logger.trace(f"Server process stopped handler {self.readable_id}")
        await self.server_process_stopped.wait_async()

        logger.debug(f"Server process {self.readable_id} stopped")

        self._fail_pending_requests("Server was stopped before getting the response")

        if self.server_exit_callback is not None:
            await self.server_exit_callback()

        logger.debug(f"End of server stopped handler {self.readable_id}")

    def stop(self) -> None:
        self._stop_event.set()

    def force_kill(self) -> None:
        """Forcefully terminate the server process and everything it spawned.

        Use this when there is no live RPC channel to ask the server to stop
        cooperatively — the start attempt timed out before the port/handshake
        completed, or the graceful ``stop()`` path did not result in exit
        within its timeout. A no-op if the process was never spawned, has
        already exited, or its group has no member left that can be signalled.

        The process is started with ``start_new_session=True`` (POSIX) so it is
        the leader of its own process group, and the pid recorded here is the
        server itself rather than a ``/bin/sh`` wrapper, so the group kill below
        and the recorded pid agree.
        """
        self._kill_requested = True
        if self.pid is None:
            return

        force_kill_process_group(
            self.pid, self.readable_id, descendant_groups=self._kill_descendant_groups
        )

    def _send_data(self, data: str, hop: HopToken | None = None):
        header = (
            f"Content-Length: {len(data)}\r\n"
            f"Content-Type: {self.CONTENT_TYPE}; charset={self.CHARSET}\r\n\r\n"
        )
        data = header + data

        frame = OutgoingFrame(data=data.encode(self.CHARSET), hop=hop)
        if hop is not None:
            self._hop_stage(hop, "submitted", capture_stage_time())
        try:
            self.writer.write(frame)
        except culsans.QueueShutDown:
            logger.debug(
                f"Cannot send data to {self.readable_id}: client already disconnected"
            )
        except Exception as error:  # noqa: BLE001
            # the writer puts a message in the queue without size, so no exception
            # are expected. If one internal come such as shutdown exception because of
            # mistake in implementation, log it
            logger.error(f"Error sending data to {self.readable_id}:")
            logger.exception(error)
        else:
            if hop is not None:
                self._hop_stage(hop, "enqueued", capture_stage_time())

    def _send_frame(self, data: str, hop: HopToken | None) -> None:
        """Send *data*, attaching *hop* only when observed.

        With no hop the call shape is exactly today's single-argument one.
        """
        if hop is not None:
            self._send_data(data, hop)
        else:
            self._send_data(data)

    def _send_error_response(
        self,
        request_id: str | int | None,
        code: int,
        message: str,
        data: typing.Any = None,
        hop: HopToken | None = None,
    ) -> None:
        """Send a JSON-RPC error response.

        Args:
            request_id: The ID of the request that caused the error. None for notifications.
            code: JSON-RPC error code (see JsonRpcErrorCode)
            message: Short description of the error
            data: Optional additional error information
        """
        error_object = {"code": code, "message": message}
        if data is not None:
            error_object["data"] = data

        response_dict = {
            "jsonrpc": self.VERSION,
            "id": request_id,
            "error": error_object,
        }

        response_str = json.dumps(response_dict)
        logger.debug(f"Sending error response: {code} - {message}")
        self._send_frame(response_str, hop)

    def notify(self, method: str, params: typing.Any | None = None) -> None:
        logger.debug(f"Sending notification: '{method}' {params}")

        try:
            notification_params_type = self.message_types[method][1]
        except KeyError as error:
            raise ValueError(
                f"Type of notification params for {method} not found"
            ) from error

        if notification_params_type is not None:
            notification_params_dict = _converter.unstructure(params)
        else:
            notification_params_dict = None

        notification_dict: dict[str, typing.Any] = {
            "method": method,
            "params": notification_params_dict,
            "jsonrpc": self.VERSION,
        }
        if self._tracing is not None:
            traceparent = self._tracing.get_traceparent()
            if traceparent is not None:
                notification_dict["_meta"] = {"traceparent": traceparent}

        try:
            notification_str = json.dumps(notification_dict)
        except (TypeError, ValueError) as error:
            raise InvalidResponse(
                f"Failed to serialize notification: {error}"
            ) from error
        logger.trace(notification_str)
        self._send_data(notification_str)
        if self._tracing is not None:
            self._tracing.notification_sent(method)

    async def notify_and_drain(
        self, method: str, params: typing.Any | None = None
    ) -> None:
        """Send a notification and wait until its bytes are written and drained.

        Reuses the ``notify`` serialization/tracing/framing behavior but
        enqueues strictly (queue closure is raised, not swallowed) and
        resolves only after the IO-loop sender drains all preceding bytes.
        """
        logger.debug(f"Sending notification with drain: '{method}' {params}")
        try:
            notification_params_type = self.message_types[method][1]
        except KeyError as error:
            raise ValueError(
                f"Type of notification params for {method} not found"
            ) from error
        if notification_params_type is not None:
            notification_params_dict = _converter.unstructure(params)
        else:
            notification_params_dict = None
        notification_dict: dict[str, typing.Any] = {
            "method": method,
            "params": notification_params_dict,
            "jsonrpc": self.VERSION,
        }
        if self._tracing is not None:
            traceparent = self._tracing.get_traceparent()
            if traceparent is not None:
                notification_dict["_meta"] = {"traceparent": traceparent}
        try:
            notification_str = json.dumps(notification_dict)
        except (TypeError, ValueError) as error:
            raise InvalidResponse(
                f"Failed to serialize notification: {error}"
            ) from error
        logger.trace(notification_str)
        header = (
            f"Content-Length: {len(notification_str)}\r\n"
            f"Content-Type: {self.CONTENT_TYPE}; charset={self.CHARSET}\r\n\r\n"
        )
        payload = (header + notification_str).encode(self.CHARSET)
        drain: concurrent.futures.Future[None] = concurrent.futures.Future()
        marker = OutputDrain(completion=drain)
        with self._pending_drains_lock:
            self._pending_drains.add(drain)
        try:
            # No suspension between the two enqueues: the marker always
            # directly follows its bytes so later items cannot extend the fence.
            self.out_message_queue.sync_q.put(payload)
            self.out_message_queue.sync_q.put(marker)
        except Exception as error:
            self._fail_drain(
                drain,
                error
                if isinstance(error, Exception)
                else ServerStoppedError(str(error)),
            )
            raise
        if self._tracing is not None:
            self._tracing.notification_sent(method)
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wrap_future(drain, loop=loop)
        except asyncio.CancelledError:
            with self._pending_drains_lock:
                self._pending_drains.discard(drain)
            if not drain.done():
                _ = drain.cancel()
            raise
        except Exception:
            with self._pending_drains_lock:
                self._pending_drains.discard(drain)
            raise

    def send_request_sync(
        self,
        method: str,
        params: typing.Any | None = None,
        # timeout: float | None = None
    ) -> concurrent.futures.Future[typing.Any]:
        if self.channel_failed:
            raise ServerStoppedError(
                f"Channel to {self.readable_id} already failed; refusing to send"
            )
        try:
            request_params_type = self.message_types[method][1]
        except KeyError as error:
            raise ValueError(f"Type for method {method} not found") from error

        msg_id = str(uuid.uuid4())
        logger.debug(
            f'Sending request with id "{msg_id}": {method} to {self.readable_id}'
        )

        if request_params_type is not None:
            request_params_dict = _converter.unstructure(params)
        else:
            request_params_dict = None

        hop = self._open_caller(msg_id, method)

        future = concurrent.futures.Future()
        try:
            self._expected_result_type_by_msg_id[msg_id] = self.message_types[method][2]
        except KeyError as error:
            self._finish_caller(hop, "send_failed")
            raise ValueError(f"Message type not found for {method}") from error

        self._sync_request_futures[msg_id] = future
        if hop is not None:
            self._sync_request_hops[msg_id] = hop

        request_dict: dict[str, typing.Any] = {
            "id": msg_id,
            "method": method,
            "params": request_params_dict,
            "jsonrpc": self.VERSION,
        }
        observer = self._hop_observer
        if hop is not None and observer is not None:
            traceparent = call_guarded(
                observer, lambda: observer.caller_traceparent(hop)
            )
            if traceparent is not None:
                request_dict["_meta"] = {"traceparent": traceparent}
        elif self._tracing is not None:
            traceparent = self._tracing.get_traceparent()
            if traceparent is not None:
                request_dict["_meta"] = {"traceparent": traceparent}

        try:
            request_str = json.dumps(request_dict)
        except (TypeError, ValueError) as error:
            # Clean up the future if serialization fails
            self._sync_request_futures.pop(msg_id, None)
            _ = self._sync_request_hops.pop(msg_id, None)
            self._expected_result_type_by_msg_id.pop(msg_id, None)
            self._finish_caller(hop, "send_failed")
            raise InvalidResponse(f"Failed to serialize request: {error}") from error

        if hop is not None:
            future.add_done_callback(
                functools.partial(self._close_sync_caller, msg_id, hop)
            )
        self._send_frame(request_str, hop)

        return future

    def _close_sync_caller(
        self,
        msg_id: str,
        hop: HopToken,
        future: concurrent.futures.Future[typing.Any],
    ) -> None:
        """Close a sync caller hop when its future completes.

        Runs on whichever thread completes the future (normally the IO
        thread); never at ``send_request_sync`` return.
        """
        _ = self._sync_request_hops.pop(msg_id, None)
        observer = self._hop_observer
        if observer is None:
            return
        try:
            error = future.exception()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            outcome: CallerOutcome = "cancelled"
        else:
            if error is None:
                outcome = "result"
            elif isinstance(error, ErrorOnRequest):
                outcome = "remote_error"
            elif isinstance(error, InvalidResponse):
                outcome = "invalid_response"
            elif isinstance(error, ServerStoppedError):
                outcome = "disconnected"
            else:
                outcome = "invalid_response"
        now = capture_stage_time()
        call_guarded(observer, lambda: observer.stage(hop, "caller_completed", now))
        call_guarded(observer, lambda: observer.close_caller(hop, outcome, now))
        # try:
        #     response = future.result(
        #         timeout=timeout,
        #     )
        #     logger.debug(f"Got response on {method} from {self.readable_id}")
        #     return response
        # except TimeoutError:
        #     raise ResponseTimeout(
        #         f"Timeout {timeout}s for response on {method} to"
        #         f" {self.readable_id}"
        #     )

    async def send_request(
        self,
        method: str,
        params: typing.Any | None = None,
        timeout: float | None = None,
        *,
        on_response: ResponseCompletion | None = None,
        await_reply_after_cancel: AwaitReplyAfterCancel | None = None,
    ) -> typing.Any:
        if self.channel_failed:
            raise ServerStoppedError(
                f"Channel to {self.readable_id} already failed; refusing to wait"
            )
        try:
            request_params_type = self.message_types[method][1]
        except KeyError as error:
            raise ValueError(f"Type for method {method} not found") from error

        msg_id = str(uuid.uuid4())
        logger.debug(
            f'Sending request with id "{msg_id}": {method} to {self.readable_id}'
        )

        if request_params_type is not None:
            request_params_dict = _converter.unstructure(params)
        else:
            request_params_dict = None

        message_dict: dict[str, typing.Any] = {
            "id": msg_id,
            "method": method,
            "params": request_params_dict,
            "jsonrpc": self.VERSION,
        }

        hop = self._open_caller(msg_id, method)
        if hop is not None:
            self._async_request_hops[msg_id] = (hop, method)

        # When a hop observer is set, request hops are timed only by it.
        span_ctx = (
            self._tracing.client_span(method, self.readable_id)
            if self._tracing is not None and hop is None
            else contextlib.nullcontext()
        )
        with span_ctx:
            if hop is not None:
                observer = self._hop_observer
                assert observer is not None
                traceparent = call_guarded(
                    observer, lambda: observer.caller_traceparent(hop)
                )
                if traceparent is not None:
                    message_dict["_meta"] = {"traceparent": traceparent}
            elif self._tracing is not None:
                traceparent = self._tracing.get_traceparent()
                if traceparent is not None:
                    message_dict["_meta"] = {"traceparent": traceparent}

            future: asyncio.Future[typing.Any] = asyncio.Future()
            self._async_request_futures[msg_id] = future
            if on_response is not None:
                self._response_completions[msg_id] = on_response

            try:
                try:
                    self._expected_result_type_by_msg_id[msg_id] = self.message_types[
                        method
                    ][2]
                except KeyError as error:
                    raise ValueError(f"Message type not found for {method}") from error
                try:
                    request_str = json.dumps(message_dict)
                except (TypeError, ValueError) as error:
                    raise InvalidResponse(
                        f"Failed to serialize request: {error}"
                    ) from error
                self._send_frame(request_str, hop)
            except Exception:
                _ = self._async_request_futures.pop(msg_id, None)
                _ = self._expected_result_type_by_msg_id.pop(msg_id, None)
                _ = self._response_completions.pop(msg_id, None)
                _ = self._async_request_hops.pop(msg_id, None)
                self._finish_caller(hop, "send_failed")
                raise

            try:
                response = await asyncio.wait_for(
                    future,
                    timeout,
                )
                logger.debug(f"Got response on {method} from {self.readable_id}")
            except TimeoutError as error:
                self.channel_failed = True
                self._finish_caller(hop, "timeout")
                raise ResponseTimeout(
                    f"Timeout {timeout}s for response on {method} to"
                    f" runner {self.readable_id}"
                ) from error
            except asyncio.CancelledError as error:
                if await_reply_after_cancel is not None:
                    raise await self._await_reply_after_cancel(
                        msg_id, future, hop, await_reply_after_cancel
                    ) from error
                self._cancelled_request_ids.add(msg_id)
                self._finish_caller(hop, "cancelled")
                raise RequestCancelledError(request_id=msg_id) from error
            except ErrorOnRequest:
                self._finish_caller(hop, "remote_error")
                raise
            except InvalidResponse:
                self._finish_caller(hop, "invalid_response")
                raise
            except ServerStoppedError:
                self._finish_caller(hop, "disconnected")
                raise
            else:
                self._finish_caller(hop, "result")
                return response
            finally:
                _ = self._async_request_futures.pop(msg_id, None)
                _ = self._expected_result_type_by_msg_id.pop(msg_id, None)
                _ = self._response_completions.pop(msg_id, None)

    def _send_cancel_request(self, msg_id: str) -> None:
        """Write a ``$/cancelRequest`` frame for *msg_id* without suspending."""
        cancel_dict: dict[str, typing.Any] = {
            "method": "$/cancelRequest",
            "params": {"id": msg_id},
            "jsonrpc": self.VERSION,
        }
        if self._tracing is not None:
            traceparent = self._tracing.get_traceparent()
            if traceparent is not None:
                cancel_dict["_meta"] = {"traceparent": traceparent}
        try:
            cancel_str = json.dumps(cancel_dict)
        except (TypeError, ValueError):
            return
        self._send_data(cancel_str)

    def _reply_from_settled_client(
        self, future: asyncio.Future[typing.Any]
    ) -> ReplyAfterCancel:
        if future.cancelled():
            return ReplyAfterCancel(kind=ReplyAfterCancelKind.DISCONNECTED)
        exc = future.exception()
        if exc is None:
            return ReplyAfterCancel(
                kind=ReplyAfterCancelKind.RESULT, result=future.result()
            )
        if isinstance(exc, ErrorOnRequest):
            return ReplyAfterCancel(
                kind=ReplyAfterCancelKind.ERROR,
                error_code=exc.error.code,
                error_data=exc.error.data,
            )
        if isinstance(exc, ServerStoppedError):
            return ReplyAfterCancel(kind=ReplyAfterCancelKind.DISCONNECTED)
        if isinstance(exc, InvalidResponse):
            return ReplyAfterCancel(kind=ReplyAfterCancelKind.ERROR)
        return ReplyAfterCancel(kind=ReplyAfterCancelKind.DISCONNECTED)

    async def _await_reply_after_cancel(
        self,
        msg_id: str,
        future: asyncio.Future[typing.Any],
        hop: HopToken | None,
        option: AwaitReplyAfterCancel,
    ) -> RequestCancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            current.uncancel()
        if future.cancelled():
            # The initial wait cancelled the future itself (asyncio.wait_for
            # cancels what it waits on), so a later reply would be dropped.
            # Swap in a fresh future under the same id so the post-cancel
            # wait can still observe the reply.
            future = asyncio.get_running_loop().create_future()
            self._async_request_futures[msg_id] = future
        if future.done() and not future.cancelled():
            reply = self._reply_from_settled_client(future)
            self._finish_caller(hop, "cancelled")
            return RequestCancelledError(msg_id, reply=reply)
        self._send_cancel_request(msg_id)
        bound = option.timeout
        deadline = None if bound is None else time.monotonic() + bound
        while True:
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._cancelled_request_ids.add(msg_id)
                    self._finish_caller(hop, "cancelled")
                    return RequestCancelledError(
                        msg_id,
                        reply=ReplyAfterCancel(kind=ReplyAfterCancelKind.TIMEOUT),
                    )
            try:
                if remaining is None:
                    settled = await asyncio.shield(future)
                else:
                    settled = await asyncio.wait_for(asyncio.shield(future), remaining)
            except TimeoutError:
                self._cancelled_request_ids.add(msg_id)
                self._finish_caller(hop, "cancelled")
                return RequestCancelledError(
                    msg_id,
                    reply=ReplyAfterCancel(kind=ReplyAfterCancelKind.TIMEOUT),
                )
            except asyncio.CancelledError:
                if current is not None and current.cancelling() > 0:
                    current.uncancel()
                if future.done() and not future.cancelled():
                    reply = self._reply_from_settled_client(future)
                    self._finish_caller(hop, "cancelled")
                    return RequestCancelledError(msg_id, reply=reply)
                continue
            except ErrorOnRequest as exc:
                self._finish_caller(hop, "cancelled")
                return RequestCancelledError(
                    msg_id,
                    reply=ReplyAfterCancel(
                        kind=ReplyAfterCancelKind.ERROR,
                        error_code=exc.error.code,
                        error_data=exc.error.data,
                    ),
                )
            except (ServerStoppedError, InvalidResponse):
                self._finish_caller(hop, "cancelled")
                kind = ReplyAfterCancelKind.DISCONNECTED
                return RequestCancelledError(msg_id, reply=ReplyAfterCancel(kind=kind))
            else:
                self._finish_caller(hop, "cancelled")
                return RequestCancelledError(
                    msg_id,
                    reply=ReplyAfterCancel(
                        kind=ReplyAfterCancelKind.RESULT, result=settled
                    ),
                )

    async def process_incoming_messages(self) -> None:
        logger.debug(f"Start processing messages from server {self.readable_id}")
        try:
            while True:
                raw_message = await self.in_message_queue.async_q.get()
                if isinstance(raw_message, ReceivedFrame):
                    message = raw_message.message
                    frame_read: StageTime | None = raw_message.received
                elif raw_message == QUEUE_END:
                    message = None
                    frame_read = None
                else:
                    message = raw_message
                    frame_read = None
                if message is None:
                    # TODO: this message doesn't come, task is always cancelled
                    logger.info("Queue with messages from server was closed")
                    self._fail_pending_requests("inbound queue closed")
                    self.in_message_queue.async_q.task_done()
                    self.in_message_queue.async_q.shutdown()
                    break

                try:
                    await self.handle_message(message, frame_read)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(exc)
                finally:
                    self.in_message_queue.async_q.task_done()
        except asyncio.CancelledError:
            # logger.warning("process_incoming_messages was cancelled")
            ...

        # self.in_message_queue.async_q.shutdown()
        logger.debug(f"End processing messages from server {self.readable_id}")

    def _open_receiver(
        self, message: dict[str, typing.Any], frame_read: StageTime | None
    ) -> HopToken | None:
        observer = self._hop_observer
        if observer is None:
            return None
        at = frame_read if frame_read is not None else capture_stage_time()
        traceparent = (message.get("_meta") or {}).get("traceparent")
        identity = HopIdentity(
            connection_id=self._connection_id,
            role="receiver",
            request_id=message["id"],
            method=message["method"],
        )
        return call_guarded(
            observer, lambda: observer.open_receiver(identity, traceparent, at)
        )

    def _report_unmatched_response(
        self, message_id: str | int, frame_read: StageTime | None
    ) -> None:
        observer = self._hop_observer
        if observer is None or not isinstance(message_id, str):
            return
        hop_info = self._async_request_hops.pop(message_id, None)
        method = hop_info[1] if hop_info is not None else ""
        identity = HopIdentity(
            connection_id=self._connection_id,
            role="caller",
            request_id=message_id,
            method=method,
        )
        at = frame_read if frame_read is not None else capture_stage_time()
        call_guarded(observer, lambda: observer.response_without_waiter(identity, at))

    def _stage_response_resolved(
        self, message_id: str | int, frame_read: StageTime | None
    ) -> None:
        if not isinstance(message_id, str):
            return
        hop_info = self._async_request_hops.pop(message_id, None)
        if hop_info is None:
            return
        hop, _method = hop_info
        at = frame_read if frame_read is not None else capture_stage_time()
        self._hop_stage(hop, "frame_read", at)
        self._hop_stage(hop, "response_resolved", capture_stage_time())

    async def handle_message(
        self,
        message: dict[str, typing.Any],
        frame_read: StageTime | None = None,
    ) -> None:
        if "id" in message:
            message_id = message["id"]

            if not isinstance(message_id, str) and not isinstance(message_id, int):
                logger.warning(
                    f"Got message with unsupported id: {type(message_id)}, but string or int expected"
                )
                return

            if "error" in message:
                # error as response
                logger.trace(f"Processing message with error on request {message_id}")
                if message_id in self._cancelled_request_ids:
                    self._cancelled_request_ids.discard(message_id)
                    _ = self._async_request_hops.pop(message_id, None)
                    logger.debug(
                        f"Dropping error response for cancelled request {message_id}"
                    )
                    return
                # sync request futures are handled in another thread, handle only async
                # here
                if message_id not in self._async_request_futures:
                    logger.error(
                        f"Got error as response for {message_id}, but no response was expected"
                    )
                    self._report_unmatched_response(message_id, frame_read)
                    return

                future = self._async_request_futures.pop(message_id, None)
                if future is None:
                    logger.error(
                        f"Got error on request {message_id}, but no response was expected"
                    )
                    self._report_unmatched_response(message_id, frame_read)
                    return
                if not future.cancelled():
                    self._stage_response_resolved(message_id, frame_read)
                else:
                    _ = self._async_request_hops.pop(message_id, None)
                completion = self._response_completions.pop(message_id, None)
                if completion is not None:
                    self._run_response_capture(completion)

                try:
                    response_error = _converter.structure(
                        message["error"], ResponseError
                    )
                except cattrs.ClassValidationError as error:
                    exception = InvalidResponse(str(error))

                    # avoid race condition: request is sent, then cancelled and the server
                    # sends the response before processing the cancel notification
                    if not future.cancelled():
                        future.set_exception(exception)
                    return

                exception = ErrorOnRequest(error=response_error)
                # avoid race condition: request is sent, then cancelled and the server
                # sends the response before processing the cancel notification
                if not future.cancelled():
                    future.set_exception(exception)
                return
            elif "method" in message:
                # incoming request
                logger.trace(
                    f"Processing message with incoming request {message['method']} | {self.readable_id}"
                )
                hop = self._open_receiver(message, frame_read)
                try:
                    request_type = self.message_types[message["method"]][0]
                    result_type = self.message_types[message["method"]][3]
                except KeyError:
                    # Method type not registered - send 'Method not found' error
                    logger.warning(
                        f"Received request for unregistered method: {message.get('method')} | {self.readable_id}"
                    )
                    self._record_handler_outcome(hop, "not_executed")
                    self._send_error_response(
                        request_id=message_id,
                        code=JsonRpcErrorCode.METHOD_NOT_FOUND,
                        message="Method not found",
                        data=f"Method '{message.get('method')}' is not registered",
                        hop=hop,
                    )
                    return

                try:
                    request = _converter.structure(message, request_type)
                except cattrs.ClassValidationError as error:
                    # Invalid request parameters - send 'Invalid params' error
                    detail = "; ".join(str(sub) for sub in error.exceptions) or str(
                        error
                    )
                    logger.warning(
                        f"Invalid params for method {message.get('method')}: {detail} | {self.readable_id}"
                    )
                    self._record_handler_outcome(hop, "not_executed")
                    self._send_error_response(
                        request_id=message_id,
                        code=JsonRpcErrorCode.INVALID_PARAMS,
                        message="Invalid params",
                        data=detail,
                        hop=hop,
                    )
                    return

                method = message["method"]
                if method not in self.feature_impls:
                    # Method implementation not found - send 'Method not found' error
                    logger.warning(
                        f"Received request for unsupported method: {method} | {self.readable_id}"
                    )
                    self._record_handler_outcome(hop, "not_executed")
                    self._send_error_response(
                        request_id=message_id,
                        code=JsonRpcErrorCode.METHOD_NOT_FOUND,
                        message="Method not found",
                        data=f"Method '{method}' is not supported",
                        hop=hop,
                    )
                    return

                impl = self.feature_impls[method]
                new_task = asyncio.create_task(
                    self.run_feature_impl(
                        message_id, impl(request.params), result_type, hop
                    )
                )
                self._inbound_requests[message_id] = _InboundRequest(
                    method=method, task=new_task
                )
                new_task.add_done_callback(
                    functools.partial(
                        task_done_log_callback,
                        task_id=f"run_feature_impl|{method}|{message_id}|{self.readable_id}",
                    )
                )
                self._async_tasks.append(new_task)
            else:
                # response on our request
                logger.trace(
                    f"Processing message with response to request {message_id}"
                )
                if message_id in self._cancelled_request_ids:
                    self._cancelled_request_ids.discard(message_id)
                    _ = self._async_request_hops.pop(message_id, None)
                    logger.debug(
                        f"Dropping response for cancelled request {message_id}"
                    )
                    return
                if message_id not in self._async_request_futures:
                    logger.error(
                        f"Got response to {message_id}, but no response was expected"
                    )
                    self._report_unmatched_response(message_id, frame_read)
                    return

                # sync request futures are handled in another thread, handle only async
                # here
                future = self._async_request_futures.pop(message_id, None)
                if future is None:
                    logger.error(
                        f"Got response to {message_id}, but no response was expected"
                    )
                    self._report_unmatched_response(message_id, frame_read)
                    return
                if not future.cancelled():
                    self._stage_response_resolved(message_id, frame_read)
                else:
                    _ = self._async_request_hops.pop(message_id, None)
                completion = self._response_completions.pop(message_id, None)
                if completion is not None:
                    self._run_response_capture(completion)

                result_type = self._expected_result_type_by_msg_id[message_id]
                self._expected_result_type_by_msg_id.pop(message_id, None)
                try:
                    response = _converter.structure(message, result_type)
                except cattrs.ClassValidationError as error:
                    logger.error("errro")
                    logger.exception(error)
                    detail = "; ".join(str(sub) for sub in error.exceptions) or str(
                        error
                    )
                    exception = InvalidResponse(detail)

                    # avoid race condition: request is sent, then cancelled and the server
                    # sends the response before processing the cancel notification
                    if not future.cancelled():
                        future.set_exception(exception)
                    return

                # avoid race condition: request is sent, then cancelled and the server
                # sends the response before processing the cancel notification
                if not future.cancelled():
                    future.set_result(response)
                    logger.trace(f"Successfully processed response to {message_id}")
                else:
                    logger.trace(
                        f"Request {message_id} was cancelled, ignore the response"
                    )
                return
        else:
            # incoming notification
            logger.trace(
                f"Processing message with incoming notification | {self.readable_id}"
            )
            if "method" not in message:
                logger.error(
                    f"Notification expected to have a 'method' field: {message} | {self.readable_id}"
                )
                return

            method = message["method"]
            if (
                method == "$/cancelRequest"
                and method not in self.feature_impls
                and method not in self._sync_feature_impls
            ):
                self._handle_peer_cancel(message.get("params"))
                return

            if (
                method not in self.feature_impls
                and method not in self._sync_feature_impls
            ):
                logger.warning(
                    f"Got notification {method}, but it is not supported | {self.readable_id}"
                )
                return

            if method not in self.message_types:
                logger.warning(f"Got unsupported notification: {method}")
                return

            try:
                notification_type = self.message_types[method][0]
                notification = _converter.structure(message, notification_type)
            except (KeyError, cattrs.ClassValidationError) as error:
                logger.warning(
                    f"Failed to deserialize notification {method}: {error} | {self.readable_id}"
                )
                # For notifications, we don't send error responses
                return

            if self._tracing is not None:
                traceparent = (message.get("_meta") or {}).get("traceparent")
                self._tracing.notification_received(method, traceparent)

            sync_impl = self._sync_feature_impls.get(method)
            if sync_impl is not None:
                try:
                    sync_result = sync_impl(notification.params)
                except Exception as sync_error:
                    logger.warning(
                        f"Error in synchronous notification {method}: {sync_error} | {self.readable_id}"
                    )
                    return
                if inspect.isawaitable(sync_result):
                    with contextlib.suppress(Exception):
                        if inspect.iscoroutine(sync_result):
                            sync_result.close()
                        elif inspect.isasyncgen(sync_result):
                            sync_result.aclose()
                        else:
                            close = getattr(sync_result, "close", None)
                            if callable(close):
                                close()
                    logger.warning(
                        f"Synchronous notification {method} returned an awaitable; disposed | {self.readable_id}"
                    )
                return

            impl = self.feature_impls[method]
            new_task = asyncio.create_task(
                self.run_notification_impl(impl(notification.params))
            )
            new_task.add_done_callback(
                functools.partial(
                    task_done_log_callback,
                    task_id=f"run_notification_impl|{method}|{self.readable_id}",
                )
            )
            self._async_tasks.append(new_task)

    def _handle_peer_cancel(self, params: dict[str, typing.Any] | None) -> None:
        """Act on an inbound ``$/cancelRequest`` without suspending.

        Only requests whose method registered ``on_peer_cancel`` are cancelled;
        any other id is ignored and its request keeps being served.
        """
        request_id = params.get("id") if isinstance(params, dict) else None
        inbound = (
            self._inbound_requests.get(request_id) if request_id is not None else None
        )
        hook = self._peer_cancel_hooks.get(inbound.method) if inbound else None
        if inbound is None or hook is None:
            logger.debug(
                f"Ignoring $/cancelRequest for id={request_id}: no cancellable request | {self.readable_id}"
            )
            return

        try:
            hook(inbound.task)
        except Exception as hook_error:
            logger.warning(
                f"on_peer_cancel hook of {inbound.method} failed: {hook_error} | {self.readable_id}"
            )
        self._cancel_inbound_task(inbound)

    def _cancel_inbound_task(self, inbound: _InboundRequest) -> None:
        """Cancel a started inbound task or flag a not-started one."""
        if inbound.started:
            inbound.task.cancel()
        else:
            # A task cancelled before its first step never enters its handler
            # `try`, so it would send no response. run_feature_impl answers it
            # when it starts instead.
            inbound.cancelled_before_start = True

    async def run_feature_impl(
        self,
        message_id: str | int,
        impl_coro,
        result_type,
        hop: HopToken | None = None,
    ) -> None:
        observer = self._hop_observer
        if hop is not None and observer is not None:
            self._hop_stage(hop, "handler_started", capture_stage_time())
        inbound = self._inbound_requests.get(message_id)
        try:
            if inbound is not None:
                if inbound.cancelled_before_start:
                    if inspect.iscoroutine(impl_coro):
                        impl_coro.close()
                    self._hop_stage(hop, "handler_ended", capture_stage_time())
                    self._record_handler_outcome(hop, "cancelled")
                    self._send_error_response(
                        request_id=message_id,
                        code=self._request_cancelled_code,
                        message="Request cancelled",
                        data=self._not_started_cancel_data.get(inbound.method),
                        hop=hop,
                    )
                    return
                inbound.started = True
            if hop is not None and observer is not None:
                with guarded_handler_scope(observer, hop):
                    result = await impl_coro
            else:
                result = await impl_coro

            logger.trace(f"{result_type} {result}")
            # Send successful response back to the server
            response_dict = {
                "jsonrpc": self.VERSION,
                "id": message_id,
                "result": _converter.unstructure(result),
            }

            response_str = json.dumps(response_dict)
            logger.debug(f"Sending response for request {message_id}")
            self._hop_stage(hop, "handler_ended", capture_stage_time())
            self._record_handler_outcome(hop, "returned")
            self._send_frame(response_str, hop)
        except asyncio.CancelledError:
            # JSON-RPC 2.0 requires exactly one response per request, and
            # cancellation is not an `Exception`, so without this branch the
            # request is answered by nothing at all and the peer waits for a
            # response that will never come — indefinitely, if it sent the
            # request without a timeout.
            logger.debug(f"Handler of message {message_id} was cancelled")
            self._hop_stage(hop, "handler_ended", capture_stage_time())
            self._record_handler_outcome(hop, "cancelled")
            self._send_error_response(
                request_id=message_id,
                code=self._request_cancelled_code,
                message="Request cancelled",
                hop=hop,
            )
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                # This task really was cancelled (loop teardown, an explicit
                # .cancel()). The response is out; now honour the cancellation
                # instead of reporting the task as completed normally.
                raise
            # Otherwise nobody cancelled this task: a nested *outbound* request
            # made by the handler was cancelled and surfaced as
            # RequestCancelledError, which subclasses CancelledError by design.
            # This task is healthy, so leave it in a completed state — marking
            # it cancelled would misreport it to done-callbacks and awaiters.
        except Exception as exception:  # noqa: BLE001
            code_attr = getattr(exception, "code", None)
            if isinstance(code_attr, int) and code_attr == self._request_cancelled_code:
                message = getattr(exception, "message", None) or str(exception)
                if not message:
                    message = type(exception).__name__
                logger.debug(
                    f"Handler of message {message_id} ended cancelled"
                    f" | {self.readable_id}: {message}"
                )
                self._hop_stage(hop, "handler_ended", capture_stage_time())
                self._record_handler_outcome(hop, "cancelled")
                self._send_error_response(
                    request_id=message_id,
                    code=code_attr,
                    message=message,
                    data=getattr(exception, "data", None),
                    hop=hop,
                )
                return
            message = getattr(exception, "message", None) or str(exception)
            if not message:
                message = type(exception).__name__
            logger.warning(
                f"Error occured on running handler of message {message_id} | {self.readable_id}: {message}"
            )
            if not hasattr(exception, "message"):
                # Unexpected exception (not a known JSONRPC/domain error) — log traceback
                logger.exception(exception)

            data = getattr(exception, "data", None)
            if data is None:
                data = {
                    "exceptionType": type(exception).__name__,
                }
            code = getattr(exception, "code", None)
            if code is None:
                code = JsonRpcErrorCode.INTERNAL_ERROR
            self._hop_stage(hop, "handler_ended", capture_stage_time())
            self._record_handler_outcome(hop, "raised")
            self._send_error_response(
                request_id=message_id,
                code=code,
                message=message,
                data=data,
                hop=hop,
            )
        finally:
            self._inbound_requests.pop(message_id, None)
            current_task = asyncio.current_task()
            with contextlib.suppress(ValueError):
                self._async_tasks.remove(current_task)

    async def run_notification_impl(self, impl_coro) -> None:
        try:
            await impl_coro
        except Exception as exception:  # noqa: BLE001
            logger.warning(
                f"Error occured on running handler of message | {self.readable_id}"
            )
            logger.exception(exception)
        finally:
            current_task = asyncio.current_task()
            with contextlib.suppress(ValueError):
                self._async_tasks.remove(current_task)

    async def _connect_to_server_io(self, timeout: float | None) -> None:
        if self.communication_type == CommunicationType.TCP:
            assert self._tcp_port_future is not None

            try:
                await asyncio.wait_for(self._tcp_port_future, timeout)
            except ServerExitedBeforePort as exception:
                for task in self._async_tasks_in_io_thread:
                    task.cancel()

                raise ServerFailedToStart(
                    f"Server exited before publishing TCP port (exit code: {exception.return_code})"
                    f"\nStartup timeline: {self.startup_timeline.describe(time.monotonic())}"
                    f"{self._stdout_tail()}"
                    f"{self._stderr_tail()}"
                ) from exception
            except TimeoutError as exception:
                for task in self._async_tasks_in_io_thread:
                    task.cancel()

                process_snapshot = (
                    describe_spawned_processes(self.pid)
                    if self.pid is not None
                    else "process snapshot unavailable: server pid unknown"
                )
                raise ServerFailedToStart(
                    f"Didn't get port in {timeout} seconds"
                    f"\nStartup timeline: {self.startup_timeline.describe(time.monotonic())}"
                    f"\nServer processes at timeout:\n{process_snapshot}"
                    f"{self._stdout_tail()}{self._stderr_tail()}"
                ) from exception

            port = self._tcp_port_future.result()
            logger.debug(f"Got port {port} | {self.readable_id}")

            try:
                self._reader, self._writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                self.startup_timeline.connected_at = time.monotonic()
            except Exception as exception:
                logger.exception(exception)

                for task in self._async_tasks_in_io_thread:
                    task.cancel()

                raise ServerFailedToStart(
                    f"Failed to open connection: {exception}"
                ) from exception

        assert self._reader is not None and self._writer is not None

        hop_state: _ReaderHopState | None = None
        if self._hop_observer is not None:
            hop_state = _ReaderHopState(
                observer=self._hop_observer,
                sync_hops=self._sync_request_hops,
            )
        task = asyncio.create_task(
            read_messages_from_reader(
                self._reader,
                self.in_message_queue.sync_q,
                self._sync_request_futures,
                self._expected_result_type_by_msg_id,
                self._stop_event,
                server_id=self.readable_id,
                hop_state=hop_state,
            )
        )
        task.add_done_callback(
            functools.partial(
                task_done_log_callback,
                task_id=f"read_messages_from_reader|{self.readable_id}",
            )
        )
        self._async_tasks_in_io_thread.append(task)

        task = asyncio.create_task(self._send_outgoing_loop(self._writer))
        task.add_done_callback(
            functools.partial(
                task_done_log_callback,
                task_id=f"send_messages_from_queue|{self.readable_id}",
            )
        )
        self._async_tasks_in_io_thread.append(task)

    def _stderr_tail(self, max_lines: int = 30) -> str:
        if not self._stderr_buffer:
            return ""

        recent = "\n".join(self._stderr_buffer[-max_lines:])
        return f"\nServer stderr output:\n{recent}"

    def _stdout_tail(self, max_lines: int = 30) -> str:
        if not self._stdout_buffer:
            return ""

        recent = "\n".join(self._stdout_buffer[-max_lines:])
        return f"\nServer stdout output:\n{recent}"


async def start_server(
    cmd: _spawn.SpawnCommand,
    communication_type: CommunicationType,
    out_message_queue: culsans.Queue[bytes],
    stop_event: LoopAwareEvent,
    server_stopped_event: threading.Event,
    server_id: str,
    async_tasks: list[asyncio.Task[typing.Any]],
    debug_port_future: concurrent.futures.Future[int] | None,
    cwd: Path,
    env: dict[str, str],
    stderr_buffer: list[str] | None = None,
    stdout_buffer: list[str] | None = None,
    timeline: StartupTimeline | None = None,
) -> tuple[
    asyncio.StreamReader | None,
    asyncio.StreamWriter | None,
    asyncio.Future[int] | None,
    int,
]:
    logger.debug(f"Starting server process: {cmd}")

    if communication_type == CommunicationType.STDIO:
        # max length of line: in STDIO mode, the whole file can be sent as a single
        # line, increase default limit 64 KBit to 10 MiB
        limit = 1024 * 1024 * 10  # 10 MiB
    elif communication_type == CommunicationType.TCP:
        limit = None
    else:
        raise ValueError(f"Unsupported communication type: {communication_type}")

    # cwd and env are explicit, so that the started server does not depend on the
    # cwd/environ this process happens to have when the spawn reaches the io thread
    server = await _spawn.spawn_process(
        cmd,
        stdin_pipe=communication_type == CommunicationType.STDIO,
        cwd=cwd,
        env=env,
        limit=limit,
    )

    if timeline is not None:
        timeline.spawned_at = time.monotonic()

    logger.debug(f"{server_id} - process id: {server.pid}")

    task = asyncio.create_task(
        log_stderr(server.stderr, stop_event, stderr_buffer, timeline=timeline)
    )
    task.add_done_callback(
        functools.partial(task_done_log_callback, task_id=f"log_stderr|{server_id}")
    )
    async_tasks.append(task)

    # Get reader and writer based on communication type
    if communication_type == CommunicationType.STDIO:
        reader = server.stdout
        writer = server.stdin
        tcp_port_future = None
    else:  # CommunicationType.TCP
        reader = None
        writer = None

        # TODO: read debug port also in stdio
        tcp_port_future = asyncio.Future[int]()
        task = asyncio.create_task(
            read_stdout(
                server.stdout,
                stop_event,
                tcp_port_future,
                server.pid,
                debug_port_future,
                stdout_buffer,
                timeline=timeline,
            )
        )
        task.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"read_stdout|{server_id}"
            )
        )
        async_tasks.append(task)

        logger.debug(f"Wait for port of {server.pid} | {server_id}")

    task = asyncio.create_task(
        wait_for_stop_event_and_clean(
            stop_event,
            server,
            async_tasks,
            server_stopped_event,
            out_message_queue.async_q,
            port_future=tcp_port_future,
        )
    )
    task.add_done_callback(
        functools.partial(
            task_done_log_callback, task_id=f"wait_for_stop_event_and_clean|{server_id}"
        )
    )

    logger.debug(
        f"Server {server.pid} started with {communication_type.name} | {server_id}"
    )

    return (reader, writer, tcp_port_future, server.pid)


async def wait_for_stop_event_and_clean(
    stop_event: threading.Event,
    server_process: asyncio.subprocess.Process,
    tasks: list[asyncio.Task[typing.Any]],
    server_stopped_event: threading.Event,
    out_message_queue: culsans.AsyncQueue[bytes],
    port_future: asyncio.Future[int] | None,
) -> None:
    # wait either on stop event (=user asks to stop the client) or end of the server
    # process
    logger.debug("Wait on one of tasks")
    wait_stop_task = asyncio.create_task(stop_event.wait_async())
    wait_process_task = asyncio.create_task(server_process.wait())
    done, _ = await asyncio.wait(
        [wait_stop_task, wait_process_task], return_when=asyncio.FIRST_COMPLETED
    )
    logger.debug("One of tasks to wait is done")

    process_finished_first = wait_process_task in done
    if process_finished_first and port_future is not None and not port_future.done():
        port_future.set_exception(ServerExitedBeforePort(server_process.returncode))

    if not stop_event.is_set():
        stop_event.set()

    # close the WriterFromQueue
    await out_message_queue.put(QUEUE_END)

    if server_process.returncode is None:
        logger.debug("Wait for the end of server process")
        _ = await server_process.wait()
        logger.debug(f"Server process ended with code {server_process.returncode}")

    for task in tasks:
        if not task.done():
            task.cancel()

    server_stopped_event.set()
    logger.debug("Cleaned resources of client")


async def log_stderr(
    stderr: asyncio.StreamReader,
    stop_event: threading.Event,
    stderr_buffer: list[str] | None = None,
    *,
    timeline: StartupTimeline | None = None,
) -> None:
    """Read and log stderr output from the subprocess."""
    logger.debug("Start reading logs from stderr")
    try:
        while not stop_event.is_set():
            line = await stderr.readline()
            if not line:
                break
            if timeline is not None and timeline.first_output_at is None:
                timeline.first_output_at = time.monotonic()
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if stderr_buffer is not None:
                stderr_buffer.append(decoded)
    except asyncio.CancelledError:
        pass

    logger.debug("End reading logs from stderr")


# Max startup-window stdout lines retained per ER for a start-failure diagnostic
# (see read_stdout). Bounded so a chatty ER cannot grow WM memory before its port.
_STDOUT_STARTUP_BUFFER_MAX = 200


async def read_stdout(
    stdout: asyncio.StreamReader,
    stop_event: threading.Event,
    port_future: asyncio.Future[int],
    server_pid: int,
    debug_port_future: concurrent.futures.Future[int] | None,
    stdout_buffer: list[str] | None = None,
    *,
    timeline: StartupTimeline | None = None,
) -> None:
    logger.debug(f"Start reading logs from stdout | {server_pid}")
    try:
        while not stop_event.is_set():
            try:
                line = await stdout.readline()
            except ValueError as exception:
                logger.error(exception)
                continue

            if not line:
                break
            if timeline is not None and timeline.first_output_at is None:
                timeline.first_output_at = time.monotonic()
            if b"Serving on (" in line:
                match = re.search(rb"Serving on \('[\d.]+', (\d+)\)", line)
                if match:
                    port = int(match.group(1))
                    if timeline is not None and timeline.port_line_at is None:
                        timeline.port_line_at = time.monotonic()
                    if not port_future.done():
                        port_future.set_result(port)
            elif b"Debug session:" in line:
                match = re.search(rb"Debug session: [\d.]+:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    if debug_port_future is not None and not debug_port_future.done():
                        debug_port_future.set_result(port)
            elif stdout_buffer is not None and not port_future.done():
                # REQUIREMENT (ADR-0049 edge case): before the RPC channel exists the
                # ER writes its own loguru diagnostics to stdout, and er/logRecords
                # forwarding cannot run yet. Retain this startup window (bounded) so a
                # ServerFailedToStart diagnostic can surface *why* the server never came up
                # — otherwise the actionable output is silently discarded. Capture stops
                # once the port arrives (channel is up; forwarding takes over).
                stdout_buffer.append(line.decode("utf-8", errors="replace").rstrip())
                if len(stdout_buffer) > _STDOUT_STARTUP_BUFFER_MAX:
                    del stdout_buffer[0]
    except asyncio.CancelledError:
        pass
    # except Exception as exception:
    #     # catch all unexpected exception to log them properly and to get explicit log
    #     # about end of reading
    #     logger.exception(exception)

    logger.debug(f"End reading logs from stdout | {server_pid}")


async def send_messages_from_queue(
    queue: culsans.AsyncQueue[bytes], writer: asyncio.StreamWriter
) -> None:
    logger.debug("Start sending messages from queue")

    try:
        while True:
            message = await queue.get()
            if isinstance(message, OutputDrain):
                if not message.completion.done():
                    with contextlib.suppress(Exception):
                        message.completion.set_result(None)
                continue
            if message == QUEUE_END:
                writer.close()
                logger.debug("Queue was closed, stop sending")
                break
            writer.write(message)
            await writer.drain()
    except asyncio.CancelledError:
        ...

    queue.shutdown()
    logger.debug("End sending messages from queue")


CONTENT_LENGTH_PATTERN = re.compile(rb"^Content-Length: (\d+)\r\n$")


async def read_messages_from_reader(
    reader: asyncio.StreamReader,
    message_queue: culsans.SyncQueue[typing.Any],
    request_futures: dict[str, concurrent.futures.Future[typing.Any]],
    result_types: dict[str, typing.Any],
    stop_event: threading.Event,
    server_id: str,
    hop_state: _ReaderHopState | None = None,
) -> None:
    content_length = 0

    try:
        while not stop_event.is_set():
            try:
                try:
                    header = await reader.readline()
                except ValueError:
                    logger.error(f"Value error in readline of {server_id}")
                    continue
                except ConnectionResetError:
                    logger.warning(
                        f"Server {server_id} closed the connection(ConnectionResetError), stop the client"
                    )
                    stop_event.set()
                    break

                if not header:
                    if reader.at_eof():
                        logger.debug(f"Reader reached EOF | {server_id}")
                        break
                    continue

                # Extract content length if possible
                if not content_length:
                    match = CONTENT_LENGTH_PATTERN.fullmatch(header)
                    if match:
                        content_length = int(match.group(1))
                        logger.debug(f"Content length | {server_id}: {content_length}")
                    else:
                        logger.debug(
                            f"Not matched content length: {header} | {server_id}"
                        )

                # Check if all headers have been read (as indicated by an empty line \r\n)
                if content_length and not header.strip():
                    # Read body
                    body = None
                    try:
                        body = await reader.readexactly(content_length)
                    except asyncio.IncompleteReadError as error:
                        logger.debug(
                            f"Incomplete read error: {error} | {server_id} : {error.partial}"
                        )
                        content_length = 0
                        continue
                    except ConnectionResetError:
                        logger.warning(
                            f"Server {server_id} closed the connection(ConnectionResetError), stop the client"
                        )
                        stop_event.set()
                        break

                    if not body:
                        content_length = 0
                        continue

                    received = capture_stage_time()

                    logger.trace(f"Got content {server_id}: {body}")
                    try:
                        message = json.loads(body)
                    except json.JSONDecodeError as exc:
                        logger.error(
                            f"Failed to parse JSON message: {exc} | {server_id}"
                        )
                        continue
                    finally:
                        # Reset
                        content_length = 0

                    if not isinstance(message, dict):
                        logger.error("JSON Message expected to be a dict")
                        continue
                    if "jsonrpc" not in message:
                        logger.error("JSON Message expected to contain 'jsonrpc' key")
                        continue

                    if message["jsonrpc"] != JsonRpcClient.VERSION:
                        logger.warning(f'Unknown message "{message}" | {server_id}')
                        continue

                    # error should be also handled here
                    is_response = (
                        "id" in message
                        and "error" not in message
                        and "method" not in message
                    )

                    if is_response:
                        logger.debug(f"Response message received. | {server_id}")
                        msg_id = message["id"]
                        raw_result = message.get("result", None)
                        future = request_futures.pop(msg_id, None)

                        if future is not None:
                            observer: IHopObserver | None = (
                                hop_state.observer if hop_state is not None else None
                            )
                            hop: HopToken | None = (
                                hop_state.sync_hops.get(msg_id)
                                if hop_state is not None
                                else None
                            )
                            if (
                                hop is not None
                                and observer is not None
                                and not future.cancelled()
                            ):
                                call_guarded(
                                    observer,
                                    lambda observer=observer, hop=hop, received=received: (
                                        observer.stage(hop, "frame_read", received)
                                    ),
                                )
                                call_guarded(
                                    observer,
                                    lambda observer=observer, hop=hop: observer.stage(
                                        hop,
                                        "response_resolved",
                                        capture_stage_time(),
                                    ),
                                )
                            try:
                                result_type = result_types[msg_id]
                            except KeyError:
                                logger.error(
                                    f"Result type not found for message {msg_id}"
                                )
                                continue

                            try:
                                result = _converter.structure(raw_result, result_type)
                            except cattrs.ClassValidationError as error:
                                exception = InvalidResponse(str(error))
                                if not future.cancelled():
                                    future.set_exception(exception)
                                continue

                            logger.debug(
                                f'Received result for message "{msg_id}" | {server_id}'
                            )
                            if not future.cancelled():
                                future.set_result(result)
                        else:
                            message_queue.put(
                                ReceivedFrame(message=message, received=received)
                            )
                    else:
                        # incoming request or notification
                        message_queue.put(
                            ReceivedFrame(message=message, received=received)
                        )
                else:
                    if not header.startswith(
                        b"Content-Length:"
                    ) and not header.startswith(b"Content-Type:"):
                        logger.debug(
                            f'Something is wrong: {content_length} "{header}" {not header.strip()} | {server_id}'
                        )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"Exception in message reader loop | {server_id}: {exc}"
                )
                # Reset state to avoid infinite loop on persistent errors
                content_length = 0
    except asyncio.CancelledError:
        ...

    message_queue.put_nowait(QUEUE_END)
    logger.debug(f"End reading messages from reader | {server_id}")


__all__ = ["JsonRpcClient"]
