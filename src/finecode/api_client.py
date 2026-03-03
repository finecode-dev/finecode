"""FineCode API client — JSON-RPC client for the FineCode API server.

Connects to the FineCode API server over TCP using Content-Length framing.
Supports both request/response and server→client notifications via a
background reader loop.

Used by LSP server, MCP server, and potentially CLI.
"""

from __future__ import annotations

import asyncio
import collections.abc
import json
import pathlib

from loguru import logger

CONTENT_LENGTH_HEADER = "Content-Length: "


async def _read_message(reader: asyncio.StreamReader) -> dict | None:
    """Read one Content-Length framed JSON-RPC message. Returns None on EOF."""
    header_line = await reader.readline()
    if not header_line:
        return None
    header_str = header_line.decode("utf-8").strip()
    if not header_str.startswith(CONTENT_LENGTH_HEADER):
        logger.warning(f"ApiClient: unexpected header: {header_str!r}")
        return None
    content_length = int(header_str[len(CONTENT_LENGTH_HEADER):])

    # Blank separator line
    await reader.readline()

    body = await reader.readexactly(content_length)
    return json.loads(body.decode("utf-8"))


class ApiClient:
    """JSON-RPC client using Content-Length framing over TCP.

    After connect(), a background reader loop dispatches incoming messages:
    - Responses (with ``id``) resolve the matching pending request future.
    - Notifications (without ``id``) are dispatched to registered callbacks.
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notification_handlers: dict[
            str, collections.abc.Callable[..., collections.abc.Coroutine]
        ] = {}
        self._reader_task: asyncio.Task | None = None

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, host: str, port: int) -> None:
        self._reader, self._writer = await asyncio.open_connection(host, port)
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info(f"Connected to FineCode API at {host}:{port}")

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None

        # Fail any pending requests.
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Connection closed"))
        self._pending.clear()

    # -- Notifications ------------------------------------------------------

    def on_notification(
        self,
        method: str,
        callback: collections.abc.Callable[..., collections.abc.Coroutine],
    ) -> None:
        """Register an async callback for a server→client notification."""
        self._notification_handlers[method] = callback

    # -- Workspace methods --------------------------------------------------

    async def list_projects(self) -> list[dict]:
        """List all projects in the workspace."""
        return await self.request("workspace/listProjects")

    async def add_dir(self, dir_path: pathlib.Path) -> dict:
        """Add a workspace directory. Returns {projects: [...]}."""
        return await self.request("workspace/addDir", {"dir_path": str(dir_path)})

    async def remove_dir(self, dir_path: pathlib.Path) -> None:
        """Remove a workspace directory."""
        await self.request("workspace/removeDir", {"dir_path": str(dir_path)})

    # -- Low-level request --------------------------------------------------

    async def request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        if self._writer is None:
            raise RuntimeError("Not connected to FineCode API server")

        self._request_id += 1
        rid = self._request_id
        msg = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        }

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future

        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self._writer.write(header + body)
        await self._writer.drain()

        response = await future

        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"API error ({error['code']}): {error['message']}")

        return response.get("result")

    # -- Background reader --------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read messages from the server and dispatch them."""
        try:
            while self._reader is not None:
                msg = await _read_message(self._reader)
                if msg is None:
                    break

                if "id" in msg:
                    # Response to a pending request.
                    future = self._pending.pop(msg["id"], None)
                    if future is not None and not future.done():
                        future.set_result(msg)
                    else:
                        logger.warning(
                            f"ApiClient: received response for unknown id {msg['id']}"
                        )
                else:
                    # Server→client notification.
                    method = msg.get("method")
                    handler = self._notification_handlers.get(method)
                    if handler is not None:
                        asyncio.create_task(handler(msg.get("params")))
                    else:
                        logger.trace(
                            f"ApiClient: unhandled notification {method}"
                        )
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info("ApiClient: server connection lost")
        except Exception:
            logger.exception("ApiClient: error in reader loop")
        finally:
            # Fail any remaining pending requests.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Connection lost"))
            self._pending.clear()
