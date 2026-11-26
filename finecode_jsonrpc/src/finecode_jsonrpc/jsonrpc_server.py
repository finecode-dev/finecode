"""Server-side JSON-RPC session.

:class:`JsonRpcServerSession` is the server counterpart of
:class:`~finecode_jsonrpc.jsonrpc_client.JsonRpcSessionImpl`.  It runs on top
of a server transport (:class:`~finecode_jsonrpc.server_transport.ServerStdioTransport`
or :class:`~finecode_jsonrpc.server_transport.TcpServerTransport`) and provides:

- Handler registration for incoming requests and notifications
  (``on_request`` / ``on_notification``)
- The ability to send requests and notifications *to the client*
  (``send_request`` / ``send_notification``)
- Concurrent request execution: each incoming request runs as its own task so
  that long-running handlers do not block the dispatch loop and
  ``$/cancelRequest`` notifications can interrupt them.

Unlike the client session, no ``AsyncIOThread`` is needed — the server session
runs entirely on the event loop that calls :meth:`attach`.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import dataclasses
import time
import uuid
from typing import Any

from loguru import logger

from finecode_jsonrpc import error_codes
from finecode_jsonrpc._hop_guard import (
    call_guarded,
    guarded_handler_scope,
)
from finecode_jsonrpc.cancel_reply import (
    AwaitReplyAfterCancel,
    ReplyAfterCancel,
    ReplyAfterCancelKind,
)
from finecode_jsonrpc.client import RequestCancelledError
from finecode_jsonrpc.jsonrpc_client import JsonRpcError
from finecode_jsonrpc.tracing import (
    CallerOutcome,
    HandlerOutcome,
    HopIdentity,
    HopStage,
    HopToken,
    IHopObserver,
    ITracingHooks,
    StageTime,
    capture_stage_time,
)

# JSON-RPC error codes. Defined in `error_codes`, re-exported here because
# callers across the codebase compare against `REQUEST_CANCELLED` by this name.
_METHOD_NOT_FOUND = error_codes.METHOD_NOT_FOUND
_INTERNAL_ERROR = error_codes.INTERNAL_ERROR


@dataclasses.dataclass(frozen=True)
class CancelNotice:
    """A notification to write when nobody waits for a request's answer anymore."""

    method: str
    params: collections.abc.Callable[[int], dict[str, Any]]
    on_timeout: bool = False


REQUEST_CANCELLED = error_codes.DEFAULT_REQUEST_CANCELLED


