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
import collections.abc

from loguru import logger

from finecode_extension_runner.concurrency import machine_subprocess_budget
from finecode_extension_runner.work_slots import ensure_outside_scope

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
        self._lease: (
            collections.abc.Callable[[], collections.abc.Awaitable[str]] | None
        ) = None
        self._release_backend: (
            collections.abc.Callable[[str], collections.abc.Awaitable[None]] | None
        ) = None
        # Strong references to cancellation-cleanup tasks. Without them the
        # event loop keeps only a weak reference, so a cleanup task can be
        # garbage-collected before it returns a granted lease — leaking one WM
        # lease, which at a budget of 1 costs every later unit a stall window.
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    @property
    def target(self) -> int:
        return self._target

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def attach_budget(
        self,
        lease: collections.abc.Callable[[], collections.abc.Awaitable[str]],
        release: collections.abc.Callable[[str], collections.abc.Awaitable[None]],
    ) -> None:
        """Attach the WM lease backend; without it the gate is purely local."""
        self._lease = lease
        self._release_backend = release

    async def acquire(self) -> str | None:
        """Take the local slot, then a WM lease, returning the lease id.

        With no backend attached this is today's gate and returns None. A
        backend failure also returns None and keeps the local slot: a throttle
        never refuses work (ADR-0090).
        """
        ensure_outside_scope("starting a bounded process or executor task")
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight < self._target)
            self._in_flight += 1
        if self._lease is None:
            return None
        try:
            lease_task: asyncio.Task[str] = asyncio.create_task(self._lease())
            return await asyncio.shield(lease_task)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._return_cancelled(lease_task))
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            raise
        except Exception as exc:
            logger.warning(f"Process budget lease failed; continuing locally: {exc}")
            return None

    async def _return_cancelled(self, lease_task: asyncio.Task[str]) -> None:
        """Return the local slot at once, then release the pending grant if any."""
        async with self._condition:
            self._in_flight -= 1
            self._condition.notify_all()
        try:
            lease_id = await lease_task
        except asyncio.CancelledError:
            logger.debug("Cancelled budget lease never granted; nothing to release")
            return
        except Exception as exc:
            logger.debug(f"Cancelled budget lease failed; nothing to release: {exc}")
            return
        if self._release_backend is None:
            return
        try:
            await self._release_backend(lease_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"Process budget release failed for a cancelled acquire: {exc}"
            )

    async def release(self, lease_id: str | None) -> None:
        """Return the local slot and the WM lease from its acquire.

        The id is required, with no default, so a caller cannot drop a lease
        by omission. A cancelled release still reached the WM.
        """
        try:
            if lease_id is not None and self._release_backend is not None:
                try:
                    await self._release_backend(lease_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"Process budget release failed: {exc}")
        finally:
            async with self._condition:
                self._in_flight -= 1
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
