"""An abandoned question is cancelled on the WM; no other request is.

REQUIREMENT: when a handler stops waiting for ``ask_choice`` the WM is told, so
the question leaves the user's screen. No other request the runner makes to the
WM carries a cancel: the WM keeps serving those, and a stray cancel would turn a
caller's timeout or teardown into lost work.
"""

from __future__ import annotations

import asyncio
import typing

import pytest

from finecode_extension_runner import er_server
from finecode_extension_runner.impls.user_prompt import UserPrompt


class _RecordingTransport:
    """Records frames; session wiring callbacks are accepted and ignored."""

    def __init__(self) -> None:
        self.sent: list[dict[str, typing.Any]] = []

    def on_message(self, _cb: object) -> None:
        pass

    def on_message_timed(self, _cb: object) -> None:
        pass

    def on_exit(self, _cb: object) -> None:
        pass

    def attach_hop_observer(self, _observer: object) -> None:
        pass

    def send(self, message: dict[str, typing.Any], **_kwargs: object) -> None:
        self.sent.append(message)


def _server() -> tuple[er_server.ErServer, _RecordingTransport]:
    server = er_server.ErServer()
    transport = _RecordingTransport()
    server._session.attach(transport)
    return server, transport


def _cancel_frames(transport: _RecordingTransport) -> list[dict[str, typing.Any]]:
    return [m for m in transport.sent if m.get("method") == "$/cancelRequest"]


async def _cancel_and_wait(task: asyncio.Task[typing.Any]) -> None:
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelled_question_writes_one_cancel_for_its_request() -> None:
    server, transport = _server()
    prompt = UserPrompt(er_server.make_wm_request_sender(server))

    task = asyncio.ensure_future(prompt.ask_choice("keep it?", ["keep", "revert"]))
    await asyncio.sleep(0)
    (question,) = [m for m in transport.sent if m.get("method") == "finecode/elicit"]
    await _cancel_and_wait(task)

    assert _cancel_frames(transport) == [
        {
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": question["id"]},
        }
    ]


@pytest.mark.parametrize(
    "method", ["finecode/runActionInProject", "finecode/elicitAvailability"]
)
async def test_cancelling_any_other_request_writes_no_cancel(method: str) -> None:
    server, transport = _server()
    send = er_server.make_wm_request_sender(server)

    task = asyncio.ensure_future(send(method, {}))
    await _cancel_and_wait(task)

    assert [m["method"] for m in transport.sent] == [method]
