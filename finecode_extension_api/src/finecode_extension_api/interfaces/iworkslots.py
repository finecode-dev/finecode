import contextlib
from typing import Protocol


class WorkSlotScopeError(RuntimeError):
    """An operation that can wait on a work slot was attempted inside its scope."""


class IWorkSlots(Protocol):
    def acquire(self) -> contextlib.AbstractAsyncContextManager[None]:
        """Hold one machine work slot while this code causes CPU-heavy work
        it does not run as its own bounded child process — typically a request
        to a long-lived local server. Inside the block, starting a bounded
        process, submitting to IProcessExecutor, acquiring another slot,
        dispatching an action, or asking the WM anything through an injected
        service raises WorkSlotScopeError: each of those can wait on a slot
        this block holds. Keep the block to the work itself; a wait that is
        not the work belongs outside it."""
        ...
