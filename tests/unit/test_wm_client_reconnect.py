"""A WM client survives losing its server; individual requests do not.

Without reconnection a dropped connection is permanent: the client is inert until
its own process restarts, which for an editor means restarting the integration and
for an assistant means losing the session that motivated the work.

These drive a real ``ApiClient`` against a throwaway TCP server, so the reader
loop, the backoff and the re-attach hook are the real ones. What a reconnected
session must contain per surface is a surface concern; PRD-0008-AC9 covers that
end to end in ``tests/e2e/``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from finecode import wm_client


class _FakeWm:
    """A minimal WM: answers ``client/initialize``, and can drop the connection."""

    def __init__(self) -> None:
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                header = await reader.readline()
                if not header:
                    return
                length = int(header.decode().split(":")[1].strip())
                await reader.readline()
                body = json.loads((await reader.readexactly(length)).decode())
                if "id" not in body:
                    continue
                response = json.dumps(
                    {"jsonrpc": "2.0", "id": body["id"], "result": {}}
                ).encode()
                writer.write(
                    f"Content-Length: {len(response)}\r\n\r\n".encode() + response
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            return

    def drop_connections(self) -> None:
        for writer in self._writers:
            writer.close()
        self._writers.clear()

    async def stop(self) -> None:
        self.drop_connections()
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
def discoverable(monkeypatch: pytest.MonkeyPatch):
    """Point the client's address discovery at a port under the test's control."""

    def _install(port: int | None) -> None:
        monkeypatch.setattr(
            wm_client.wm_lifecycle, "running_port", lambda: port, raising=True
        )

    return _install


async def test_reconnects_and_reattaches_after_the_connection_drops(
    fake_wm, discoverable
) -> None:
    """A client whose connection dropped re-establishes it and re-runs its
    session setup before considering itself connected."""
    discoverable(fake_wm.port)
    client = wm_client.ApiClient()
    attaches: list[bool] = []

    async def _attach(*, first_connect: bool) -> None:
        attaches.append(first_connect)

    client.configure_reconnect(
        wm_client.ReconnectPolicy(base_delay=0.01, jitter=0.0), on_reattach=_attach
    )
    await client.connect("127.0.0.1", fake_wm.port)
    assert attaches == [True]

    epoch = client.connection_epoch
    fake_wm.drop_connections()
    await client.wait_connected(timeout=2.0, after_epoch=epoch)

    assert attaches == [True, False]
    assert fake_wm.connections == 2
    await client.close()


async def test_address_is_rediscovered_on_every_attempt(fake_wm, discoverable) -> None:
    """The client re-reads the discovery file rather than reusing the port it
    last held: a restarted WM listens on a new one (ADR-0002), and reusing the
    old address reconnects to nothing."""
    replacement = _FakeWm()
    await replacement.start()
    try:
        discoverable(fake_wm.port)
        client = wm_client.ApiClient()
        client.configure_reconnect(
            wm_client.ReconnectPolicy(base_delay=0.01, jitter=0.0)
        )
        await client.connect("127.0.0.1", fake_wm.port)

        # The "restart": the old server is gone and a new one holds a new port.
        epoch = client.connection_epoch
        discoverable(replacement.port)
        await fake_wm.stop()
        await client.wait_connected(timeout=2.0, after_epoch=epoch)

        assert replacement.connections == 1
        await client.close()
    finally:
        await replacement.stop()


async def test_in_flight_requests_fail_and_are_not_retried(
    fake_wm, discoverable
) -> None:
    """A request outstanding when the connection dropped fails.

    FineCode actions are not idempotent — a run may have formatted files or
    published an artifact before the drop, and the client cannot tell how far it
    got. Re-issuing it silently is the one outcome worse than reporting the loss.
    """
    discoverable(fake_wm.port)
    client = wm_client.ApiClient()
    client.configure_reconnect(wm_client.ReconnectPolicy(base_delay=0.01, jitter=0.0))
    await client.connect("127.0.0.1", fake_wm.port)

    pending = asyncio.create_task(client.request("actions/run", {"action": "publish"}))
    await asyncio.sleep(0)
    epoch = client.connection_epoch
    fake_wm.drop_connections()

    with pytest.raises(ConnectionError):
        await pending

    await client.wait_connected(timeout=2.0, after_epoch=epoch)
    # The reconnect delivered a session, not a re-issued request.
    assert fake_wm.connections == 2
    await client.close()


async def test_a_request_made_while_reconnecting_fails_rather_than_hangs(
    fake_wm, discoverable
) -> None:
    """A call issued in the gap between the drop and the reconnect reports the
    disconnection.

    The window is not hypothetical: replacing the WM drops every other client's
    connection on purpose, and an editor keeps sending hovers and formats
    through it. A client that still looked connected would write the request
    into a closed transport and then wait on a response no reader is left to
    deliver — an editor request that never returns, with no timeout anywhere.
    """
    discoverable(fake_wm.port)
    client = wm_client.ApiClient()
    # Slow enough that the request below lands inside the reconnect window.
    client.configure_reconnect(wm_client.ReconnectPolicy(base_delay=5.0, jitter=0.0))
    await client.connect("127.0.0.1", fake_wm.port)

    fake_wm.drop_connections()
    while client.is_connected:
        await asyncio.sleep(0.01)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(client.request("workspace/listProjects"), timeout=1.0)

    await client.close()


async def test_the_session_lost_hook_reports_the_gap_and_the_giving_up(
    discoverable,
) -> None:
    """The surface hears the session end, and hears whether it is coming back.

    A surface that gates its work on the session needs both edges: the first to
    stop dispatching into a WM that has never heard of it, the second to stop
    waiting on a re-attach that will never happen — which would turn every
    request it is holding into one that hangs forever.
    """
    server = _FakeWm()
    await server.start()
    client = wm_client.ApiClient()
    events: list[bool] = []
    client.configure_reconnect(
        wm_client.ReconnectPolicy(
            base_delay=0.01, jitter=0.0, max_delay=0.02, max_attempts=2
        ),
        on_session_lost=events.append,
    )
    await client.connect("127.0.0.1", server.port)
    assert events == []

    # Nothing to reconnect to, so the attempts run out.
    discoverable(None)
    server.drop_connections()
    await asyncio.sleep(0.3)

    assert events == [True, False]

    # A deliberate shutdown is not a lost session.
    await client.close()
    assert events == [True, False]
    await server.stop()


async def test_close_does_not_trigger_a_reconnect(fake_wm, discoverable) -> None:
    """Shutting a client down is not a lost connection.

    ``close()`` cancels the reader loop, which is the same thing a dropped
    connection does; a client that could not tell them apart would race its own
    shutdown and leave a connection nobody asked for.
    """
    discoverable(fake_wm.port)
    client = wm_client.ApiClient()
    client.configure_reconnect(wm_client.ReconnectPolicy(base_delay=0.01, jitter=0.0))
    await client.connect("127.0.0.1", fake_wm.port)

    await client.close()
    await asyncio.sleep(0.1)

    assert fake_wm.connections == 1
    assert client.is_connected is False


async def test_a_client_that_cannot_reattach_is_not_connected(
    fake_wm, discoverable
) -> None:
    """Restoring the socket is not the success condition (ADR-0074 rule 3).

    A client whose session was not re-established is talking to a server that has
    never heard of it, and the next request behaves differently for reasons the
    caller cannot see.
    """
    discoverable(fake_wm.port)
    client = wm_client.ApiClient()

    async def _attach(*, first_connect: bool) -> None:
        if not first_connect:
            raise RuntimeError("workspace could not be re-added")

    client.configure_reconnect(
        wm_client.ReconnectPolicy(
            base_delay=0.01, jitter=0.0, max_attempts=2, max_delay=0.01
        ),
        on_reattach=_attach,
    )
    await client.connect("127.0.0.1", fake_wm.port)

    epoch = client.connection_epoch
    fake_wm.drop_connections()
    with pytest.raises(asyncio.TimeoutError):
        await client.wait_connected(timeout=0.5, after_epoch=epoch)

    assert client.is_connected is False
    await client.close()


async def test_backoff_is_bounded_and_gives_up(discoverable) -> None:
    """A client that cannot reconnect reports that state rather than retrying
    forever or sitting silently disconnected.

    The schedule is sized against the WM's own disconnect timeout: past it there
    is no server left to reach, so an unbounded loop would only delay the report.
    """
    discoverable(None)
    server = _FakeWm()
    await server.start()
    client = wm_client.ApiClient()
    client.configure_reconnect(
        wm_client.ReconnectPolicy(
            base_delay=0.01, jitter=0.0, max_delay=0.02, max_attempts=3
        )
    )
    await client.connect("127.0.0.1", server.port)

    server.drop_connections()
    await asyncio.sleep(0.3)

    assert client.is_connected is False
    assert client._reconnect_task is not None and client._reconnect_task.done()
    await client.close()
    await server.stop()


def test_default_schedule_fits_inside_the_disconnect_timeout() -> None:
    """The default backoff spends its attempts while a server can still be there.

    The WM exits when no client reconnects within its disconnect timeout, so a
    schedule that outlives that window spends its last attempts on a server that
    has already gone.
    """
    policy = wm_client.ReconnectPolicy()
    total, delay = 0.0, policy.base_delay
    for _ in range(policy.max_attempts):
        total += delay * (1 + policy.jitter)
        delay = min(delay * 2, policy.max_delay)

    from finecode.wm_server import wm_lifecycle

    assert total < wm_lifecycle.NO_CLIENT_TIMEOUT_SECONDS
