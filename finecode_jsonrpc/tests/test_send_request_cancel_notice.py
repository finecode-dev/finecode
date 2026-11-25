"""CancelNotice writes a configured frame once, on cancellation or opted-in timeout."""

from __future__ import annotations

import asyncio
import typing

import pytest
from loguru import logger

from finecode_jsonrpc import CancelNotice, JsonRpcServerSession
from finecode_jsonrpc.tracing import HopIdentity, HopToken, StageTime


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


def _session() -> tuple[JsonRpcServerSession, _FakeTransport]:
    session = JsonRpcServerSession()
    transport = _FakeTransport()
    session.attach(transport)
    return session, transport


def _notice() -> CancelNotice:
    return CancelNotice(
        method="notifications/cancelled",
        params=lambda request_id: {"requestId": request_id, "reason": "gone"},
        on_timeout=True,
    )


async def test_cancel_writes_notice_once() -> None:
    session, transport = _session()
    task = asyncio.ensure_future(
        session.send_request("m/m", {}, cancel_notice=_notice())
    )
    await asyncio.sleep(0)
    request_id = next(iter(session._request_futures))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.sent[-1] == {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": request_id, "reason": "gone"},
    }


async def test_timeout_with_on_timeout_writes_notice() -> None:
    session, transport = _session()
    with pytest.raises(TimeoutError):
        await session.send_request("m/m", {}, timeout=0.01, cancel_notice=_notice())
    assert any(m.get("method") == "notifications/cancelled" for m in transport.sent)


async def test_timeout_without_on_timeout_writes_nothing() -> None:
    session, transport = _session()
    notice = CancelNotice(
        method="notifications/cancelled", params=lambda i: {"requestId": i}
    )
    with pytest.raises(TimeoutError):
        await session.send_request("m/m", {}, timeout=0.01, cancel_notice=notice)
    assert all(m.get("method") != "notifications/cancelled" for m in transport.sent)


async def test_late_response_dropped_at_debug() -> None:
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        session, _ = _session()
        task = asyncio.ensure_future(
            session.send_request("m/m", {}, cancel_notice=_notice())
        )
        await asyncio.sleep(0)
        request_id = next(iter(session._request_futures))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session._dispatch(
            {"jsonrpc": "2.0", "id": request_id, "result": None}, None
        )
    finally:
        logger.remove(handler_id)
    assert session._cancelled_request_ids == set()
    assert not any("ERROR" in s for s in seen)


async def test_both_options_raise() -> None:
    session, _ = _session()
    with pytest.raises(ValueError):
        await session.send_request(
            "m/m", {}, cancel_on_abandon=True, cancel_notice=_notice()
        )
