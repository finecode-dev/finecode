"""One machine-wide budget for subprocess work slots, leased to ERs per run.

The WM owns the single number that bounds how many OS processes (subprocesses
in an ER via ``CommandRunner``, and pool workers via ``ProcessExecutor``) may be
alive across *all* Extension Runners at once.  ERs ask for a lease when an
action run begins and release it when the run ends; the WM re-grants freed
slots to waiting leases and reclaims everything a dead runner held.

This is the *work* half of one combined subprocess-concurrency budget: the WM
splits a single machine-bound total into the ER-startup cap and this work cap
so their sum leaves a core for the WM's own event loop (ADR-0093).
``resolve_subprocess_budgets`` is the single resolution point.

The allocator deliberately grants at least one slot to a *nested* lease even
when the budget is already exhausted — a run that is asked for by another run
must always be able to make progress, or the whole chain deadlocks (ADR-0090).
Leases that wait (non-nested) are held to the budget plus at most one
stall-escape slot — see ``STALL_ESCAPE_SEC``. Leases that do not wait take at
least one slot each, so their total is the budget or the number of active
non-waiting runs, whichever is greater (ADR-0094).

See ADR-0090 for why this replaces the three per-axis throttles that used to
bound project fan-out, prepare-envs fan-out and per-ER subprocess fan-out
separately.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
import uuid

from finecode_extension_runner.concurrency import (
    ConcurrencyDecision,
    machine_subprocess_budget,
)
from loguru import logger

STALL_ESCAPE_SEC = 30.0
"""A non-nested waiter with no budget movement this long is granted one slot
over the budget, once, so no waiting lease can hang forever (ADR-0094)."""

__all__ = [
    "STALL_ESCAPE_SEC",
    "ProcessBudget",
    "ProcessLease",
    "SubprocessBudgets",
    "resolve_process_budget",
    "resolve_subprocess_budgets",
]


@dataclasses.dataclass(frozen=True)
class ProcessLease:
    """One ER's granted share of the machine-wide process budget."""

    lease_id: str
    runner_id: str
    requested: int
    granted: int
    stall_escape: bool = False


@dataclasses.dataclass(frozen=True)
class SubprocessBudgets:
    """The one machine-bound concurrency budget, split into its two consumers.

    ``total`` is the combined ceiling; ``startup_cap`` sizes the ER-startup
    semaphore and ``work_cap`` the process-budget allocator.  Their sum stays
    at or below ``total`` so the WM's own event loop always has headroom, while
    the two caps remain separate objects so a run holding work slots can still
    start the ER it fans into (ADR-0090).  ``total_source`` is carried for the
    startup log.
    """

    total: int
    startup_cap: int
    work_cap: int
    total_source: str


