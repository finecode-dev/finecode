"""Strict writer-drain handoff for outgoing notifications.

Observable contract: ``notify_and_drain`` returns only after the preceding
bytes have been written and drained by the IO sender. A gated writer must
block completion, and later items must not be able to extend the fence.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib

import pytest

from finecode_jsonrpc import client as jc


def _make_client() -> jc.JsonRpcClient:
    return jc.JsonRpcClient(
        message_types={"m/n": (dict, dict, None, None)},
        readable_id="test-client",
    )


class _FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.drain_gate: asyncio.Event | None = None
        self.drain_calls = 0
        self.fail_on_write: Exception | None = None
        self.fail_on_drain: Exception | None = None
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.fail_on_write is not None:
            raise self.fail_on_write
        self.written.append(data)

    async def drain(self) -> None:
        self.drain_calls += 1
        if self.fail_on_drain is not None:
            raise self.fail_on_drain
        if self.drain_gate is not None:
            await self.drain_gate.wait()

    def close(self) -> None:
        self.closed = True


async def test_gated_drain_blocks_until_writer_drains() -> None:
    """Completion waits while the writer gate is closed, then fires on drain."""
    client = _make_client()
    writer = _FakeWriter()
    writer.drain_gate = asyncio.Event()
    sender = asyncio.ensure_future(client._send_outgoing_loop(writer))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    waiter = asyncio.ensure_future(client.notify_and_drain("m/n", {"v": 1}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not waiter.done()

    writer.drain_gate.set()
    await asyncio.wait_for(waiter, timeout=5)
    assert len(writer.written) == 1
    _ = sender.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await sender


async def test_following_bytes_cannot_delay_marker() -> None:
    """The marker resolves even when a later payload is still queued behind it."""
    client = _make_client()
    writer = _FakeWriter()
    sender = asyncio.ensure_future(client._send_outgoing_loop(writer))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    first = asyncio.ensure_future(client.notify_and_drain("m/n", {"v": 1}))
    await asyncio.wait_for(first, timeout=5)
    # A later fire-and-forget notify must not disturb the finished fence.
    client.notify("m/n", {"v": 2})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert first.done() and first.exception() is None
    assert len(writer.written) == 2
    _ = sender.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await sender


async def test_write_failure_fails_marker() -> None:
    """A writer write error fails the pending drain instead of hanging it."""
    client = _make_client()
    writer = _FakeWriter()
    writer.fail_on_write = OSError("write down")
    sender = asyncio.ensure_future(client._send_outgoing_loop(writer))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    with pytest.raises(OSError):
        await asyncio.wait_for(client.notify_and_drain("m/n"), timeout=5)
    _ = sender.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await sender


async def test_drain_failure_fails_marker() -> None:
    """A writer drain error fails the pending drain instead of hanging it."""
    client = _make_client()
    writer = _FakeWriter()
    writer.fail_on_drain = OSError("drain down")
    sender = asyncio.ensure_future(client._send_outgoing_loop(writer))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    with pytest.raises(OSError):
        await asyncio.wait_for(client.notify_and_drain("m/n"), timeout=5)
    _ = sender.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await sender


async def test_caller_abort_abandons_marker() -> None:
    """Cancelling the waiter removes it; a late IO completion is a safe no-op."""
    client = _make_client()
    writer = _FakeWriter()
    writer.drain_gate = asyncio.Event()
    sender = asyncio.ensure_future(client._send_outgoing_loop(writer))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    waiter = asyncio.ensure_future(client.notify_and_drain("m/n"))
    await asyncio.sleep(0)
    _ = waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    # Late completion must not raise out of the sender.
    writer.drain_gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    _ = sender.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await sender


async def test_queue_closure_fails_pending_marker() -> None:
    """Closing the outgoing queue fails a still-queued drain marker."""
    client = _make_client()
    drain: concurrent.futures.Future[None] = concurrent.futures.Future()
    marker = jc.OutputDrain(completion=drain)
    with client._pending_drains_lock:
        client._pending_drains.add(drain)
    client._fail_all_drains(jc.ServerStoppedError("sender closed"))
    assert drain.done()
    with pytest.raises(jc.ServerStoppedError):
        drain.result()
    assert marker.completion.done()
