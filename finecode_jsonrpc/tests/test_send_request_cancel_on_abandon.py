"""A request the caller stops waiting for can ask its peer to stop working on it.

REQUIREMENT: with ``cancel_on_abandon`` a cancelled ``send_request`` writes a
``$/cancelRequest`` frame before it finishes, so the peer does not keep serving
an answer nobody reads. Without the opt-in, nothing changes for existing
callers, and a timeout is never an abandonment. The late reply to an abandoned
request must not raise alarms: it is expected.
"""

from __future__ import annotations

import asyncio
import typing

import pytest
from loguru import logger

from finecode_jsonrpc import JsonRpcServerSession
from finecode_jsonrpc.tracing import HopIdentity, HopToken, StageTime


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


class _UnmatchedCounter:
    """Hop observer double counting ``response_without_waiter`` calls."""

    def __init__(self) -> None:
        self.unmatched = 0

    def __getattr__(self, name: str) -> typing.Callable[..., object]:
        def _noop(*_args: object, **_kwargs: object) -> None:
            return None

        return _noop

    def open_caller(
        self, identity: HopIdentity, peer: str, at: StageTime
    ) -> HopToken | None:
        return typing.cast(HopToken, ("hop", identity.request_id))

    def caller_traceparent(self, token: HopToken) -> str | None:
        return None

    def response_without_waiter(self, identity: HopIdentity, at: StageTime) -> None:
        self.unmatched += 1


def _session(
    observer: _UnmatchedCounter | None = None,
) -> tuple[JsonRpcServerSession, _FakeTransport]:
    session = JsonRpcServerSession(
        hop_observer=typing.cast(typing.Any, observer) if observer else None
    )
    transport = _FakeTransport()
    session.attach(transport)
    return session, transport


def _cancel_frames(transport: _FakeTransport) -> list[dict[str, typing.Any]]:
    return [m for m in transport.sent if m.get("method") == "$/cancelRequest"]


@pytest.fixture
def warnings() -> typing.Iterator[list[str]]:
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    yield seen
    logger.remove(handler_id)


async def _start(
    session: JsonRpcServerSession, **kwargs: typing.Any
) -> tuple[asyncio.Task[typing.Any], int]:
    task = asyncio.ensure_future(session.send_request("test/method", {}, **kwargs))
    await asyncio.sleep(0)
    return task, next(iter(session._request_futures))


async def test_cancel_writes_one_frame_before_cancellation_finishes() -> None:
    session, transport = _session()
    task, request_id = await _start(session, cancel_on_abandon=True)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _cancel_frames(transport) == [
        {
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": request_id},
        }
    ]


async def test_cancel_without_opt_in_writes_no_frame() -> None:
    session, transport = _session()
    task, _ = await _start(session)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _cancel_frames(transport) == []


async def test_timeout_writes_no_frame_even_with_opt_in() -> None:
    session, transport = _session()
    task = asyncio.ensure_future(
        session.send_request("test/method", {}, timeout=0.01, cancel_on_abandon=True)
    )

    with pytest.raises(TimeoutError):
        await task

    assert _cancel_frames(transport) == []


async def test_late_reply_to_abandoned_request_is_quiet(warnings: list[str]) -> None:
    observer = _UnmatchedCounter()
    session, _ = _session(observer)
    task, request_id = await _start(session, cancel_on_abandon=True)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await session._dispatch(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32800, "message": "x"}},
        None,
    )

    assert warnings == []
    assert observer.unmatched == 0
    assert session._cancelled_request_ids == set()


async def test_late_reply_without_opt_in_keeps_todays_path(
    warnings: list[str],
) -> None:
    session, _ = _session()
    task, request_id = await _start(session)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cancelled future is still registered: the reply is dropped as a
    # cancelled future, and no frame or state is left behind.
    assert request_id in session._request_futures
    await session._dispatch({"jsonrpc": "2.0", "id": request_id, "result": None}, None)

    assert warnings == []
    assert session._request_futures == {}


async def test_unknown_reply_without_opt_in_still_warns(warnings: list[str]) -> None:
    session, _ = _session()

    await session._dispatch({"jsonrpc": "2.0", "id": 99, "result": None}, None)

    assert any("No pending future" in w for w in warnings)
