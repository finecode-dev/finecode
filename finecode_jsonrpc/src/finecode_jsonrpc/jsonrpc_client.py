from __future__ import annotations

import asyncio
import collections.abc
import concurrent.futures
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from finecode_jsonrpc import _io_thread
from finecode_jsonrpc.transports import StdioTransport
from loguru import logger


class JsonRpcSessionImpl:
    """IJsonRpcSession implementation using StdioTransport + AsyncIOThread."""

    def __init__(
        self,
        cmd: str,
        cwd: Path | None,
        env: dict[str, str] | None,
        readable_id: str,
    ) -> None:
        self._cmd = cmd
        self._cwd = cwd
        self._env = env
        self._readable_id = readable_id

        self._transport: StdioTransport | None = None
        self._io_thread: _io_thread.AsyncIOThread | None = None

        self._next_id: int = 0
        self._async_request_futures: dict[int, asyncio.Future[Any]] = {}
        self._sync_request_futures: dict[int, concurrent.futures.Future[Any]] = {}
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

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._io_thread = _io_thread.AsyncIOThread()
        self._io_thread.start()

        self._transport = StdioTransport(readable_id=self._readable_id)
        self._transport.on_message(self._handle_message)
        self._transport.on_exit(self._handle_exit)

        # Start transport on the IO thread
        start_future = self._io_thread.run_coroutine(
            self._transport.start(cmd=self._cmd, cwd=self._cwd, env=self._env)
        )
        await asyncio.wrap_future(start_future)

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._transport is not None and self._io_thread is not None:
            try:
                stop_future = self._io_thread.run_coroutine(self._transport.stop())
                await asyncio.wrap_future(stop_future)
            except RuntimeError:
                # IO thread may already be stopped
                pass

        # Cancel pending futures
        for fut in self._async_request_futures.values():
            if not fut.done():
                fut.cancel()
        for fut in self._sync_request_futures.values():
            if not fut.done():
                fut.cancel()
        self._async_request_futures.clear()
        self._sync_request_futures.clear()

        if self._io_thread is not None:
            self._io_thread.stop(timeout=5.0)
            self._io_thread = None

    # ------------------------------------------------------------------
    # Async API
    # ------------------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        msg_id = self._next_id
        self._next_id += 1

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        future: asyncio.Future[Any] = asyncio.Future()
        self._async_request_futures[msg_id] = future

        assert self._transport is not None
        self._transport.send(message)

        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._async_request_futures.pop(msg_id, None)
            raise

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        assert self._transport is not None
        self._transport.send(message)

    # ------------------------------------------------------------------
    # Sync API
    # ------------------------------------------------------------------

    def send_request_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        msg_id = self._next_id
        self._next_id += 1

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._sync_request_futures[msg_id] = future

        assert self._transport is not None
        self._transport.send(message)

        return future.result(timeout=timeout)

    def send_notification_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        assert self._transport is not None
        self._transport.send(message)

    # ------------------------------------------------------------------
    # Server-initiated messages
    # ------------------------------------------------------------------

    def on_notification(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[None]
        ],
    ) -> None:
        self._notification_handlers[method] = handler

    def on_request(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[Any]
        ],
    ) -> None:
        self._request_handlers[method] = handler

    # ------------------------------------------------------------------
    # Internal message dispatch (runs on IO thread)
    # ------------------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        has_id = "id" in message
        has_method = "method" in message
        has_result = "result" in message
        has_error = "error" in message

        if has_id and (has_result or has_error) and not has_method:
            # Response to one of our requests
            msg_id = message["id"]
            await self._resolve_response(msg_id, message)
        elif has_id and has_method:
            # Incoming request from server
            await self._handle_incoming_request(message)
        elif has_method and not has_id:
            # Incoming notification from server
            await self._handle_incoming_notification(message)
        else:
            logger.warning(
                f"Unknown message structure | {self._readable_id}: {message}"
            )

    async def _resolve_response(self, msg_id: int, message: dict[str, Any]) -> None:
        # Try async futures first
        async_future = self._async_request_futures.pop(msg_id, None)
        if async_future is not None:
            # The future lives on the caller's event loop, but this callback
            # runs on the IO thread.  asyncio.Future is not thread-safe, so
            # we must schedule the resolution on the future's own loop.
            loop = async_future.get_loop()
            if "error" in message:
                loop.call_soon_threadsafe(
                    async_future.set_exception, JsonRpcError(message["error"])
                )
            else:
                loop.call_soon_threadsafe(
                    async_future.set_result, message.get("result")
                )
            return

        # Try sync futures
        sync_future = self._sync_request_futures.pop(msg_id, None)
        if sync_future is not None:
            if "error" in message:
                sync_future.set_exception(JsonRpcError(message["error"]))
            else:
                sync_future.set_result(message.get("result"))
            return

        logger.warning(
            f"No pending future for response id={msg_id} | {self._readable_id}"
        )

    async def _handle_incoming_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        handler = self._request_handlers.get(method)
        if handler is None:
            logger.warning(
                f"No handler for server request '{method}' | {self._readable_id}"
            )
            # Send error response
            error_response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }
            assert self._transport is not None
            self._transport.send(error_response)
            return

        try:
            result = await handler(message.get("params"))
        except Exception as exc:
            logger.exception(
                f"Error handling server request '{method}' | {self._readable_id}: {exc}"
            )
            error_response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {
                    "code": -32603,
                    "message": str(exc),
                },
            }
            assert self._transport is not None
            self._transport.send(error_response)
            return

        response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": result,
        }
        assert self._transport is not None
        self._transport.send(response)

    async def _handle_incoming_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        handler = self._notification_handlers.get(method)
        if handler is not None:
            try:
                await handler(message.get("params"))
            except Exception as exc:
                logger.exception(
                    f"Error handling notification '{method}' | {self._readable_id}: {exc}"
                )

    async def _handle_exit(self) -> None:
        # Cancel pending futures when server process exits.
        # Async futures live on the caller's loop — resolve them thread-safely.
        err = RuntimeError("Server process exited before response")
        for fut in list(self._async_request_futures.values()):
            if not fut.done():
                fut.get_loop().call_soon_threadsafe(fut.set_exception, err)
        for fut in list(self._sync_request_futures.values()):
            if not fut.done():
                fut.set_exception(err)


class JsonRpcError(Exception):
    """Error received in a JSON-RPC response."""

    def __init__(self, error: dict[str, Any]) -> None:
        self.code: int = error.get("code", -1)
        self.rpc_message: str = error.get("message", "Unknown error")
        self.data: Any = error.get("data")
        super().__init__(f"JSON-RPC error {self.code}: {self.rpc_message}")


class JsonRpcClientImpl:
    """IJsonRpcClient implementation. Factory for JsonRpcSessionImpl."""

    def session(
        self,
        cmd: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        readable_id: str = "",
    ) -> JsonRpcSessionImpl:
        return JsonRpcSessionImpl(
            cmd=cmd,
            cwd=cwd,
            env=env,
            readable_id=readable_id,
        )
