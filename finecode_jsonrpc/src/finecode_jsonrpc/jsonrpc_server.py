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
from typing import Any

from loguru import logger

from finecode_jsonrpc.jsonrpc_client import JsonRpcError

# JSON-RPC error codes
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603
_REQUEST_CANCELLED = -32800


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
    """

    def __init__(self) -> None:
        self._transport: Any | None = None

        self._next_id: int = 0
        # Pending outbound requests (server → client)
        self._request_futures: dict[int, asyncio.Future[Any]] = {}

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
    ) -> None:
        """Register *handler* for incoming JSON-RPC *request* ``method``."""
        self._request_handlers[method] = handler

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
    ) -> Any:
        """Send a request to the client and return the result."""
        msg_id = self._next_id
        self._next_id += 1

        message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._request_futures[msg_id] = future

        assert self._transport is not None, "Transport not attached"
        self._transport.send(message)

        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._request_futures.pop(msg_id, None)
            raise

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a notification to the client (no response expected)."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        assert self._transport is not None, "Transport not attached"
        self._transport.send(message)

    # ------------------------------------------------------------------
    # Internal message dispatch
    # ------------------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        has_id = "id" in message
        has_method = "method" in message
        has_result = "result" in message
        has_error = "error" in message

        if has_id and (has_result or has_error) and not has_method:
            # Response to one of our outbound requests (server → client)
            await self._resolve_response(message["id"], message)
        elif has_id and has_method:
            # Incoming request from client — run as a separate task, not inline.
            # This is load-bearing for $/cancelRequest: _builtin_cancel_request
            # cancels the task by request id. If you ever refactor this to an
            # inline `await handler(params)`, cancellation will silently stop working.
            asyncio.create_task(
                self._execute_request(message),
                name=f"er_request|{message['method']}|{message['id']}",
            )
        elif has_method and not has_id:
            # Incoming notification from client
            await self._handle_incoming_notification(message)
        else:
            logger.warning(f"Unknown message structure: {message}")

    async def _resolve_response(self, msg_id: int, message: dict[str, Any]) -> None:
        future = self._request_futures.pop(msg_id, None)
        if future is None:
            logger.warning(f"No pending future for response id={msg_id}")
            return

        if "error" in message:
            future.set_exception(JsonRpcError(message["error"]))
        else:
            future.set_result(message.get("result"))

    async def _execute_request(self, message: dict[str, Any]) -> None:
        """Execute an incoming request as an isolated task."""
        method = message["method"]
        msg_id = message["id"]
        handler = self._request_handlers.get(method)

        if handler is None:
            logger.warning(f"No handler for request '{method}'")
            self._send_response(msg_id, None, _METHOD_NOT_FOUND, f"Method not found: {method}")
            return

        task = asyncio.current_task()
        assert task is not None
        self._active_request_tasks[msg_id] = task

        try:
            result = await handler(message.get("params"))
            self._transport.send(
                {"jsonrpc": "2.0", "id": msg_id, "result": result}
            )
        except asyncio.CancelledError:
            self._send_response(msg_id, None, _REQUEST_CANCELLED, "Request cancelled")
        except Exception as exc:
            logger.exception(f"Error handling request '{method}': {exc}")
            self._send_response(msg_id, None, _INTERNAL_ERROR, str(exc))
        finally:
            self._active_request_tasks.pop(msg_id, None)

    def _send_response(
        self,
        msg_id: int | str,
        result: Any,
        error_code: int | None = None,
        error_message: str | None = None,
    ) -> None:
        assert self._transport is not None
        if error_code is not None:
            self._transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": error_code, "message": error_message or ""},
                }
            )
        else:
            self._transport.send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _handle_incoming_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        handler = self._notification_handlers.get(method)
        if handler is not None:
            try:
                await handler(message.get("params"))
            except Exception as exc:
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

    async def _handle_exit(self) -> None:
        """Cancel pending outbound request futures when transport closes."""
        err = RuntimeError("Transport closed before response was received")
        for fut in list(self._request_futures.values()):
            if not fut.done():
                fut.set_exception(err)
        self._request_futures.clear()

        # Cancel all active incoming request tasks
        for task in list(self._active_request_tasks.values()):
            if not task.done():
                task.cancel()
        self._active_request_tasks.clear()
