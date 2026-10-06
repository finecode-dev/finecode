"""Requirement tests: an event a coroutine can await without pinning a thread.

REQUIREMENT (ADR-0097): a runner client waits for its server process to stop,
and some server processes never stop. Waiting through ``asyncio.to_thread`` on a
``threading.Event`` pins a default-executor worker for the client's whole life;
enough such clients exhaust the pool and any other ``to_thread`` work on the
same loop queues behind them forever. ``LoopAwareEvent.wait_async`` must wake
without holding a thread, must not lose a wakeup raised concurrently from
another thread, and must degrade cleanly when the waiter's loop is gone.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from finecode_jsonrpc import client as jc
from finecode_jsonrpc._loop_event import LoopAwareEvent


async def test_set_from_another_thread_wakes_waiter() -> None:
    """A ``set()`` from the IO thread must reach a ``wait_async`` on the client's
    loop; otherwise the client never learns its server stopped."""
    event = LoopAwareEvent()
    task = asyncio.create_task(event.wait_async())
    await asyncio.sleep(0)  # let the waiter register

    threading.Timer(0.01, event.set).start()

    await asyncio.wait_for(task, timeout=1.0)


async def test_wait_async_on_set_event_returns_without_suspending() -> None:
    """An already-set event must resolve without yielding to the loop, so a
    caller that waits on a stopped process does not pay an extra scheduling
    round-trip."""
    event = LoopAwareEvent()
    event.set()

    suspended = False

    def _mark_suspended() -> None:
        nonlocal suspended
        suspended = True

    asyncio.get_running_loop().call_soon(_mark_suspended)
    await event.wait_async()

    assert suspended is False
    assert event._waiters == []


async def test_cancelled_waiter_is_deregistered() -> None:
    """A cancelled waiter must leave no entry behind — a stale one would be
    resolved into a dead future on the next ``set()``."""
    event = LoopAwareEvent()
    task = asyncio.create_task(event.wait_async())
    await asyncio.sleep(0)
    assert len(event._waiters) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert event._waiters == []


def test_set_after_waiter_loop_closed_does_not_raise() -> None:
    """A waiter whose loop closed before the event was set must not turn the
    ``set()`` into an exception for whoever is stopping the process."""
    event = LoopAwareEvent()
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        event._waiters.append((loop, fut))
    finally:
        loop.close()

    event.set()  # must not raise

    assert event._waiters == []


async def test_concurrent_waiters_hold_no_thread() -> None:
    """The regression this class exists for: any number of waiters must add no
    OS threads. Thread-per-wait is what exhausted the default executor and kept
    every long-lived WM alive after shutdown."""
    event = LoopAwareEvent()
    before = threading.active_count()

    tasks = [asyncio.create_task(event.wait_async()) for _ in range(50)]
    await asyncio.sleep(0)

    assert threading.active_count() == before

    event.set()
    await asyncio.gather(*tasks)


def test_sync_wait_api_is_unchanged() -> None:
    """The class is still a ``threading.Event``: existing synchronous callers
    keep working. A timed-out wait must report ``False``, not raise."""
    event = LoopAwareEvent()
    assert event.wait(timeout=0.01) is False
    event.set()
    assert event.wait(timeout=0.01) is True


async def test_stop_handler_wait_holds_no_thread() -> None:
    """The client's process-stop handler must live on the loop — one task, no
    worker thread — and must finish as soon as the event is set."""
    client = jc.JsonRpcClient(message_types={}, readable_id="test-client")
    before = threading.active_count()

    task = asyncio.create_task(client._server_process_stop_handler())
    await asyncio.sleep(0)

    assert threading.active_count() == before
    assert not task.done()

    client.server_process_stopped.set()
    await asyncio.wait_for(task, timeout=1.0)
