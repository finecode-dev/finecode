from __future__ import annotations

import asyncio
import contextlib
import threading

__all__ = ["LoopAwareEvent"]


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


class LoopAwareEvent(threading.Event):
    """A ``threading.Event`` a coroutine can await without holding a thread.

    ``threading.Event.wait()`` blocks the calling OS thread, so an asyncio
    caller can only use it through ``asyncio.to_thread`` — which pins a worker
    of the default executor for as long as the event stays unset. A client
    that waits this way for its whole life therefore exhausts the executor's
    fixed worker pool. ``wait_async`` registers a future on the running loop
    instead, so no thread is held.

    Set-once: ``clear()`` is unsupported. Every caller needs wake-up on the
    first set and none needs to re-arm.
    """

    def __init__(self) -> None:
        super().__init__()
        self._waiters_lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    def set(self) -> None:
        super().set()
        with self._waiters_lock:
            waiters, self._waiters = self._waiters, []
        for loop, fut in waiters:
            with contextlib.suppress(RuntimeError):
                # The waiter's loop may already be closed; nothing to wake then.
                loop.call_soon_threadsafe(_resolve, fut)

    async def wait_async(self) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        with self._waiters_lock:
            # The flag is checked under the same lock `set()` drains under, and
            # `set()` sets the flag before draining, so a concurrent set is
            # either seen here or resolves the registered future — never lost.
            if self.is_set():
                return
            self._waiters.append((loop, fut))
        try:
            await fut
        finally:
            with self._waiters_lock, contextlib.suppress(ValueError):
                self._waiters.remove((loop, fut))
