"""Requirement tests: a re-armable event a coroutine can await without a thread.

``LoopAwareEvent`` is set-once, but a repeating wait — such as waiting for a
follow-up diagnostics publish after one has already arrived — has to clear the
event and wait again. The re-arm cycle must not lose a wake-up raised
concurrently from the publishing thread, and neither waiting nor clearing may
hold an OS thread.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from finecode_extension_api.contrib._loop_event import RearmableLoopEvent


async def test_set_from_another_thread_wakes_waiter() -> None:
    """A ``set()`` from the IO thread must reach a waiter on the client's loop."""
    event = RearmableLoopEvent()
    task = asyncio.create_task(event.wait_async())
    await asyncio.sleep(0)  # let the waiter register

    threading.Timer(0.01, event.set).start()

    await asyncio.wait_for(task, timeout=1.0)


async def test_wait_async_on_set_event_returns_without_suspending() -> None:
    """An already-set event must resolve without yielding to the loop."""
    event = RearmableLoopEvent()
    event.set()

    suspended = False

    def _mark_suspended() -> None:
        nonlocal suspended
        suspended = True

    _ = asyncio.get_running_loop().call_soon(_mark_suspended)
    await event.wait_async()

    assert not suspended
    assert event._waiters == []


async def test_cancelled_waiter_is_deregistered() -> None:
    """A cancelled waiter must leave no entry behind."""
    event = RearmableLoopEvent()
    task = asyncio.create_task(event.wait_async())
    await asyncio.sleep(0)
    assert len(event._waiters) == 1

    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert event._waiters == []


async def test_clear_wakes_current_waiters_and_accepts_new_ones() -> None:
    """``clear`` drops the waiters it finds; the next ``wait_async`` waits again."""
    event = RearmableLoopEvent()
    first = asyncio.create_task(event.wait_async())
    await asyncio.sleep(0)

    event.clear()
    await asyncio.wait_for(first, timeout=1.0)
    assert not event.is_set()

    second = asyncio.create_task(event.wait_async())
    await asyncio.sleep(0)
    assert not second.done()

    event.set()
    await asyncio.wait_for(second, timeout=1.0)


async def test_clear_from_another_thread_wakes_waiter() -> None:
    """The publishing thread may clear as well as set; the wake-up must arrive."""
    event = RearmableLoopEvent()
    task = asyncio.create_task(event.wait_async())
    await asyncio.sleep(0)

    threading.Timer(0.01, event.clear).start()

    await asyncio.wait_for(task, timeout=1.0)


def test_set_after_waiter_loop_closed_does_not_raise() -> None:
    """A waiter whose loop closed before the event was set must not turn the
    ``set()`` into an exception for whoever is stopping the process."""
    event = RearmableLoopEvent()
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        event._waiters.append((loop, fut))
    finally:
        loop.close()

    event.set()  # must not raise

    assert event._waiters == []


async def test_concurrent_waiters_hold_no_thread() -> None:
    """Any number of waiters must add no OS threads; thread-per-wait is what
    exhausted the default executor."""
    event = RearmableLoopEvent()
    before = threading.active_count()

    tasks = [asyncio.create_task(event.wait_async()) for _ in range(50)]
    await asyncio.sleep(0)

    assert threading.active_count() == before

    event.set()
    await asyncio.gather(*tasks)


def test_sync_wait_api_is_unchanged() -> None:
    """The class is still a ``threading.Event`` for synchronous callers."""
    event = RearmableLoopEvent()
    assert event.wait(timeout=0.01) is False
    event.set()
    assert event.wait(timeout=0.01) is True
