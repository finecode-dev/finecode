"""A WM client must tell an inbound request from an inbound response.

Bug class this locks (ADR-0082, and the same shape as the ``er/userMessage``
regression next door): before the WM could send requests, ``ApiClient``'s reader
loop classified every message carrying an ``id`` as a *response*. JSON-RPC 2.0
says ``id`` **and** ``method`` is a request and ``id`` alone is a response, so
the first request the WM ever sent would have been swallowed as "response for
unknown id" — silently, with the server left waiting out its deadline.

These drive a real ``ApiClient`` against a throwaway TCP server, so the framing
and the reader loop are the real ones.
"""

from __future__ import annotations

import asyncio
import json

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
async def fake_wm():
    server = _FakeWm()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def client(fake_wm: _FakeWm):
    api_client = wm_client.ApiClient()
    try:
        yield api_client
    finally:
        await api_client.close()


async def test_client_answers_an_inbound_request(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    async def _handler(params: dict | None) -> dict:
        return {"outcome": "answered", "value": (params or {})["options"][0]}

    client.on_request("client/elicit", _handler)
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.ask(99, "client/elicit", {"message": "keep it?", "options": ["yes", "no"]})

    answer = await fake_wm.answer_for(99)
    assert answer["result"] == {"outcome": "answered", "value": "yes"}
    assert "error" not in answer


async def test_responses_are_still_routed_to_their_pending_request(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    """The response path must not be captured by the new request branch."""

    async def _handler(_params: dict | None) -> dict:
        return {}

    client.on_request("client/elicit", _handler)
    await client.connect("127.0.0.1", fake_wm.port)

    # `client/initialize` already round-tripped inside connect(); do another one
    # explicitly so a broken discrimination shows up as a hang, not a pass.
    result = await asyncio.wait_for(client.request("server/getInfo", {}), timeout=2.0)
    assert result == {}


async def test_unknown_inbound_method_is_answered_with_method_not_found(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    """An unhandled request is refused, not crashed on and not left unanswered."""
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.ask(7, "client/somethingNobodyImplemented")

    answer = await fake_wm.answer_for(7)
    assert answer["error"]["code"] == -32601


async def test_a_handler_that_raises_does_not_take_down_the_reader(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    async def _handler(_params: dict | None) -> dict:
        raise RuntimeError("no terminal here")

    client.on_request("client/elicit", _handler)
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.ask(11, "client/elicit", {"options": ["a"]})
    answer = await fake_wm.answer_for(11)
    assert answer["error"]["code"] == -32603

    # The connection is still usable: a dead reader loop would hang this.
    assert (
        await asyncio.wait_for(client.request("server/getInfo", {}), timeout=2.0) == {}
    )


async def test_a_lost_connection_ends_a_question_still_on_screen(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    """The server resolves its side the moment a client goes away (ADR-0082 rule 4).

    Leaving the handler running would keep a prompt in front of a person whose
    answer can no longer land anywhere: the id it belongs to is gone, and by the
    time they typed it the transport underneath could be a reconnected one.
    """
    started = asyncio.Event()
    ended = asyncio.Event()

    async def _never_answers(_params: dict | None) -> dict:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            ended.set()
            raise
        return {}  # pragma: no cover - the sleep is the point

    client.on_request("client/elicit", _never_answers)
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.ask(5, "client/elicit", {"options": ["a"]})
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await fake_wm.stop()

    await asyncio.wait_for(ended.wait(), timeout=2.0)


async def test_capabilities_are_declared_at_initialize(
    fake_wm: _FakeWm, client: wm_client.ApiClient
) -> None:
    """A client that cannot answer declares nothing, so it is never asked."""
    await client.connect(
        "127.0.0.1", fake_wm.port, capabilities={"elicitation": {"choice": True}}
    )
    initialize = next(
        msg for msg in fake_wm.received if msg.get("method") == "client/initialize"
    )
    assert initialize["params"]["capabilities"] == {"elicitation": {"choice": True}}

    silent = wm_client.ApiClient()
    try:
        await silent.connect("127.0.0.1", fake_wm.port)
    finally:
        await silent.close()
    latest = [
        msg for msg in fake_wm.received if msg.get("method") == "client/initialize"
    ][-1]
    assert "capabilities" not in latest["params"]
