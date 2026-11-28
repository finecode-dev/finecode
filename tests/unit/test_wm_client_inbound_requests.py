"""An inbound request can be told apart, and can end without being answered.

A client that holds a question for a person has to match the server's later
withdrawal to it by request id, and a question the server has already given up
on must not be answered: the answer would only be discarded, and writing it
would suggest the person's choice took effect. Connection loss still ends every
held request, so a handler never outlives the connection that asked.
"""

from __future__ import annotations

import asyncio
import json
import typing

import pytest

from finecode import wm_client


class _FakeWm:
    """A minimal WM that answers ``client/initialize`` and can ask questions."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.port = 0
        self.received: list[dict] = []
        self.connected = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writer = writer
        self.connected.set()
        try:
            while True:
                header = await reader.readline()
                if not header:
                    return
                length = int(header.decode().split(":")[1].strip())
                await reader.readline()
                body = json.loads((await reader.readexactly(length)).decode())
                self.received.append(body)
                if "method" in body and "id" in body:
                    # A request from the client (e.g. client/initialize).
                    self._send({"jsonrpc": "2.0", "id": body["id"], "result": {}})
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            return

    def _send(self, msg: dict) -> None:
        assert self._writer is not None
        body = json.dumps(msg).encode()
        self._writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    def ask(self, request_id: int, method: str, params: dict | None = None) -> None:
        """Send a server→client *request* — the direction that did not exist."""
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )

    async def answer_for(self, request_id: int, timeout: float = 2.0) -> dict:
        """Wait for the client's response to *request_id*."""

        async def _wait() -> dict:
            while True:
                for msg in self.received:
                    if msg.get("id") == request_id and "method" not in msg:
                        return msg
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def stop(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def fake_wm() -> typing.AsyncIterator[_FakeWm]:
    server = _FakeWm()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def client(fake_wm: _FakeWm) -> typing.AsyncIterator[wm_client.ApiClient]:
    api_client = wm_client.ApiClient()
    try:
        yield api_client
    finally:
        await api_client.close()


async def test_a_callback_registered_for_ids_receives_the_request_id(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    seen: list[tuple[int | str, dict | None]] = []

    async def _handler(request_id: int | str, params: dict | None) -> dict:
        seen.append((request_id, params))
        return {"outcome": "declined"}

    client.on_request("client/elicit", _handler, pass_request_id=True)
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.ask(77, "client/elicit", {"message": "?"})

    answer = await fake_wm.answer_for(77)
    assert seen == [(77, {"message": "?"})]
    assert answer["result"] == {"outcome": "declined"}


async def test_ending_without_an_answer_writes_no_response(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    ended = asyncio.Event()

    async def _handler(_request_id: int | str, _params: dict | None) -> dict:
        ended.set()
        raise wm_client.RequestEndedWithoutAnswer

    client.on_request("client/elicit", _handler, pass_request_id=True)
    client.on_request("client/other", _echo)
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.ask(1, "client/elicit", {})
    await asyncio.wait_for(ended.wait(), timeout=2.0)
    fake_wm.ask(2, "client/other", {})
    await fake_wm.answer_for(2)

    assert [m for m in fake_wm.received if m.get("id") == 1 and "method" not in m] == []


async def _echo(_params: dict | None) -> dict:
    return {"ok": True}


async def test_the_default_callback_signature_is_unchanged(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    client.on_request("client/other", _echo)
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.ask(5, "client/other", {})

    assert (await fake_wm.answer_for(5))["result"] == {"ok": True}


async def test_losing_the_connection_still_cancels_a_held_request(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _handler(_request_id: int | str, _params: dict | None) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    client.on_request("client/elicit", _handler, pass_request_id=True)
    await client.connect("127.0.0.1", fake_wm.port)
    fake_wm.ask(9, "client/elicit", {})
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await fake_wm.stop()

    await asyncio.wait_for(cancelled.wait(), timeout=2.0)
