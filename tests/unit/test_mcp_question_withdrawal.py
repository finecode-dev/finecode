"""Withdrawn or expired questions are cancelled in the AI client (issue 983, A29-A30)."""

from __future__ import annotations

import asyncio
import pathlib
import typing

import pytest

from finecode import wm_client
from finecode.mcp_server import core as core_module
from finecode.mcp_server.contracts import (
    Question,
    QuestionOutcome,
    QuestionResult,
    ToolDependencies,
    ToolEffects,
    WmConnectionConfig,
)
from finecode.mcp_server.legacy import LegacyFront
from finecode_jsonrpc import JsonRpcServerSession


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, typing.Any]] = []

    def on_message(self, cb: object) -> None:
        pass

    def on_message_timed(self, cb: object) -> None:
        pass

    def on_exit(self, cb: object) -> None:
        pass

    def attach_hop_observer(self, observer: object) -> None:
        pass

    def send(self, message: dict[str, typing.Any], **_kwargs: object) -> None:
        self.sent.append(message)


class _RecordingClient:
    def __init__(self) -> None:
        self.request_handlers: dict[str, typing.Any] = {}
        self.notification_handlers: dict[str, typing.Any] = {}
        self.connect_caps: dict[str, typing.Any] | None = None

    def on_request(self, name: str, handler: typing.Any, **_kwargs: object) -> None:
        self.request_handlers[name] = handler

    def on_notification(self, name: str, handler: typing.Any) -> None:
        self.notification_handlers[name] = handler

    def configure_reconnect(self, *args: object, **kwargs: object) -> None:
        self.reconnect_kwargs = kwargs

    async def connect(self, *args: object, **kwargs: object) -> None:
        self.connect_caps = typing.cast(dict, kwargs.get("capabilities"))

    async def add_dir(self, *args: object, **_kwargs: object) -> dict:
        return {}


def _service_with_blocking_front(
    block: asyncio.Event,
) -> tuple[core_module.ToolService, _RecordingClient, list[Question]]:
    client = _RecordingClient()
    seen: list[Question] = []

    async def _question(question: Question) -> QuestionResult:
        seen.append(question)
        await block.wait()
        return QuestionResult(QuestionOutcome.ANSWERED, "yes")

    async def _noop(*args: object, **_kwargs: object) -> None:
        return None

    service = core_module.ToolService(
        workdir=pathlib.Path("/probe"),
        port=1,
        client=typing.cast(typing.Any, client),
        dependencies=ToolDependencies(
            connection_config=lambda: WmConnectionConfig(
                client_id="m", can_answer_choices=True, can_answer_text=True
            ),
            current_client_label=lambda: "m",
        ),
        effects=ToolEffects(
            partial_result=_noop,
            progress=_noop,
            question=_question,
            catalog_changed=_noop,
        ),
    )
    return service, client, seen


async def test_withdrawal_ends_held_question_without_wm_reply() -> None:
    block = asyncio.Event()
    service, client, _ = _service_with_blocking_front(block)
    await service.ensure_connected()
    elicit = client.request_handlers["client/elicit"]
    asking = asyncio.create_task(elicit(7, {"message": "m?", "options": ["yes", "no"]}))
    await asyncio.sleep(0.05)
    assert 7 in service._held_questions
    await client.notification_handlers["client/elicitWithdrawn"](
        {"id": 7, "reason": "expired"}
    )
    with pytest.raises(wm_client.RequestEndedWithoutAnswer):
        await asking
    assert 7 not in service._held_questions


async def test_unknown_withdrawal_id_changes_nothing() -> None:
    block = asyncio.Event()
    service, client, _ = _service_with_blocking_front(block)
    await service.ensure_connected()
    await client.notification_handlers["client/elicitWithdrawn"](
        {"id": 999, "reason": "expired"}
    )
    assert service._held_questions == {}
    assert service._ending_questions == []
    block.set()


async def test_session_loss_ends_everything() -> None:
    block = asyncio.Event()
    service, client, _ = _service_with_blocking_front(block)
    await service.ensure_connected()
    elicit = client.request_handlers["client/elicit"]
    first = asyncio.create_task(elicit(1, {"message": "a?", "options": ["x"]}))
    second = asyncio.create_task(elicit(2, {"kind": "text", "message": "b?"}))
    await asyncio.sleep(0.05)
    assert set(service._held_questions) == {1, 2}
    on_lost = typing.cast(dict, client.reconnect_kwargs)["on_session_lost"]
    on_lost(True)
    for task in (first, second):
        with pytest.raises(wm_client.RequestEndedWithoutAnswer):
            await task
    block.set()


