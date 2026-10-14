"""Method-not-found must arrive as its own type, not a probed code."""

from __future__ import annotations

import asyncio
import json

import pytest

from finecode.wm_client import (
    ApiClient,
    ApiMethodNotFoundError,
    ApiServerError,
)


class _FakeWm:
    """Answers one probe method with a scripted error code."""

    def __init__(self, code: int) -> None:
        self._code = code
        self._server = None
        self._writer = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer) -> None:
        self._writer = writer
        try:
            while True:
                header = await reader.readline()
                if not header:
                    return
                length = int(header.decode().split(":")[1].strip())
                await reader.readline()
                body = json.loads((await reader.readexactly(length)).decode())
                if "method" not in body or "id" not in body:
                    continue
                if body["method"] == "client/initialize":
                    result: dict = {}
                    self._send({"jsonrpc": "2.0", "id": body["id"], "result": result})
                else:
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": {"code": self._code, "message": "wire says no"},
                        }
                    )
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            return

    def _send(self, msg: dict) -> None:
        assert self._writer is not None
        body = json.dumps(msg).encode()
        self._writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    async def stop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def _probe(code: int) -> BaseException:
    server = _FakeWm(code)
    await server.start()
    client = ApiClient()
    try:
        await client.connect("127.0.0.1", server.port)
        with pytest.raises(ApiServerError) as excinfo:
            await client.request("server/getResourceUsage", {})
        return excinfo.value
    finally:
        await client.close()
        await server.stop()


async def test_method_not_found_arrives_as_its_own_type() -> None:
    """Callers must be able to catch staleness without probing codes."""
    exc = await _probe(-32601)

    assert isinstance(exc, ApiMethodNotFoundError)
    assert exc.code == -32601


async def test_other_wire_errors_stay_plain_server_errors() -> None:
    """Only method-not-found is promoted; everything else is unchanged."""
    exc = await _probe(-32603)

    assert type(exc) is ApiServerError
    assert exc.code == -32603
