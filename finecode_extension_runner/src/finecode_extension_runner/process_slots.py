"""One ER-lifetime gate shared by every OS-process spawn inside this runner.

``CommandRunner`` and ``ProcessExecutor`` both draw from the same
:class:`ProcessSlots` gate, whose target is the ER's leased quota from the
WM's machine-wide process budget (ADR-0090).  The gate is a counter plus an
``asyncio.Condition``, deliberately not an ``asyncio.Semaphore``: a target that
can shrink must block *new* grants without revoking slots already held, and a
semaphore cannot be resized below its in-flight count.

The process default instance is a module-level singleton so both
``ProcessExecutor`` (constructed per action, outside the DI registry) and
``CommandRunner`` (constructed through the DI registry) can reach the same
gate.  It lives for the ER process's whole lifetime, independent of any
``RunnerContext`` rebuild.
"""

from __future__ import annotations

import asyncio

from finecode_extension_runner.concurrency import machine_subprocess_budget

__all__ = [
    "ProcessSlots",
    "get_process_slots",
    "reset_process_slots",
    "set_process_slots",
]


class ProcessSlots:
    """A resizable gate for the ER's share of the process budget."""

    def __init__(self, target: int) -> None:
        self._target = max(target, 1)
        self._in_flight = 0
        self._condition = asyncio.Condition()

    @property
    def target(self) -> int:
        return self._target

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def acquire(self) -> None:
        """Wait for a free slot and take it.

        Never raises: shrinking the target only delays new grants, it does not
        revoke slots already held (ADR-0090).
        """
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight < self._target)
            self._in_flight += 1

    async def release(self) -> None:
        """Return one slot, waking any waiters."""
        async with self._condition:
            self._in_flight -= 1
            self._condition.notify_all()

    async def set_target(self, target: int) -> None:
        """Resize the gate.

        Growing wakes waiters; shrinking only blocks future grants until the
        in-flight count falls back to the new target.  Held slots are never
        revoked.
        """
        async with self._condition:
            self._target = max(target, 1)
            self._condition.notify_all()


_process_slots: ProcessSlots | None = None


def get_process_slots() -> ProcessSlots:
    """The process-wide gate, created lazily from the machine budget."""
    global _process_slots
    if _process_slots is None:
        _process_slots = ProcessSlots(machine_subprocess_budget())
    return _process_slots


def set_process_slots(slots: ProcessSlots) -> None:
    """Replace the process-wide gate. Tests only."""
    global _process_slots
    _process_slots = slots


def reset_process_slots() -> None:
    """Forget the process-wide gate so the next access recreates it. Tests only."""
    global _process_slots
    _process_slots = None