async def test_next_question_waits_for_ended_cancellation() -> None:
    order: list[str] = []
    release = asyncio.Event()

    async def _question(question: Question) -> QuestionResult:
        order.append(f"start-{question.message}")
        try:
            await release.wait()
        except asyncio.CancelledError:
            order.append(f"cancel-{question.message}")
            await asyncio.sleep(0.2)
            raise
        order.append(f"end-{question.message}")
        return QuestionResult(QuestionOutcome.ANSWERED, "yes")

    async def _noop(*args: object, **_kwargs: object) -> None:
        return None

    client = _RecordingClient()
    service = core_module.ToolService(
        workdir=pathlib.Path("/probe"),
        port=1,
        client=typing.cast(typing.Any, client),
        dependencies=ToolDependencies(
            connection_config=lambda: WmConnectionConfig(
                client_id="m", can_answer_choices=True, can_answer_text=True
            ),
            current_client_label=lambda: "m",
        ),
        effects=ToolEffects(
            partial_result=_noop,
            progress=_noop,
            question=_question,
            catalog_changed=_noop,
        ),
    )
    await service.ensure_connected()
    elicit = client.request_handlers["client/elicit"]
    first = asyncio.create_task(elicit(1, {"message": "first?", "options": ["a"]}))
    await asyncio.sleep(0.05)
    await client.notification_handlers["client/elicitWithdrawn"](
        {"id": 1, "reason": "expired"}
    )
    second = asyncio.create_task(elicit(2, {"message": "second?", "options": ["a"]}))
    await asyncio.sleep(0.05)
    # The second question must not start until the first finished cancelling.
    assert order[0] == "start-first?"
    assert "start-second?" not in order
    release.set()
    with pytest.raises(wm_client.RequestEndedWithoutAnswer):
        await first
    reply = await second
    assert reply == {"outcome": "answered", "value": "yes"}
    assert order.index("cancel-first?") < order.index("start-second?")


async def test_front_cancellation_writes_one_notice() -> None:
    session = JsonRpcServerSession()
    transport = _FakeTransport()
    session.attach(transport)
    front = LegacyFront(typing.cast(typing.Any, session))
    await front.handle_initialize(
        {"capabilities": {"elicitation": True}, "clientInfo": {"name": "e"}}
    )
    asking = asyncio.create_task(
        front.ask_question(Question(message="m?", options=("a",), timeout_seconds=30.0))
    )
    await asyncio.sleep(0.05)
    request_id = session._next_id - 1
    asking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asking
    notices = [
        m for m in transport.sent if m.get("method") == "notifications/cancelled"
    ]
    assert len(notices) == 1
    assert notices[0]["params"]["requestId"] == request_id
    assert notices[0]["params"]["reason"] == (
        "FineCode no longer needs an answer to this question."
    )


async def test_front_timeout_writes_notice_and_returns_unavailable() -> None:
    session = JsonRpcServerSession()
    transport = _FakeTransport()
    session.attach(transport)
    front = LegacyFront(typing.cast(typing.Any, session))
    await front.handle_initialize(
        {"capabilities": {"elicitation": True}, "clientInfo": {"name": "e"}}
    )
    result = await front.ask_question(
        Question(message="m?", options=("a",), timeout_seconds=0.01)
    )
    assert result.outcome is QuestionOutcome.UNAVAILABLE
    notices = [
        m for m in transport.sent if m.get("method") == "notifications/cancelled"
    ]
    assert len(notices) == 1


async def test_late_accept_after_cancellation_is_discarded() -> None:
    session = JsonRpcServerSession()
    transport = _FakeTransport()
    session.attach(transport)
    front = LegacyFront(typing.cast(typing.Any, session))
    await front.handle_initialize(
        {"capabilities": {"elicitation": True}, "clientInfo": {"name": "e"}}
    )
    asking = asyncio.create_task(
        front.ask_question(Question(message="m?", options=("a",), timeout_seconds=30.0))
    )
    await asyncio.sleep(0.05)
    request_id = session._next_id - 1
    asking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asking
    # A late accept for that id is dropped quietly at debug level.
    await session._dispatch(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"action": "accept", "content": {}},
        },
        None,
    )
    notices = [
        m for m in transport.sent if m.get("method") == "notifications/cancelled"
    ]
    assert len(notices) == 1
