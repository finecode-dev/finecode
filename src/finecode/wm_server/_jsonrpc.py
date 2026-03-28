"""Low-level JSON-RPC framing, protocol types and stubs for the WM TCP server."""
from __future__ import annotations

import asyncio
import json
import typing

from loguru import logger

CONTENT_LENGTH_HEADER = "Content-Length: "

NOT_IMPLEMENTED_CODE = -32002
NOT_IMPLEMENTED_MSG = "Not yet implemented"


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _jsonrpc_response(id: int | str, result: typing.Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _jsonrpc_error(
    id: int | str | None, code: int, message: str
) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Content-Length framing (shared with finecode_jsonrpc)
# ---------------------------------------------------------------------------


async def _read_message(reader: asyncio.StreamReader) -> dict | None:
    """Read one Content-Length framed JSON-RPC message. Returns None on EOF."""
    header_line = await reader.readline()
    if not header_line:
        return None
    header = header_line.decode("utf-8").strip()
    if not header.startswith(CONTENT_LENGTH_HEADER):
        logger.warning(f"FineCode API: unexpected header: {header!r}")
        return None
    content_length = int(header[len(CONTENT_LENGTH_HEADER):])

    # Read the blank separator line
    separator = await reader.readline()
    if separator.strip():
        logger.warning(f"FineCode API: expected blank line, got: {separator!r}")

    body = await reader.readexactly(content_length)
    return json.loads(body.decode("utf-8"))


def _write_message(writer: asyncio.StreamWriter, msg: dict) -> None:
    """Write one Content-Length framed JSON-RPC message."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    writer.write(header + body)


# ---------------------------------------------------------------------------
# Method handler types and stubs
# ---------------------------------------------------------------------------


class _NotImplementedError(Exception):
    """Raised by stubs to signal that the method is not yet implemented."""


MethodHandler = typing.Callable[
    [dict | None, typing.Any],
    typing.Coroutine[typing.Any, typing.Any, typing.Any],
]

NotificationHandler = typing.Callable[
    [dict | None, typing.Any],
    typing.Coroutine[typing.Any, typing.Any, None],
]


def _stub(method_name: str) -> MethodHandler:
    """Create a stub handler that raises _NotImplementedError."""

    async def handler(
        params: dict | None, ws_context: typing.Any
    ) -> typing.Any:
        raise _NotImplementedError(f"{method_name}: {NOT_IMPLEMENTED_MSG}")

    handler.__doc__ = f"Stub for {method_name}. See docs/wm-protocol.md."
    return handler


def _notification_stub(method_name: str) -> NotificationHandler:
    """Create a stub notification handler that logs and does nothing."""

    async def handler(
        params: dict | None, ws_context: typing.Any
    ) -> None:
        logger.trace(f"FineCode API: notification {method_name} received (stub, ignoring)")

    handler.__doc__ = f"Stub for {method_name}. See docs/wm-protocol.md."
    return handler