def resolve_subprocess_budgets(env_value: str | None = None) -> SubprocessBudgets:
    """Resolve the combined subprocess-concurrency budget and split it.

    ``total``: ``FINECODE_MAX_CONCURRENT_PROCESSES`` env var (if set) >
    ``machine_subprocess_budget()``.  ``startup_cap`` is half of the total and
    ``work_cap`` the remainder, never below one each.  Machine-bound, so there
    is no ``finecode-workspace.toml`` equivalent.  ``env_value`` is injectable
    for tests; production callers omit it and let this read ``os.environ``
    directly.  See ADR-0093.
    """
    if env_value is None:
        env_value = os.environ.get("FINECODE_MAX_CONCURRENT_PROCESSES")
    if env_value is not None:
        total = max(int(env_value), 1)
        total_source = "FINECODE_MAX_CONCURRENT_PROCESSES env var"
    else:
        total = machine_subprocess_budget()
        total_source = (
            f"computed default (machine budget {machine_subprocess_budget()})"
        )

    startup_cap = max(1, total // 2)
    return SubprocessBudgets(
        total=total,
        startup_cap=startup_cap,
        work_cap=max(1, total - startup_cap),
        total_source=total_source,
    )


def resolve_process_budget(env_value: str | None = None) -> ConcurrencyDecision:
    """Effective size of the work-slot half of the combined budget (ADR-0093).

    Kept for callers and tests that only want the work cap;
    ``WorkspaceContext`` uses ``resolve_subprocess_budgets`` directly so both
    caps come from one resolution.  ``env_value`` is the combined total (the
    work cap is then ``total − startup_cap``), injectable for tests.
    """
    budgets = resolve_subprocess_budgets(env_value=env_value)
    return ConcurrencyDecision(
        budgets.work_cap,
        f"{budgets.total_source}; work cap of combined budget {budgets.total}",
    )


class ProcessBudget:
    """Lease work slots to ERs, at most ``size`` of them granted at once.

    ``lease`` grants at least one slot to every request — waiting for a slot
    when the budget is exhausted and the request is not nested, but never
    handing a nested request zero.  A waiting lease that sees no budget
    movement for ``stall_escape_sec`` is granted one slot over the budget,
    and at most one such escape is outstanding at a time, so no waiter can
    hang forever (ADR-0094).  ``release`` returns one lease's slots;
    ``reclaim_for_runner`` returns everything a runner still holds (used when
    an ER is force-killed and can no longer release its own leases).
    """

    def __init__(
        self, size: int, *, stall_escape_sec: float = STALL_ESCAPE_SEC
    ) -> None:
        self._size = max(size, 1)
        self._granted = 0
        self._leases: dict[str, ProcessLease] = {}
        self._leases_by_runner: dict[str, set[str]] = {}
        self._condition = asyncio.Condition()
        self._stall_escape_sec = stall_escape_sec
        self._last_progress = time.monotonic()
        self._stall_escape_lease_id: str | None = None

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
        waited_since = time.monotonic()
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
                    self._last_progress = time.monotonic()
                    logger.debug(
                        f"Process budget granted {grant}/{requested} slot(s) to "
                        f"'{runner_id}' ({self._granted}/{self._size} in use)"
                    )
                    return granted_lease
                # Non-nested and nothing available: wait for a release, but
                # escape a stall that never moves so no lease hangs forever.
                now = time.monotonic()
                stalled_for = now - max(waited_since, self._last_progress)
                if (
                    self._stall_escape_lease_id is None
                    and stalled_for >= self._stall_escape_sec
                ):
                    granted_lease = ProcessLease(
                        lease_id=lease_id,
                        runner_id=runner_id,
                        requested=requested,
                        granted=1,
                        stall_escape=True,
                    )
                    self._leases[lease_id] = granted_lease
                    self._granted += 1
                    self._last_progress = now
                    self._stall_escape_lease_id = lease_id
                    logger.warning(
                        f"Process budget stalled: '{runner_id}' waited "
                        f"{stalled_for:.1f}s for a slot (stall window "
                        f"{self._stall_escape_sec:.1f}s); granting 1 over budget "
                        f"({self._granted}/{self._size} in use). Holders: "
                        f"{self._holders_summary()}"
                    )
                    return granted_lease
                if self._stall_escape_lease_id is not None:
                    # An escape is already outstanding; its release is what
                    # will wake us, so there is nothing to time out.
                    await self._condition.wait()
                else:
                    try:
                        async with asyncio.timeout(
                            max(self._stall_escape_sec - stalled_for, 0.01)
                        ):
                            await self._condition.wait()
                    except TimeoutError:
                        # Re-evaluate under the lock; the next pass may grant
                        # the stall escape.
                        pass

    def _holders_summary(self) -> str:
        """The active leases' runner ids and slot totals, largest first."""
        totals: dict[str, int] = {}
        for held in self._leases.values():
            totals[held.runner_id] = totals.get(held.runner_id, 0) + held.granted
        ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        return ", ".join(f"{rid}→{slots}" for rid, slots in ranked[:5])

    async def release(self, lease_id: str) -> None:
        """Return every slot held by one lease."""
        async with self._condition:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                return
            self._leases_by_runner.get(lease.runner_id, set()).discard(lease_id)
            self._granted -= lease.granted
            self._last_progress = time.monotonic()
            if self._stall_escape_lease_id == lease_id:
                self._stall_escape_lease_id = None
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
                    self._last_progress = time.monotonic()
                    if self._stall_escape_lease_id == lease_id:
                        self._stall_escape_lease_id = None
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
