"""The machine-wide process budget grants work slots to ERs without ever
starving a run to zero (ADR-0090).

A run whose parent holds slots must still be able to make progress, so a
*nested* lease is always granted at least one slot even when the budget is
exhausted.  A non-nested lease waits for a slot instead of oversubscribing,
which is what keeps the total at or below the budget in the common case.
"""

from __future__ import annotations

import asyncio

import pytest

from finecode.wm_server.services.process_budget import ProcessBudget, resolve_process_budget


async def test_peak_grants_never_exceed_budget_without_nesting() -> None:
    """Twelve top-level runs against a budget of four must not oversubscribe.

    Without this, a machine-wide workspace fan-out would launch as many
    subprocesses as there are projects, starving the WM's own event loop.
    """
    budget = ProcessBudget(size=4)

    async def _hold(lease):
        await asyncio.sleep(0.05)
        await budget.release(lease.lease_id)

    tasks = []
    for i in range(12):
        lease = await budget.lease(f"runner_{i}", requested=4)
        assert budget.granted <= 4
        tasks.append(asyncio.create_task(_hold(lease)))

    await asyncio.gather(*tasks)
    assert budget.granted == 0


async def test_nested_lease_is_granted_one_when_budget_is_exhausted() -> None:
    """A run asked for by another run must never be handed zero slots.

    Zero would deadlock the whole fan-out: the parent holds slots while
    waiting on a child that can never spawn anything (ADR-0090).
    """
    budget = ProcessBudget(size=1)
    outer = await budget.lease("outer_runner", requested=1)

    inner = await budget.lease("inner_runner", requested=4, nested=True)

    assert inner.granted == 1
    assert budget.granted == 2  # soft overshoot only under nesting

    await budget.release(inner.lease_id)
    await budget.release(outer.lease_id)
    assert budget.granted == 0


async def test_reclaim_for_runner_returns_a_dead_runners_slots() -> None:
    """A force-killed runner cannot release its own lease; the WM must be able
    to take its slots back and hand them to the next waiter.
    """
    budget = ProcessBudget(size=2)
    first = await budget.lease("dead_runner", requested=2)
    assert budget.granted == 2

    # The second run must wait — the budget is exhausted and it is not nested.
    waiter = asyncio.create_task(budget.lease("waiting_runner", requested=2))
    await asyncio.sleep(0)
    assert not waiter.done()

    freed = await budget.reclaim_for_runner("dead_runner")
    assert freed == 2

    second = await waiter
    assert second.granted == 2

    await budget.release(second.lease_id)
    assert budget.granted == 0


async def test_partial_grant_throttles_but_never_starves() -> None:
    """When only part of a request is free, the run gets that part rather than
    waiting for the whole ask — throttle, never refuse.
    """
    budget = ProcessBudget(size=4)
    first = await budget.lease("first_runner", requested=3)
    second = await budget.lease("second_runner", requested=3)

    assert first.granted == 3
    assert second.granted == 1  # only one slot was left

    await budget.release(first.lease_id)
    await budget.release(second.lease_id)


def test_resolve_process_budget_env_var_overrides(monkeypatch) -> None:
    monkeypatch.setenv("FINECODE_MAX_CONCURRENT_PROCESSES", "9")

    decision = resolve_process_budget()

    assert decision.value == 9
    assert "env var" in decision.source


def test_resolve_process_budget_clamps_zero_to_one(monkeypatch) -> None:
    monkeypatch.setenv("FINECODE_MAX_CONCURRENT_PROCESSES", "0")

    assert resolve_process_budget().value == 1


def test_resolve_process_budget_falls_back_to_machine_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FINECODE_MAX_CONCURRENT_PROCESSES", raising=False)

    decision = resolve_process_budget()

    assert "computed default" in decision.source
    assert decision.value >= 1


async def test_waiting_lease_is_rejected_when_reclaimed() -> None:
    """A lease that is reclaimed while queued must not be granted afterward —
    the runner is gone and there is nobody left to use the slots.
    """
    budget = ProcessBudget(size=1)
    first = await budget.lease("first_runner", requested=1)

    waiter = asyncio.create_task(budget.lease("dead_runner", requested=1))
    await asyncio.sleep(0)
    assert not waiter.done()

    await budget.reclaim_for_runner("dead_runner")

    with pytest.raises(RuntimeError, match="reclaimed"):
        await waiter

    await budget.release(first.lease_id)
