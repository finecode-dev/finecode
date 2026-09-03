"""One machine-wide budget for subprocess work slots, leased to ERs per run.

The WM owns the single number that bounds how many OS processes (subprocesses
in an ER via ``CommandRunner``, and pool workers via ``ProcessExecutor``) may be
alive across *all* Extension Runners at once.  ERs ask for a lease when an
action run begins and release it when the run ends; the WM re-grants freed
slots to waiting leases and reclaims everything a dead runner held.

The allocator deliberately grants at least one slot to a *nested* lease even
when the budget is already exhausted — a run that is asked for by another run
must always be able to make progress, or the whole chain deadlocks (ADR-0090).
Non-nested leases wait for a slot instead of oversubscribing the machine.

See ADR-0090 for why this replaces the three per-axis throttles that used to
bound project fan-out, prepare-envs fan-out and per-ER subprocess fan-out
separately.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import uuid

from finecode_extension_runner.concurrency import (
    ConcurrencyDecision,
    machine_subprocess_budget,
)
from loguru import logger

__all__ = [
    "ProcessBudget",
    "ProcessLease",
    "resolve_process_budget",
]


@dataclasses.dataclass(frozen=True)
class ProcessLease:
    """One ER's granted share of the machine-wide process budget."""

    lease_id: str
    runner_id: str
    requested: int
    granted: int


def resolve_process_budget(env_value: str | None = None) -> ConcurrencyDecision:
    """Effective size of the machine-wide process budget, with the reason it
    was picked (for logging — see ``ConcurrencyDecision``).

    Priority: ``FINECODE_MAX_CONCURRENT_PROCESSES`` env var (if set) >
    ``machine_subprocess_budget()``.  Machine-bound, like the ER-startup cap,
    so there is no ``finecode-workspace.toml`` equivalent.  ``env_value`` is
    injectable for tests; production callers omit it and let this read
    ``os.environ`` directly.
    """
    if env_value is None:
        env_value = os.environ.get("FINECODE_MAX_CONCURRENT_PROCESSES")
    if env_value is not None:
        return ConcurrencyDecision(
            max(int(env_value), 1), "FINECODE_MAX_CONCURRENT_PROCESSES env var"
        )
    return ConcurrencyDecision(
        machine_subprocess_budget(),
        f"computed default (machine budget {machine_subprocess_budget()})",
    )


class ProcessBudget:
    """Lease work slots to ERs, at most ``size`` of them granted at once.

    ``lease`` grants at least one slot to every request — waiting for a slot
    when the budget is exhausted and the request is not nested, but never
    handing a nested request zero.  ``release`` returns one lease's slots;
    ``reclaim_for_runner`` returns everything a runner still holds (used when
    an ER is force-killed and can no longer release its own leases).
    """

    def __init__(self, size: int) -> None:
        self._size = max(size, 1)
        self._granted = 0
        self._leases: dict[str, ProcessLease] = {}
        self._leases_by_runner: dict[str, set[str]] = {}
        self._condition = asyncio.Condition()

    @property
    def size(self) -> int:
        return self._size

    @property
    def granted(self) -> int:
        return self._granted

    def _available(self) -> int:
        return self._size - self._granted

    def _grant(self, requested: int, *, nested: bool) -> int:
        """How many of *requested* slots can be granted right now.

        A nested request is never refused outright — it gets at least one slot
        so the run that asked for it can make progress (ADR-0090).  A
        non-nested request with nothing available returns 0 and waits.
        """
        available = self._available()
        if available >= requested:
            return requested
        if available > 0:
            return available
        if nested:
            return 1
        return 0

    async def lease(
        self, runner_id: str, requested: int, *, nested: bool = False
    ) -> ProcessLease:
        """Grant a lease for *runner_id*, waiting if necessary for a slot.

        Raises:
            RuntimeError: the lease was reclaimed (the runner died) while it
                was waiting for a slot.
        """
        requested = max(requested, 1)
        lease_id = uuid.uuid4().hex
        lease = ProcessLease(
            lease_id=lease_id, runner_id=runner_id, requested=requested, granted=0
        )
        self._leases[lease_id] = lease
        self._leases_by_runner.setdefault(runner_id, set()).add(lease_id)

        async with self._condition:
            while True:
                if lease_id not in self._leases:
                    raise RuntimeError(
                        f"Process budget lease {lease_id} for '{runner_id}' was "
                        "reclaimed before it could be granted"
                    )
                grant = self._grant(requested, nested=nested)
                if grant > 0:
                    granted_lease = ProcessLease(
                        lease_id=lease_id,
                        runner_id=runner_id,
                        requested=requested,
                        granted=grant,
                    )
                    self._leases[lease_id] = granted_lease
                    self._granted += grant
                    logger.debug(
                        f"Process budget granted {grant}/{requested} slot(s) to "
                        f"'{runner_id}' ({self._granted}/{self._size} in use)"
                    )
                    return granted_lease
                await self._condition.wait()

    async def release(self, lease_id: str) -> None:
        """Return every slot held by one lease."""
        async with self._condition:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                return
            self._leases_by_runner.get(lease.runner_id, set()).discard(lease_id)
            self._granted -= lease.granted
            logger.debug(
                f"Process budget released {lease.granted} slot(s) from "
                f"'{lease.runner_id}' ({self._granted}/{self._size} in use)"
            )
            self._condition.notify_all()

    async def reclaim_for_runner(self, runner_id: str) -> int:
        """Return every slot still held by *runner_id* and wake waiters.

        Used when a runner is torn down without releasing its own leases (a
        force-killed ER, or an ER whose stop never got an explicit release).
        """
        freed = 0
        async with self._condition:
            lease_ids = self._leases_by_runner.pop(runner_id, set())
            for lease_id in lease_ids:
                lease = self._leases.pop(lease_id, None)
                if lease is not None:
                    freed += lease.granted
                    self._granted -= lease.granted
            if freed:
                logger.debug(
                    f"Process budget reclaimed {freed} slot(s) from "
                    f"'{runner_id}' ({self._granted}/{self._size} in use)"
                )
            self._condition.notify_all()
        return freed

    def target_for_runner(self, runner_id: str) -> int:
        """The gate target an ER should use: the sum of its active leases."""
        return sum(
            lease.granted
            for lease_id in self._leases_by_runner.get(runner_id, set())
            if (lease := self._leases.get(lease_id)) is not None
        )


def _make_process_budget() -> ProcessBudget:
    decision = resolve_process_budget()
    logger.info(f"Process budget: {decision.value} ({decision.source})")
    return ProcessBudget(decision.value)
