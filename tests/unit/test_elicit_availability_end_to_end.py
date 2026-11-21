"""The availability probe over a real WM connection (D6.G2, D6.check causes).

The ER→WM hop is stubbed by a direct call to the same callback body
``runner_manager``'s ``handle_elicit_availability`` registers — resolve the
client the run names, then answer from the installed bridge — because standing
up an ER subprocess is an e2e concern. What travels the real TCP connection is
the WM's own ``client/elicit`` request, and the test asserts it never does.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from finecode_extension_runner.impls.user_prompt import UserPrompt

from finecode import wm_client
from finecode.wm_client import question_capability_allowed
from finecode.wm_server import context, wm_server
from finecode.wm_server.runner import elicitation_bridge
from finecode_extension_runner import run_context

_RUN_ID = "run-0001"


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    wm_server._pending_client_requests.clear()
    wm_server._pending_client_request_owners.clear()
    wm_server._client_capabilities.clear()
    wm_server._connected_clients.clear()
    elicitation_bridge.reset_origins()


@pytest.fixture
async def wm_port():
    """A real WM dispatch loop, minus everything an action would need."""
    wm_server._keep_alive = True
    ws_context = context.WorkspaceContext([])
    server = await asyncio.start_server(
        lambda r, w: wm_server._handle_client(r, w, ws_context), "127.0.0.1", 0
    )
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _er_side_prompt() -> UserPrompt:
    """An ``IUserPrompt`` whose back-channel is the WM's own availability body."""

    async def _send_request_to_wm(method: str, params: dict) -> dict:
        installed = elicitation_bridge.handlers()
        assert installed is not None
        origin = elicitation_bridge.originating_client_for_run(params["runId"])
        if method == "finecode/elicitAvailability":
            return {
                "canAnswer": installed.can_elicit(run_writer_key=origin),
            }
        return await installed.elicit(
            message=params["message"],
            options=params["options"],
            default=params["default"],
            timeout_sec=params["timeoutSec"],
            run_writer_key=origin,
        )

    return UserPrompt(_send_request_to_wm)


@contextlib.contextmanager
def _a_run_from(connection: object):
    """The WM half of a dispatch: this connection started run ``_RUN_ID``."""
    with elicitation_bridge.bind_run(
        _RUN_ID, elicitation_bridge.RunDispatchOrigin(connection=connection)
    ):
        yield


async def _connected_client(port: int, *, capable: bool) -> tuple:
    client = wm_client.ApiClient()
    asked: list[dict] = []

    async def _handler(params: dict | None) -> dict:
        asked.append(params or {})
        return {"outcome": "answered", "value": "yes"}

    client.on_request("client/elicit", _handler)
    await client.connect(
        "127.0.0.1",
        port,
        capabilities={"elicitation": {"choice": True}} if capable else None,
    )
    return client, asked


def _writer_of() -> object:
    """The server-side connection object, which is what the origin registry holds."""
    return next(iter(wm_server._connected_clients))


async def test_a_capable_run_passes_the_start_check_without_a_round_trip(
    wm_port: int,
) -> None:
    """D6.G2: bound to a capable client, the probe says "can" — and nothing is
    sent to the client, so a start check never interrupts the person."""
    client, asked = await _connected_client(wm_port, capable=True)
    try:
        await asyncio.sleep(0.05)  # let client/initialize land server-side
        with _a_run_from(_writer_of()), run_context.run(_RUN_ID):
            result = await asyncio.wait_for(
                _er_side_prompt().check_availability(), timeout=1.0
            )
    finally:
        await client.close()

    assert result.can_ask is True
    assert asked == []


async def test_a_client_muted_by_ci_fails_the_start_check(
    wm_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "not declared" cause end-to-end: with ``CI`` set the FineCode
    client declares nothing (the Phase 1 call-site rule), so the probe reads
    "cannot" and the client's handler is never called."""
    monkeypatch.setenv("CI", "true")
    declared = True and question_capability_allowed(client_label="cli")
    assert declared is False

    client = wm_client.ApiClient()
    asked: list[dict] = []

    async def _handler(params: dict | None) -> dict:
        asked.append(params or {})
        return {"outcome": "answered", "value": "yes"}

    client.on_request("client/elicit", _handler)
    await client.connect(
        "127.0.0.1",
        wm_port,
        capabilities={"elicitation": {"choice": True}} if declared else None,
    )
    try:
        await asyncio.sleep(0.05)
        with _a_run_from(_writer_of()), run_context.run(_RUN_ID):
            result = await asyncio.wait_for(
                _er_side_prompt().check_availability(), timeout=1.0
            )
    finally:
        await client.close()

    assert result.can_ask is False
    assert asked == []
