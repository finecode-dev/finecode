"""The combined subprocess-concurrency budget splits into ER-startup and work
caps that together never exceed the machine budget (ADR-0093).

Each cap on its own reserved one core, but the two gate different phases of the
same ER fleet and can be saturated at the same wall-clock moment.  Splitting one
total makes the "-1 core for the WM" guarantee hold in aggregate, while keeping
the caps separate so a run holding work slots can still start the ER it fans
into (ADR-0090).
"""

from __future__ import annotations

import asyncio

import pytest

from finecode.wm_server.services import process_budget
from finecode.wm_server.services.process_budget import (
    ProcessBudget,
    resolve_subprocess_budgets,
)


def test_default_split_stays_within_the_machine_budget(monkeypatch) -> None:
    """With no env vars the two caps must sum to at most the machine budget.

    Without this, the defaults return to 2 * (cores - 1) concurrent heavy
    operations and the WM event loop can be starved past its 10s RPC timeout.
    """
    monkeypatch.delenv("FINECODE_MAX_CONCURRENT_PROCESSES", raising=False)
    monkeypatch.setattr(process_budget, "machine_subprocess_budget", lambda: 8)

    budgets = resolve_subprocess_budgets()

    assert budgets.total == 8
    assert budgets.startup_cap + budgets.work_cap <= 8
    assert budgets.startup_cap >= 1
    assert budgets.work_cap >= 1
    assert "computed default" in budgets.total_source


def test_env_value_is_the_combined_total_not_the_work_cap(monkeypatch) -> None:
    """``FINECODE_MAX_CONCURRENT_PROCESSES`` sizes the total, so the work cap is
    strictly smaller than the value — a caller cannot accidentally stack a second
    cores-1 budget on top of it (ADR-0093).
    """
    monkeypatch.setenv("FINECODE_MAX_CONCURRENT_PROCESSES", "9")

    budgets = resolve_subprocess_budgets()

    assert budgets.total == 9
    assert budgets.startup_cap + budgets.work_cap == 9
    assert budgets.work_cap < 9
    assert "env var" in budgets.total_source


def test_removed_er_startup_env_var_is_ignored(monkeypatch) -> None:
    """``FINECODE_WM_MAX_CONCURRENT_ER_STARTS`` is removed (ADR-0093); setting it
    must not move the split, or a stale CI value would silently reintroduce the
    stacking this budget exists to prevent."""
    monkeypatch.setenv("FINECODE_MAX_CONCURRENT_PROCESSES", "8")
    monkeypatch.setenv("FINECODE_WM_MAX_CONCURRENT_ER_STARTS", "1")

    budgets = resolve_subprocess_budgets()

    assert budgets.startup_cap == 4  # half of 8, not 1
    assert budgets.work_cap == 4


@pytest.mark.parametrize("total", range(1, 129))
def test_split_invariant_holds_for_every_total(monkeypatch, total: int) -> None:
    """For any configured total the caps are both >= 1 and the split never
    exceeds the total (with a documented two-process floor at total == 1)."""
    monkeypatch.setenv("FINECODE_MAX_CONCURRENT_PROCESSES", str(total))

    budgets = resolve_subprocess_budgets()

    assert budgets.startup_cap >= 1
    assert budgets.work_cap >= 1
    if total >= 2:
        assert budgets.startup_cap + budgets.work_cap == total
    assert budgets.startup_cap + budgets.work_cap <= max(total, 2)


async def test_startup_and_work_caps_share_no_lock() -> None:
    """The split must not couple the two budgets through a shared lock: a run
    holding the startup semaphore must still be able to lease work slots, and
    vice versa.  A shared pool would deadlock the fan-out (ADR-0090/ADR-0093).
    """
    startup = asyncio.Semaphore(1)
    work = ProcessBudget(size=1)

    async with startup:
        lease = await asyncio.wait_for(work.lease("r", requested=1), timeout=1)
        await work.release(lease.lease_id)

    held = await work.lease("r2", requested=1)
    await asyncio.wait_for(startup.acquire(), timeout=1)
    startup.release()
    await work.release(held.lease_id)
