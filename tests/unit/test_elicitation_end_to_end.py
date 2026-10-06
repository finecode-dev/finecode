"""A question travels from a handler's service to a client and back.

Everything between ``IUserPrompt.ask_choice`` and the client's answer is real
here except the ER↔WM socket: the ER-side service, the WM-side addressing
registry, the ``client/elicit`` request over a real TCP connection, the client's
inbound-request dispatch, and the mapping of what comes back onto an
``ElicitationResult``. The ER↔WM hop is stubbed with a direct call to the same
callback body ``runner_manager`` registers, because standing up an ER subprocess
is an e2e concern; that its method is registered at all is pinned by
``test_er_user_message_registration``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from finecode_extension_api.interfaces.iuserprompt import ElicitationOutcome
from finecode_extension_runner.impls.user_prompt import UserPrompt

from finecode import wm_client
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
    """An ``IUserPrompt`` whose back-channel is the WM's own elicit callback.

    The body mirrors ``runner_manager``'s ``handle_elicit``: resolve the client
    that started the run the ER named, then hand the question to the installed
    bridge.
    """

    async def _send_request_to_wm(_method: str, params: dict) -> dict:
        installed = elicitation_bridge.handlers()
        assert installed is not None
        return await installed.elicit(
            message=params["message"],
            options=params["options"],
            default=params["default"],
            timeout_sec=params["timeoutSec"],
            run_writer_key=elicitation_bridge.originating_client_for_run(
                params["runId"]
            ),
        )

    return UserPrompt(_send_request_to_wm)


@contextlib.contextmanager
def _a_run_from(connection: object):
    """The WM half of a dispatch: this connection started run ``_RUN_ID``."""
    with elicitation_bridge.bind_run(
        _RUN_ID, elicitation_bridge.RunDispatchOrigin(connection=connection)
    ):
        yield


async def _connected_client(port: int, answer: dict, *, capable: bool) -> tuple:
    client = wm_client.ApiClient()
    asked: list[dict] = []

    async def _handler(params: dict | None) -> dict:
        asked.append(params or {})
        return answer

    client.on_request("client/elicit", _handler)
    await client.connect(
        "127.0.0.1",
        port,
        capabilities={"elicitation": {"choice": True}} if capable else None,
    )
    return client, asked


def _writer_of():
    """The server-side connection object, which is what the origin registry holds.

    Stands in for the streaming request handler, which registers the very
    ``writer`` its own dispatch was handed.
    """
    return next(iter(wm_server._connected_clients))


async def test_a_handler_gets_the_answer_a_person_gave(wm_port: int) -> None:
    client, asked = await _connected_client(
        wm_port, {"outcome": "answered", "value": "revert"}, capable=True
    )
    try:
        await asyncio.sleep(0.05)  # let client/initialize land server-side
        with _a_run_from(_writer_of()), run_context.run(_RUN_ID):
            result = await _er_side_prompt().ask_choice(
                "Keep the generated changes?", ["keep", "revert"], default="keep"
            )
    finally:
        await client.close()

    assert result.outcome is ElicitationOutcome.ANSWERED
    assert result.value == "revert"
    assert asked == [
        {
            "message": "Keep the generated changes?",
            "options": ["keep", "revert"],
            "default": "keep",
            "timeoutSec": 300.0,
        }
    ]


async def test_a_person_who_refuses_is_reported_as_declined(wm_port: int) -> None:
    client, _ = await _connected_client(wm_port, {"outcome": "declined"}, capable=True)
    try:
        await asyncio.sleep(0.05)
        with _a_run_from(_writer_of()), run_context.run(_RUN_ID):
            result = await _er_side_prompt().ask_choice("Proceed?", ["yes", "no"])
    finally:
        await client.close()

    assert result.outcome is ElicitationOutcome.DECLINED


async def test_a_client_in_a_pipeline_costs_no_round_trip(wm_port: int) -> None:
    """The CI case: nothing declared, so the ask resolves without being sent."""
    client, asked = await _connected_client(
        wm_port, {"outcome": "answered", "value": "yes"}, capable=False
    )
    try:
        await asyncio.sleep(0.05)
        with _a_run_from(_writer_of()), run_context.run(_RUN_ID):
            result = await asyncio.wait_for(
                _er_side_prompt().ask_choice("Proceed?", ["yes", "no"]), timeout=1.0
            )
    finally:
        await client.close()

    assert result.outcome is ElicitationOutcome.UNAVAILABLE
    assert asked == []


async def test_a_run_that_did_not_stream_is_told_at_once(wm_port: int) -> None:
    """No origin recorded: the non-streaming path, answered without waiting."""
    client, asked = await _connected_client(
        wm_port, {"outcome": "answered", "value": "yes"}, capable=True
    )
    try:
        await asyncio.sleep(0.05)
        result = await asyncio.wait_for(
            _er_side_prompt().ask_choice("Proceed?", ["yes", "no"]), timeout=1.0
        )
    finally:
        await client.close()

    assert result.outcome is ElicitationOutcome.UNAVAILABLE
    assert asked == []


async def test_losing_the_client_mid_question_does_not_wait_it_out(
    wm_port: int,
) -> None:
    """ADR-0082 rule 4: the deadline is 300s; this must not take it."""
    client = wm_client.ApiClient()
    hanging = asyncio.Event()

    async def _never_answers(_params: dict | None) -> dict:
        await hanging.wait()
        return {"outcome": "declined"}

    client.on_request("client/elicit", _never_answers)
    await client.connect(
        "127.0.0.1", wm_port, capabilities={"elicitation": {"choice": True}}
    )
    await asyncio.sleep(0.05)

    with _a_run_from(_writer_of()), run_context.run(_RUN_ID):
        asking = asyncio.create_task(
            _er_side_prompt().ask_choice("Proceed?", ["yes", "no"])
        )
        await asyncio.sleep(0.05)
        await client.close()  # the person's terminal went away

        result = await asyncio.wait_for(asking, timeout=2.0)

    assert result.outcome is ElicitationOutcome.UNAVAILABLE
