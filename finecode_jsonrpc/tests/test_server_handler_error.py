"""Requirement tests: a handler error reaches the wire as the error object the
handler described.

REQUIREMENT: when a request handler raises ``JsonRpcHandlerError``, the
response carries its ``code`` and ``message``, and its ``data`` when one is
given. Protocols built on this session define errors whose ``data`` member is
mandatory (for example MCP's unsupported-protocol-version error), so dropping
it would make such errors impossible to send.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from finecode_jsonrpc import JsonRpcHandlerError, JsonRpcServerSession


class _RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._on_message: Any = None

    def on_message(self, callback: Any) -> None:
        self._on_message = callback

    def on_exit(self, callback: Any) -> None:
        pass

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def deliver(self, message: dict[str, Any]) -> None:
        await self._on_message(message)


async def _response_to_raising_handler(
    error: JsonRpcHandlerError,
) -> dict[str, Any]:
    async def handler(_params: dict[str, Any] | None) -> Any:
        raise error

    session = JsonRpcServerSession()
    session.on_request("test/method", handler)
    transport = _RecordingTransport()
    session.attach(transport)

    await transport.deliver({"jsonrpc": "2.0", "id": 1, "method": "test/method"})
    for _ in range(10):
        if transport.sent:
            break
        await asyncio.sleep(0)

    assert len(transport.sent) == 1
    return transport.sent[0]


@pytest.mark.parametrize(
    "data",
    [
        {"supported": ["2026-07-28"], "requested": "1999-01-01"},
        ["a", 1],
        "detail",
        0,
        False,
    ],
)
async def test_handler_error_data_is_sent(data: Any) -> None:
    response = await _response_to_raising_handler(
        JsonRpcHandlerError(-32022, "Unsupported protocol version", data)
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32022,
            "message": "Unsupported protocol version",
            "data": data,
        },
    }


async def test_handler_error_without_data_omits_data_member() -> None:
    response = await _response_to_raising_handler(
        JsonRpcHandlerError(-32602, "Invalid params")
    )

    assert response["error"] == {"code": -32602, "message": "Invalid params"}
