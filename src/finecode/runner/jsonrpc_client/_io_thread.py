import asyncio
import threading
import typing
import collections.abc
from loguru import logger


class AsyncIOThread:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ended_event: typing.Final = threading.Event()
        self._running: bool = False

    def start(self) -> None:
        if self._running:
            raise RuntimeError("IO Thread is already running")

        self._thread = threading.Thread(
            target=self._run_loop, name="IO Thread", daemon=True
        )
        self._thread.start()

        # Wait for the loop to be ready
        while self._loop is None:
            threading.Event().wait(0.01)

        self._running = True
        logger.debug(f"IO Thread started")

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return

        self.run_coroutine(stop_loop_with_timeout(timeout))
        self._running = False
        logger.debug("IO Thread stopped")

    def run_coroutine(
        self, coro: collections.abc.Coroutine[typing.Any, typing.Any, typing.Any]
    ) -> asyncio.Future:
        if not self._running or not self._loop:
            raise RuntimeError("IO Thread is not running")

        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    @property
    def is_running(self) -> bool:
        return self._running

    def _run_loop(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            logger.debug(f"IO Thread event loop started")
            self._loop.run_forever()
        except Exception as e:
            logger.error(f"Error in IO Thread event loop: {e}")
        finally:
            if self._loop and not self._loop.is_closed():
                self._loop.close()
            self._loop = None
            logger.debug(f"IO Thread event loop stopped")


async def stop_loop_with_timeout(timeout: float) -> None:
    loop = asyncio.get_running_loop()
    tasks = [
        task
        for task in asyncio.all_tasks(loop)
        if not task.done() and task != asyncio.current_task()
    ]

    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks)
    except TimeoutError:
        logger.debug("Timeout! Cancelling all tasks...")
        for task in tasks:
            task.cancel()
        # Wait for cancellation to complete
        await asyncio.gather(*tasks, return_exceptions=True)

    loop.stop()
    logger.debug("Stopped event loop in IO thread")
