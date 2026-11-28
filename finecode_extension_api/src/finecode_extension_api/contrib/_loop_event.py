from __future__ import annotations

import asyncio
import contextlib
import threading

__all__ = ["RearmableLoopEvent"]

_Waiter = tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


class RearmableLoopEvent(threading.Event):
    """A ``threading.Event`` a coroutine can await, re-armed by ``clear()``.

    The same bridge as ``LoopAwareEvent`` (a future registered on the waiting
    loop, so no OS thread is held while waiting), plus the re-arm cycle a
    repeating wait needs: ``set`` wakes the current waiters, ``clear`` drops
    them and accepts new ones. Both update the flag and swap the waiter list
    under one lock, so a concurrent ``wait_async`` either observes the new flag
    or has the future it just registered resolved — a wake-up cannot be lost.

    Deliberately a sibling rather than a change to ``LoopAwareEvent``: that one
    is set-once by contract, and code that relies on "never cleared" should not
    have to reason about clearers.
    """

    def __init__(self) -> None:
        super().__init__()
        self._waiters_lock = threading.Lock()
        self._waiters: list[_Waiter] = []

    def set(self) -> None:
        with self._waiters_lock:
            super().set()
            waiters = self._waiters
            self._waiters = []
        self._wake(waiters)

    def clear(self) -> None:
        with self._waiters_lock:
            super().clear()
            waiters = self._waiters
            self._waiters = []
        self._wake(waiters)

    @staticmethod
    def _wake(waiters: list[_Waiter]) -> None:
        for loop, fut in waiters:
            with contextlib.suppress(RuntimeError):
                # The waiter's loop may already be closed; nothing to wake then.
                _ = loop.call_soon_threadsafe(_resolve, fut)

    async def wait_async(self) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        with self._waiters_lock:
            if self.is_set():
                return
            self._waiters.append((loop, fut))
        try:
            await fut
        finally:
            with self._waiters_lock, contextlib.suppress(ValueError):
                self._waiters.remove((loop, fut))
