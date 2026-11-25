"""A request sent after the transport closed fails at once.

REQUIREMENT: once the WM connection is gone, a scope release must not park
forever on a future nothing resolves — the failure is prompt so cleanup can
finish within its bound.
"""

from __future__ import annotations

import asyncio
import typing

import pytest

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

    def send(self, message: dict[str, typing.Any], **_kwargs: object) -> None:
        self.sent.append(message)


async def test_send_request_after_exit_raises_promptly() -> None:
    """After disconnect, `send_request` raises instead of hanging."""
    session = JsonRpcServerSession()
    transport = _FakeTransport()
    session.attach(transport)
    await session._handle_exit()

    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(RuntimeError, match="Transport closed"):
        await session.send_request("finecode/releaseProcessBudget", {"leaseId": "L1"})
    assert loop.time() - start < 0.1
    assert transport.sent == []


async def test_attach_clears_closed_state() -> None:
    """Re-attaching a new transport lets requests flow again."""
    session = JsonRpcServerSession()
    session.attach(_FakeTransport())
    await session._handle_exit()
    fresh = _FakeTransport()
    session.attach(fresh)

    task = asyncio.ensure_future(session.send_request("test/method", {}))
    await asyncio.sleep(0)
    assert len(fresh.sent) == 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