class JsonRpcHandlerError(Exception):
    """Raise from a request handler to return a specific JSON-RPC error code.

    Use this instead of letting an arbitrary exception propagate (which would
    always produce INTERNAL_ERROR -32603). ``data`` becomes the error object's
    ``data`` member; it is omitted from the response when ``None``.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class JsonRpcServerSession:
    """Bidirectional JSON-RPC session for the *server* role.

    Example usage::

        session = JsonRpcServerSession()
        session.on_request("my/method", handle_my_method)
        session.on_notification("my/event", handle_my_event)

        transport = ServerStdioTransport()
        session.attach(transport)
        await transport.start()
        # session is now active

    Handlers for incoming requests receive the raw ``params`` dict (or ``None``)
    and must return a JSON-serialisable result::

        async def handle_my_method(params: dict | None) -> Any:
            return {"result": 42}

    To send a request to the *client*::

        result = await session.send_request("workspace/applyEdit", params)

    To send a notification to the *client*::

        await session.send_notification("$/progress", {"token": t, "value": v})

    ``$/cancelRequest`` is handled internally: when the client sends it,
    the corresponding in-flight request task is cancelled and a cancellation
    error is sent back to the client.

    In the other direction, ``send_request(..., cancel_on_abandon=True)`` sends
    ``$/cancelRequest`` to the client when the awaiting task is cancelled, so the
    client can stop work nobody waits for. It is opt-in per request because a
    client may treat a cancel as the end of the request.
    """

    def __init__(
        self,
        tracing: ITracingHooks | None = None,
        hop_observer: IHopObserver | None = None,
    ) -> None:
        self._transport: Any | None = None
        self._tracing = tracing
        self._hop_observer = hop_observer

        self._connection_id: str = ""
        self._peer_id: str = "client"
        self._transport_closed = False

        self._next_id: int = 0
        # Pending outbound requests (server → client)
        self._request_futures: dict[int, asyncio.Future[Any]] = {}
        # Open caller hops (server → client requests) by wire id. An entry
        # outlives its future after a timeout so a late response can still be
        # matched; everything is released in _resolve_response/_handle_exit.
        self._caller_hops: dict[int, tuple[HopToken, str]] = {}
        # Ids of opted-in requests (``cancel_on_abandon``) whose caller was
        # cancelled. The client still answers them; the late response is
        # dropped quietly and the id is released then or in _handle_exit.
        self._cancelled_request_ids: set[int] = set()

        self._notification_handlers: dict[
            str,
            collections.abc.Callable[
                [dict[str, Any] | None], collections.abc.Awaitable[None]
            ],
        ] = {}
        self._request_handlers: dict[
            str,
            collections.abc.Callable[
                [dict[str, Any] | None], collections.abc.Awaitable[Any]
            ],
        ] = {}

        # Tasks currently executing incoming requests, keyed by request id
        self._active_request_tasks: dict[int | str, asyncio.Task[None]] = {}
        # Strong references for _execute_request tasks between create_task()
        # and their first scheduling, so they cannot be garbage-collected
        # before _execute_request registers them in _active_request_tasks.
        self._pending_request_tasks: set[asyncio.Task[None]] = set()
        # Incoming requests whose task exists but has not run its first step,
        # keyed by request id. A $/cancelRequest for one of them is honoured
        # without ever running the handler (see _builtin_cancel_request).
        self._pending_request_by_id: dict[int | str, tuple[asyncio.Task[None], str]] = {}
        # Request ids cancelled before their task ran its first step, mapped
        # to the request method. _execute_request answers them cancelled
        # with that method's not-started data instead of running the handler.
        self._cancelled_before_start: dict[int | str, str] = {}
        # Method-specific ``error.data`` for a request cancelled before its
        # handler started (see on_request).
        self._request_not_started_data: dict[str, Any] = {}

        # Register built-in cancel handler
        self._notification_handlers["$/cancelRequest"] = self._builtin_cancel_request

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def attach(self, transport: Any) -> None:
        """Wire session to *transport* by registering message/exit callbacks.

        Call this before starting the transport.
        """
        self._transport = transport
        self._transport_closed = False
        self._connection_id = uuid.uuid4().hex
        self._peer_id = getattr(transport, "_readable_id", "") or "client"
        if self._hop_observer is not None:
            transport.attach_hop_observer(self._hop_observer)
            transport.on_message_timed(self._handle_message_timed)
        else:
            transport.on_message(self._handle_message)
        transport.on_exit(self._handle_exit)

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def on_request(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[Any]
        ],
        *,
        not_started_cancel_data: collections.abc.Mapping[str, object] | None = None,
    ) -> None:
        """Register *handler* for incoming JSON-RPC *request* ``method``.

        With *not_started_cancel_data*, a request cancelled before its task
        ran its first step is answered cancelled carrying that mapping as
        ``error.data``, and the handler never runs.
        """
        self._request_handlers[method] = handler
        if not_started_cancel_data is not None:
            self._request_not_started_data[method] = dict(not_started_cancel_data)

    def on_notification(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[None]
        ],
    ) -> None:
        """Register *handler* for incoming JSON-RPC *notification* ``method``."""
        self._notification_handlers[method] = handler

    # ------------------------------------------------------------------
    # Send (server → client)
    # ------------------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        *,
        cancel_on_abandon: bool = False,
        cancel_notice: CancelNotice | None = None,
        await_reply_after_cancel: AwaitReplyAfterCancel | None = None,
    ) -> Any:
        """Send a request to the client and return the result.

        With *cancel_on_abandon*, cancelling the awaiting task writes a
        ``$/cancelRequest`` frame for this request before the ``CancelledError``
        leaves this method. A timeout is not an abandonment and sends none.

        With *cancel_notice*, cancelling the awaiting task writes the
        configured notification instead, and a timeout writes it only when
        ``on_timeout`` is set. Passing more than one of *cancel_on_abandon*,
        *cancel_notice* and *await_reply_after_cancel* raises ``ValueError``.

        With *await_reply_after_cancel*, cancelling the awaiting task writes
        ``$/cancelRequest`` and then keeps waiting for the reply up to
        ``timeout`` seconds after the first cancellation. It then raises
        ``RequestCancelledError`` carrying what the wait observed.
        """
        if sum(
            [
                cancel_on_abandon,
                cancel_notice is not None,
                await_reply_after_cancel is not None,
            ]
        ) > 1:
            raise ValueError(
                "cancel_on_abandon, cancel_notice and await_reply_after_cancel"
                " are exclusive"
            )
        if self._transport_closed:
            raise RuntimeError("Transport closed before response was received")
        msg_id = self._next_id
        self._next_id += 1

        hop = self._open_caller(msg_id, method)

        message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        observer = self._hop_observer
        if hop is not None and observer is not None:
            traceparent = call_guarded(
                observer, lambda: observer.caller_traceparent(hop)
            )
            if traceparent is not None:
                message["_meta"] = {"traceparent": traceparent}

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._request_futures[msg_id] = future
        if hop is not None:
            self._caller_hops[msg_id] = (hop, method)

        assert self._transport is not None, "Transport not attached"
        if hop is not None:
            self._transport.send(message, hop=hop)
        else:
            self._transport.send(message)

        try:
            result = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            if cancel_notice is not None and cancel_notice.on_timeout:
                self._request_futures.pop(msg_id, None)
                self._caller_hops.pop(msg_id, None)
                self._cancelled_request_ids.add(msg_id)
                self._transport.send(
                    {
                        "jsonrpc": "2.0",
                        "method": cancel_notice.method,
                        "params": cancel_notice.params(msg_id),
                    }
                )
            else:
                self._request_futures.pop(msg_id, None)
            self._finish_caller(hop, "timeout")
            raise
        except asyncio.CancelledError as exc:
            if await_reply_after_cancel is not None:
                raise await self._await_reply_after_cancel(
                    msg_id, future, hop, await_reply_after_cancel
                ) from exc
            if cancel_on_abandon:
                self._request_futures.pop(msg_id, None)
                self._caller_hops.pop(msg_id, None)
                self._cancelled_request_ids.add(msg_id)
                self._transport.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "$/cancelRequest",
                        "params": {"id": msg_id},
                    }
                )
            elif cancel_notice is not None:
                self._request_futures.pop(msg_id, None)
                self._caller_hops.pop(msg_id, None)
                self._cancelled_request_ids.add(msg_id)
                self._transport.send(
                    {
                        "jsonrpc": "2.0",
                        "method": cancel_notice.method,
                        "params": cancel_notice.params(msg_id),
                    }
                )
            self._finish_caller(hop, "cancelled")
            raise
        except JsonRpcError:
            self._finish_caller(hop, "remote_error")
            raise
        except RuntimeError:
            self._finish_caller(hop, "disconnected")
            raise
        self._finish_caller(hop, "result")
        return result

    def _reply_from_settled(self, future: asyncio.Future[Any]) -> ReplyAfterCancel:
        if future.cancelled():
            return ReplyAfterCancel(kind=ReplyAfterCancelKind.DISCONNECTED)
        exc = future.exception()
        if exc is None:
            return ReplyAfterCancel(
                kind=ReplyAfterCancelKind.RESULT, result=future.result()
            )
        if isinstance(exc, JsonRpcError):
            return ReplyAfterCancel(
                kind=ReplyAfterCancelKind.ERROR,
                error_code=exc.code,
                error_data=exc.data,
            )
        return ReplyAfterCancel(kind=ReplyAfterCancelKind.DISCONNECTED)

    async def _await_reply_after_cancel(
        self,
        msg_id: int,
        future: asyncio.Future[Any],
        hop: HopToken | None,
        option: AwaitReplyAfterCancel,
    ) -> RequestCancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            current.uncancel()
        if future.cancelled():
            # The initial wait cancelled the future itself (asyncio.wait_for
            # cancels what it waits on), so a later reply would be dropped as
            # "already cancelled". Swap in a fresh future under the same id
            # so the post-cancel wait can still observe the reply.
            future = asyncio.get_running_loop().create_future()
            self._request_futures[msg_id] = future
        if future.done() and not future.cancelled():
            reply = self._reply_from_settled(future)
            self._request_futures.pop(msg_id, None)
            self._caller_hops.pop(msg_id, None)
            self._finish_caller(hop, "cancelled")
            return RequestCancelledError(msg_id, reply=reply)
        assert self._transport is not None, "Transport not attached"
        self._transport.send(
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": msg_id},
            }
        )
        bound = option.timeout
        deadline = None if bound is None else time.monotonic() + bound
        while True:
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._request_futures.pop(msg_id, None)
                    self._caller_hops.pop(msg_id, None)
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
                    settled = await asyncio.wait_for(
                        asyncio.shield(future), remaining
                    )
            except TimeoutError:
                self._request_futures.pop(msg_id, None)
                self._caller_hops.pop(msg_id, None)
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
                    reply = self._reply_from_settled(future)
                    self._request_futures.pop(msg_id, None)
                    self._caller_hops.pop(msg_id, None)
                    self._finish_caller(hop, "cancelled")
                    return RequestCancelledError(msg_id, reply=reply)
                continue
            except JsonRpcError as exc:
                self._request_futures.pop(msg_id, None)
                self._caller_hops.pop(msg_id, None)
                self._finish_caller(hop, "cancelled")
                return RequestCancelledError(
                    msg_id,
                    reply=ReplyAfterCancel(
                        kind=ReplyAfterCancelKind.ERROR,
                        error_code=exc.code,
                        error_data=exc.data,
                    ),
                )
            except RuntimeError:
                self._request_futures.pop(msg_id, None)
                self._caller_hops.pop(msg_id, None)
                self._finish_caller(hop, "cancelled")
                return RequestCancelledError(
                    msg_id,
                    reply=ReplyAfterCancel(
                        kind=ReplyAfterCancelKind.DISCONNECTED
                    ),
                )
            else:
                self._request_futures.pop(msg_id, None)
                self._caller_hops.pop(msg_id, None)
                self._finish_caller(hop, "cancelled")
                return RequestCancelledError(
                    msg_id,
                    reply=ReplyAfterCancel(
                        kind=ReplyAfterCancelKind.RESULT, result=settled
                    ),
                )

    def _open_caller(self, msg_id: int, method: str) -> HopToken | None:
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
            lambda: observer.open_caller(identity, self._peer_id, capture_stage_time()),
        )

    def _finish_caller(self, hop: HopToken | None, outcome: CallerOutcome) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        now = capture_stage_time()
        call_guarded(observer, lambda: observer.stage(hop, "caller_completed", now))
        call_guarded(observer, lambda: observer.close_caller(hop, outcome, now))

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a notification to the client (no response expected)."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if self._tracing is not None:
            traceparent = self._tracing.get_traceparent()
            if traceparent is not None:
                message["_meta"] = {"traceparent": traceparent}
        assert self._transport is not None, "Transport not attached"
        self._transport.send(message)
        if self._tracing is not None:
            self._tracing.notification_sent(method)

    # ------------------------------------------------------------------
    # Internal message dispatch
    # ------------------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        await self._dispatch(message, None)

    async def _handle_message_timed(
        self, message: dict[str, Any], frame_read: StageTime
    ) -> None:
        await self._dispatch(message, frame_read)

    async def _dispatch(
        self, message: dict[str, Any], frame_read: StageTime | None
    ) -> None:
        has_id = "id" in message
        has_method = "method" in message
        has_result = "result" in message
        has_error = "error" in message

        if has_id and (has_result or has_error) and not has_method:
            # Response to one of our outbound requests (server → client)
            await self._resolve_response(message["id"], message, frame_read)
        elif has_id and has_method:
            # Incoming request from client — run as a separate task, not inline.
            # This is load-bearing for $/cancelRequest: _builtin_cancel_request
            # cancels the task by request id. If you ever refactor this to an
            # inline `await handler(params)`, cancellation will silently stop working.
            hop = self._open_receiver(message, frame_read)
            if hop is not None:
                self._hop_stage(hop, "dispatched", capture_stage_time())
                coro = self._execute_request_observed(message, hop)
            else:
                coro = self._execute_request(message)
            task = asyncio.create_task(
                coro,
                name=f"er_request|{message['method']}|{message['id']}",
            )
            self._pending_request_tasks.add(task)
            self._pending_request_by_id[message["id"]] = (
                task,
                message["method"],
            )

            def _release_pending(
                done: asyncio.Task[None],
                _msg_id: int | str = message["id"],
            ) -> None:
                self._pending_request_tasks.discard(done)
                pending = self._pending_request_by_id.get(_msg_id)
                if pending is not None and pending[0] is done:
                    del self._pending_request_by_id[_msg_id]

            task.add_done_callback(_release_pending)
        elif has_method and not has_id:
            # Incoming notification from client
            await self._handle_incoming_notification(message)
        else:
            logger.warning(f"Unknown message structure: {message}")

    def _open_receiver(
        self, message: dict[str, Any], frame_read: StageTime | None
    ) -> HopToken | None:
        observer = self._hop_observer
        if observer is None or frame_read is None:
            return None
        traceparent = (message.get("_meta") or {}).get("traceparent")
        identity = HopIdentity(
            connection_id=self._connection_id,
            role="receiver",
            request_id=message["id"],
            method=message["method"],
        )
        return call_guarded(
            observer, lambda: observer.open_receiver(identity, traceparent, frame_read)
        )

    def _hop_stage(
        self, hop: HopToken | None, stage: HopStage, at: StageTime | None
    ) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(observer, lambda: observer.stage(hop, stage, at))

    def _record_handler_outcome(
        self, hop: HopToken | None, outcome: HandlerOutcome
    ) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(observer, lambda: observer.handler_outcome(hop, outcome))

    async def _resolve_response(
        self,
        msg_id: int,
        message: dict[str, Any],
        frame_read: StageTime | None = None,
    ) -> None:
        if msg_id in self._cancelled_request_ids:
            self._cancelled_request_ids.discard(msg_id)
            logger.debug(f"Dropping response for abandoned request id={msg_id}")
            return

        hop_info = self._caller_hops.pop(msg_id, None)
        future = self._request_futures.pop(msg_id, None)
        if future is None:
            logger.warning(f"No pending future for response id={msg_id}")
            self._report_unmatched_response(msg_id, hop_info, frame_read)
            return

        if future.cancelled():
            logger.debug(f"Future for id={msg_id} already cancelled, dropping response")
            return

        observer = self._hop_observer
        if hop_info is not None and observer is not None:
            hop, _method = hop_info
            at = frame_read if frame_read is not None else capture_stage_time()
            call_guarded(observer, lambda: observer.stage(hop, "frame_read", at))
            call_guarded(
                observer,
                lambda: observer.stage(hop, "response_resolved", capture_stage_time()),
            )

        if "error" in message:
            future.set_exception(JsonRpcError(message["error"]))
        else:
            future.set_result(message.get("result"))

    def _report_unmatched_response(
        self,
        msg_id: int,
        hop_info: tuple[HopToken, str] | None,
        frame_read: StageTime | None,
    ) -> None:
        observer = self._hop_observer
        if observer is None:
            return
        method = hop_info[1] if hop_info is not None else ""
        identity = HopIdentity(
            connection_id=self._connection_id,
            role="caller",
            request_id=msg_id,
            method=method,
        )
        at = frame_read if frame_read is not None else capture_stage_time()
        call_guarded(observer, lambda: observer.response_without_waiter(identity, at))

    async def _execute_request(self, message: dict[str, Any]) -> None:
        """Execute an incoming request as an isolated task."""
        method = message["method"]
        msg_id = message["id"]
        self._pending_request_by_id.pop(msg_id, None)
        cancelled_method = self._cancelled_before_start.pop(msg_id, None)
        if cancelled_method is not None:
            self._send_response(
                msg_id,
                None,
                REQUEST_CANCELLED,
                "Request cancelled",
                self._request_not_started_data.get(cancelled_method),
            )
            return
        traceparent = (message.get("_meta") or {}).get("traceparent")
        handler = self._request_handlers.get(method)

        if handler is None:
            logger.warning(f"No handler for request '{method}'")
            self._send_response(
                msg_id, None, _METHOD_NOT_FOUND, f"Method not found: {method}"
            )
            return

        task = asyncio.current_task()
        assert task is not None
        self._active_request_tasks[msg_id] = task

        span_ctx = (
            self._tracing.server_span(method, traceparent)
            if self._tracing
            else contextlib.nullcontext()
        )
        with span_ctx:
            try:
                result = await handler(message.get("params"))
                self._transport.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            except asyncio.CancelledError:
                self._send_response(
                    msg_id, None, REQUEST_CANCELLED, "Request cancelled"
                )
            except JsonRpcHandlerError as exc:
                self._send_response(msg_id, None, exc.code, exc.message, exc.data)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error handling request '{method}': {exc}")
                self._send_response(msg_id, None, _INTERNAL_ERROR, str(exc))
            finally:
                self._active_request_tasks.pop(msg_id, None)

    async def _execute_request_observed(
        self, message: dict[str, Any], hop: HopToken
    ) -> None:
        """Execute an incoming request with hop stages (observer attached)."""
        observer = self._hop_observer
        assert observer is not None
        method = message["method"]
        msg_id = message["id"]
        self._pending_request_by_id.pop(msg_id, None)
        cancelled_method = self._cancelled_before_start.pop(msg_id, None)
        if cancelled_method is not None:
            self._record_handler_outcome(hop, "cancelled")
            self._send_response(
                msg_id,
                None,
                REQUEST_CANCELLED,
                "Request cancelled",
                self._request_not_started_data.get(cancelled_method),
                hop=hop,
            )
            return
        handler = self._request_handlers.get(method)

        if handler is None:
            logger.warning(f"No handler for request '{method}'")
            self._record_handler_outcome(hop, "not_executed")
            self._send_response(
                msg_id, None, _METHOD_NOT_FOUND, f"Method not found: {method}", hop=hop
            )
            return

        task = asyncio.current_task()
        assert task is not None
        self._active_request_tasks[msg_id] = task

        self._hop_stage(hop, "handler_started", capture_stage_time())
        try:
            with guarded_handler_scope(observer, hop):
                result = await handler(message.get("params"))
        except asyncio.CancelledError:
            self._hop_stage(hop, "handler_ended", capture_stage_time())
            self._record_handler_outcome(hop, "cancelled")
            self._send_response(
                msg_id, None, REQUEST_CANCELLED, "Request cancelled", hop=hop
            )
        except JsonRpcHandlerError as exc:
            self._hop_stage(hop, "handler_ended", capture_stage_time())
            self._record_handler_outcome(hop, "raised")
            self._send_response(msg_id, None, exc.code, exc.message, exc.data, hop=hop)
        except Exception as exc:
            logger.exception(f"Error handling request '{method}': {exc}")
            self._hop_stage(hop, "handler_ended", capture_stage_time())
            self._record_handler_outcome(hop, "raised")
            self._send_response(msg_id, None, _INTERNAL_ERROR, str(exc), hop=hop)
        else:
            self._hop_stage(hop, "handler_ended", capture_stage_time())
            self._record_handler_outcome(hop, "returned")
            self._send_envelope({"jsonrpc": "2.0", "id": msg_id, "result": result}, hop)
        finally:
            self._active_request_tasks.pop(msg_id, None)

    def _send_envelope(self, envelope: dict[str, Any], hop: HopToken | None) -> None:
        assert self._transport is not None
        if hop is not None:
            self._transport.send(envelope, hop=hop)
        else:
            self._transport.send(envelope)

    def _send_response(
        self,
        msg_id: int | str,
        result: Any,
        error_code: int | None = None,
        error_message: str | None = None,
        error_data: Any = None,
        hop: HopToken | None = None,
    ) -> None:
        assert self._transport is not None
        if error_code is not None:
            error: dict[str, Any] = {"code": error_code, "message": error_message or ""}
            if error_data is not None:
                error["data"] = error_data
            envelope: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "error": error}
        else:
            envelope = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        self._send_envelope(envelope, hop)

    async def _handle_incoming_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if self._tracing is not None:
            traceparent = (message.get("_meta") or {}).get("traceparent")
            self._tracing.notification_received(method, traceparent)
        handler = self._notification_handlers.get(method)
        if handler is not None:
            try:
                await handler(message.get("params"))
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error handling notification '{method}': {exc}")

    async def _builtin_cancel_request(self, params: dict[str, Any] | None) -> None:
        """Built-in handler for ``$/cancelRequest``."""
        if not params:
            return
        request_id = params.get("id")
        if request_id is None:
            return
        task = self._active_request_tasks.pop(request_id, None)
        if task is not None and not task.done():
            logger.debug(f"Cancelling request id={request_id}")
            task.cancel()
            return
        pending = self._pending_request_by_id.get(request_id)
        if pending is not None:
            _, method = pending
            logger.debug(f"Cancelling request id={request_id} before it started")
            self._cancelled_before_start[request_id] = method

    async def _handle_exit(self) -> None:
        """Cancel pending outbound request futures when transport closes."""
        self._transport_closed = True
        err = RuntimeError("Transport closed before response was received")
        for fut in list(self._request_futures.values()):
            if not fut.done():
                fut.set_exception(err)
        self._request_futures.clear()
        self._cancelled_request_ids.clear()

        observer = self._hop_observer
        for hop, _method in self._caller_hops.values():
            self._finish_caller(hop, "disconnected")
        self._caller_hops.clear()

        # Cancel all active incoming request tasks
        for task in list(self._active_request_tasks.values()):
            if not task.done():
                task.cancel()
        self._active_request_tasks.clear()

        if observer is not None:
            connection_id = self._connection_id
            call_guarded(observer, lambda: observer.connection_closed(connection_id))
