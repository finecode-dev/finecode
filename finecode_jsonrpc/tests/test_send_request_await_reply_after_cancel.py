"""Await-reply-after-cancel send mode on both JSON-RPC peers.

REQUIREMENT: when a caller stops waiting for a request but still needs to know
how the peer ended it (a cancelled delegation must learn whether the child
stopped), the request is sent with ``await_reply_after_cancel``. Cancelling the
awaiting task then writes exactly one ``$/cancelRequest`` and keeps waiting up
to the bound for the reply, raising ``RequestCancelledError`` carrying what the
wait observed. A reply that already resolved writes no cancel frame. After the
bound the late reply is dropped quietly and the id is released.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import typing

import pytest
from loguru import logger

from finecode_jsonrpc import (
    AwaitReplyAfterCancel,
    JsonRpcServerSession,
    ReplyAfterCancelKind,
    RequestCancelledError,
)
from finecode_jsonrpc import client as jc


class _FakeTransport:
    """Records frames; session wiring callbacks are accepted and ignored."""

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


def _server_session() -> tuple[JsonRpcServerSession, _FakeTransport]:
    session = JsonRpcServerSession()
    transport = _FakeTransport()
    session.attach(transport)
    return session, transport


def _cancel_frames(transport: _FakeTransport) -> list[dict[str, typing.Any]]:
    return [m for m in transport.sent if m.get("method") == "$/cancelRequest"]


async def _resolve_server(
    session: JsonRpcServerSession, request_id: int, **payload: typing.Any
) -> None:
    await session._dispatch({"jsonrpc": "2.0", "id": request_id, **payload}, None)


async def test_server_reply_before_cancel_writes_no_frame_and_carries_result() -> None:
    """A reply that resolved before the cancel writes no ``$/cancelRequest``."""
    session, transport = _server_session()
    task = asyncio.ensure_future(
        session.send_request(
            "test/method", {}, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
        )
    )
    await asyncio.sleep(0)
    request_id = next(iter(session._request_futures))
    await _resolve_server(session, request_id, result={"ok": True})
    task.cancel()
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.RESULT
    assert exc_info.value.reply.result == {"ok": True}
    assert _cancel_frames(transport) == []


async def test_server_reply_within_timeout_carries_result() -> None:
    """A reply arriving inside the bound ends the wait with its value."""
    session, transport = _server_session()
    task = asyncio.ensure_future(
        session.send_request(
            "test/method", {}, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
        )
    )
    await asyncio.sleep(0)
    request_id = next(iter(session._request_futures))
    task.cancel()
    await asyncio.sleep(0)
    await _resolve_server(session, request_id, result=42)
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.RESULT
    assert exc_info.value.reply.result == 42
    assert len(_cancel_frames(transport)) == 1
    assert session._request_futures == {}


async def test_server_error_reply_carries_code_and_data() -> None:
    """An error reply inside the bound is reported with its code and data."""
    session, _ = _server_session()
    task = asyncio.ensure_future(
        session.send_request(
            "test/method", {}, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
        )
    )
    await asyncio.sleep(0)
    request_id = next(iter(session._request_futures))
    task.cancel()
    await asyncio.sleep(0)
    await _resolve_server(
        session,
        request_id,
        error={"code": -32800, "message": "Request cancelled", "data": {"s": True}},
    )
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.ERROR
    assert exc_info.value.reply.error_code == -32800
    assert exc_info.value.reply.error_data == {"s": True}


async def test_server_timeout_then_late_reply_is_quiet() -> None:
    """Past the bound the wait ends as TIMEOUT and the late reply is dropped."""
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        session, _ = _server_session()
        task = asyncio.ensure_future(
            session.send_request(
                "test/method", {}, await_reply_after_cancel=AwaitReplyAfterCancel(0.05)
            )
        )
        await asyncio.sleep(0)
        request_id = next(iter(session._request_futures))
        task.cancel()
        with pytest.raises(RequestCancelledError) as exc_info:
            await task
        assert exc_info.value.reply is not None
        assert exc_info.value.reply.kind is ReplyAfterCancelKind.TIMEOUT
        await _resolve_server(session, request_id, result=1)
        assert session._cancelled_request_ids == set()
    finally:
        logger.remove(handler_id)
    assert not any("ERROR" in s for s in seen)


async def test_server_transport_close_reports_disconnected() -> None:
    """A transport that closes mid-wait ends it as DISCONNECTED."""
    session, _ = _server_session()
    task = asyncio.ensure_future(
        session.send_request(
            "test/method", {}, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    await session._handle_exit()
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.DISCONNECTED


async def test_server_repeated_cancellation_neither_shortens_nor_lost() -> None:
    """A second cancel during the wait does not end it early."""
    session, _ = _server_session()
    task = asyncio.ensure_future(
        session.send_request(
            "test/method", {}, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
        )
    )
    await asyncio.sleep(0)
    request_id = next(iter(session._request_futures))
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    await _resolve_server(session, request_id, result="late")
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.RESULT
    assert exc_info.value.reply.result == "late"


async def test_server_options_are_exclusive() -> None:
    """Passing two send modes raises instead of sending a confusing frame."""
    session, _ = _server_session()
    with pytest.raises(ValueError):
        await session.send_request(
            "m/m",
            {},
            cancel_on_abandon=True,
            await_reply_after_cancel=AwaitReplyAfterCancel(1.0),
        )


# ---------------------------------------------------------------------------
# Client side (JsonRpcClient.send_request)
# ---------------------------------------------------------------------------

_METHOD = "test/method"


@dataclasses.dataclass
class _Params:
    pass


@dataclasses.dataclass
class _Request:
    id: int | str
    method: str
    jsonrpc: str
    params: _Params | None


@dataclasses.dataclass
class _Response:
    id: int | str
    jsonrpc: str
    result: typing.Any = None


@dataclasses.dataclass
class _Result:
    pass


def _make_client() -> tuple[jc.JsonRpcClient, list[str]]:
    client = jc.JsonRpcClient(
        message_types={_METHOD: (_Request, _Params, _Response, _Response)},
        readable_id="test-client",
    )
    sent: list[str] = []
    client._send_data = lambda data: sent.append(data)  # type: ignore[method-assign]
    return client, sent


def _cancel_frames_client(sent: list[str]) -> list[dict[str, typing.Any]]:
    return [
        json.loads(s) for s in sent if json.loads(s).get("method") == "$/cancelRequest"
    ]


async def _start_client(
    client: jc.JsonRpcClient, **kwargs: typing.Any
) -> tuple[asyncio.Task[typing.Any], str]:
    task = asyncio.ensure_future(client.send_request(_METHOD, _Params(), **kwargs))
    await asyncio.sleep(0)
    assert len(client._async_request_futures) == 1
    return task, next(iter(client._async_request_futures))


async def _answer_client(
    client: jc.JsonRpcClient, request_id: str, **payload: typing.Any
) -> None:
    await client.handle_message({"jsonrpc": "2.0", "id": request_id, **payload})


async def test_client_reply_within_timeout_carries_result() -> None:
    """The client waits past the cancel and reports the reply it received."""
    client, sent = _make_client()
    task, request_id = await _start_client(
        client, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
    )
    task.cancel()
    await asyncio.sleep(0)
    await _answer_client(client, request_id, result={"v": 1})
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.RESULT
    assert exc_info.value.reply.result.result == {"v": 1}
    assert len(_cancel_frames_client(sent)) == 1


async def test_client_reply_before_cancel_writes_no_frame() -> None:
    """A reply that resolved before the cancel writes no ``$/cancelRequest``."""
    client, sent = _make_client()
    task, request_id = await _start_client(
        client, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
    )
    await _answer_client(client, request_id, result=7)
    task.cancel()
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.RESULT
    assert exc_info.value.reply.result.result == 7
    assert _cancel_frames_client(sent) == []


async def test_client_timeout_then_late_reply_is_debug_only() -> None:
    """Past the bound the client ends TIMEOUT and drops the late reply quietly."""
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        client, _sent = _make_client()
        task, request_id = await _start_client(
            client, await_reply_after_cancel=AwaitReplyAfterCancel(0.05)
        )
        task.cancel()
        with pytest.raises(RequestCancelledError) as exc_info:
            await task
        assert exc_info.value.reply is not None
        assert exc_info.value.reply.kind is ReplyAfterCancelKind.TIMEOUT
        await _answer_client(client, request_id, result=1)
        assert client._cancelled_request_ids == set()
    finally:
        logger.remove(handler_id)
    assert not any("ERROR" in s for s in seen)


async def test_client_error_reply_carries_code_and_data() -> None:
    """An error reply inside the bound is reported with its code and data."""
    client, _sent = _make_client()
    task, request_id = await _start_client(
        client, await_reply_after_cancel=AwaitReplyAfterCancel(5.0)
    )
    task.cancel()
    await asyncio.sleep(0)
    await client.handle_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32800,
                "message": "Request cancelled",
                "data": {"s": False},
            },
        }
    )
    with pytest.raises(RequestCancelledError) as exc_info:
        await task
    assert exc_info.value.reply is not None
    assert exc_info.value.reply.kind is ReplyAfterCancelKind.ERROR
    assert exc_info.value.reply.error_code == -32800
    assert exc_info.value.reply.error_data == {"s": False}


async def test_server_cancel_before_first_step_honoured_with_data() -> None:
    """A cancel arriving before the task's first step still ends the request."""
    from finecode_jsonrpc import REQUEST_CANCELLED

    session, transport = _server_session()
    ran: list[bool] = []

    async def handler(params: dict[str, typing.Any] | None) -> typing.Any:
        ran.append(True)
        return {"x": 1}

    session.on_request(
        "test/m", handler, not_started_cancel_data={"subtreeStopped": True}
    )
    await session._dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "test/m", "params": {}}, None
    )
    await session._builtin_cancel_request({"id": 1})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ran == []
    assert transport.sent != []
    envelope = transport.sent[-1]
    assert envelope["id"] == 1
    assert envelope["error"]["code"] == REQUEST_CANCELLED
    assert envelope["error"]["data"] == {"subtreeStopped": True}


async def test_server_cancel_before_first_step_without_data() -> None:
    """Without not-started data the early cancel answers with no data."""
    from finecode_jsonrpc import REQUEST_CANCELLED

    session, transport = _server_session()
    ran: list[bool] = []

    async def handler(params: dict[str, typing.Any] | None) -> typing.Any:
        ran.append(True)
        return {"x": 1}

    session.on_request("test/m", handler)
    await session._dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "test/m", "params": {}}, None
    )
    await session._builtin_cancel_request({"id": 2})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ran == []
    envelope = transport.sent[-1]
    assert envelope["id"] == 2
    assert envelope["error"]["code"] == REQUEST_CANCELLED
    assert "data" not in envelope["error"]
