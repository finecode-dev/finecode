"""An event loop whose clock jumps to the next timer instead of waiting for it.

Code under test keeps calling ``asyncio.sleep``, ``asyncio.wait(timeout=...)`` and
``loop.time()``; a 60 s deadline then runs in milliseconds, and a test measures
timing from ``loop.time()`` instead of patching sleeps or constants.
"""

import asyncio
import collections.abc
import selectors
import typing

_T = typing.TypeVar("_T")


class _AdvancingSelector(selectors.DefaultSelector):
    """Never waits for a timer: when nothing is ready, moves the loop clock to it."""

    def __init__(self, loop: "VirtualTimeLoop") -> None:
        super().__init__()
        self._loop = loop

    def select(
        self, timeout: float | None = None
    ) -> list[tuple[selectors.SelectorKey, int]]:
        events = super().select(0)
        if events or timeout == 0:
            return events
        if timeout is None:
            raise RuntimeError(
                "virtual-time loop would block forever: nothing is ready and no timer"
                " is pending"
            )
        self._loop.advance(timeout)
        return events


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """Event loop on a virtual clock that starts at 0.0.

    When no callback is ready, the clock jumps to the earliest timer. Waiting
    with nothing ready and no timer pending raises ``RuntimeError`` instead of
    hanging the test. The code under test must not wait on real I/O, threads
    or subprocesses: the clock does not stop for them.
    """

    def __init__(self) -> None:
        self._now = 0.0
        super().__init__(selector=_AdvancingSelector(self))

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward without running anything, as a blocked loop would."""
        self._now += seconds


def run_on_virtual_time(
    coro: collections.abc.Coroutine[typing.Any, typing.Any, _T],
) -> _T:
    """Run ``coro`` to completion on a fresh ``VirtualTimeLoop``."""
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        return runner.run(coro)
