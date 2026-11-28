"""Explicit work-slot scopes for CPU-heavy work sent to long-lived processes.

Ambient state: the in-scope mark is a ``ContextVar`` because its consumers
(``ProcessSlots``, ``CommandRunner``, ``ProjectActionRunnerImpl``, the WM
request sender) are registered once per configuration and shared by every run,
so a per-scope value cannot reach them through the object a handler already
holds, and the alternative is a public API change that makes every extension
call site pass a scope token. It is set at one choke point
(``WorkSlots.acquire``) and read at the edge (``ensure_outside_scope``).
"""

from __future__ import annotations

import contextlib
import contextvars
import typing
from collections.abc import AsyncGenerator

from finecode_extension_api.interfaces.iworkslots import IWorkSlots, WorkSlotScopeError

if typing.TYPE_CHECKING:
    from finecode_extension_runner.process_slots import ProcessSlots

__all__ = ["WorkSlots", "ensure_outside_scope"]

_IN_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "finecode_work_slot_scope", default=None
)


def ensure_outside_scope(operation: str) -> None:
    """Refuse an operation that can wait on the work slot the scope holds."""
    if _IN_SCOPE.get() is not None:
        raise WorkSlotScopeError(
            f"{operation} inside IWorkSlots.acquire() — "
            "it can wait on the slot this scope holds"
        )


class WorkSlots(IWorkSlots):
    def __init__(self, slots: ProcessSlots) -> None:
        self._slots = slots

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[None, None]:
        ensure_outside_scope("acquiring a work slot")
        lease_id = await self._slots.acquire()
        token = _IN_SCOPE.set("work-slot-scope")
        try:
            yield
        finally:
            _IN_SCOPE.reset(token)
            await self._slots.release(lease_id)
