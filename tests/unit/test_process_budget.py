"""The machine-wide process budget grants work slots to ERs without ever
starving a run to zero (ADR-0090).

A run whose parent holds slots must still be able to make progress, so a
*nested* lease is always granted at least one slot even when the budget is
exhausted.  A non-nested lease waits for a slot instead of oversubscribing,
which is what keeps the total at or below the budget in the common case.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from finecode.wm_server.services.process_budget import (
    ProcessBudget,
    resolve_process_budget,
)


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
    await budget.lease("dead_runner", requested=2)
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
    # FINECODE_MAX_CONCURRENT_PROCESSES now sizes the *combined* budget, so the
    # work half is smaller than the value (ADR-0093).
    monkeypatch.setenv("FINECODE_MAX_CONCURRENT_PROCESSES", "9")

    decision = resolve_process_budget()

    assert decision.value == 5
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


async def test_waiting_lease_gets_one_stall_escape_slot_when_nothing_moves() -> None:
    """A lease that can never get a slot must still not hang forever.

    Without this, a non-nested run waiting behind a slot holder that never
    releases would wait for the entire WM session, and a caller of
    ``prepare-envs`` would never see a result.
    """
    budget = ProcessBudget(size=1, stall_escape_sec=0.05)
    holder = await budget.lease("holder", requested=1)

    waiter = asyncio.create_task(budget.lease("waiter", requested=1))
    escaped = await asyncio.wait_for(waiter, 2)

    assert escaped.granted == 1
    assert escaped.stall_escape is True
    assert budget.granted == 2

    await budget.release(escaped.lease_id)
    await budget.release(holder.lease_id)
    assert budget.granted == 0


async def test_no_stall_escape_while_slots_keep_turning_over() -> None:
    """A slot that keeps being released and re-taken is progress, not a stall.

    If regular turnover could trip the escape, a healthy W-wide fan-out would
    be granted slots over the budget for no reason.
    """
    budget = ProcessBudget(size=1, stall_escape_sec=0.2)
    leases: list = []
    peak = 0

    async def _sample() -> None:
        nonlocal peak
        while True:
            peak = max(peak, budget.granted)
            await asyncio.sleep(0.01)

    async def _turn_over() -> None:
        end = time.monotonic() + 0.6
        while time.monotonic() < end:
            lease = await budget.lease("holder", requested=1)
            leases.append(lease)
            await asyncio.sleep(0.05)
            await budget.release(lease.lease_id)

    async def _waiter() -> None:
        lease = await budget.lease("waiter", requested=1)
        leases.append(lease)
        await asyncio.sleep(0.01)
        await budget.release(lease.lease_id)

    sampler = asyncio.create_task(_sample())
    turn_over = asyncio.create_task(_turn_over())
    waiter = asyncio.create_task(_waiter())
    await asyncio.gather(turn_over, waiter)
    sampler.cancel()
    await asyncio.gather(sampler, return_exceptions=True)

    assert peak <= 1
    assert all(not lease.stall_escape for lease in leases)
    assert budget.granted == 0


async def test_at_most_one_stall_escape_is_outstanding() -> None:
    """Two waiters behind a dead holder must escape one at a time.

    Letting both escape at once would let a stalled budget drift arbitrarily
    far above its size, defeating the bound the budget exists to enforce.
    """
    budget = ProcessBudget(size=1, stall_escape_sec=0.15)
    holder = await budget.lease("holder", requested=1)

    first = asyncio.create_task(budget.lease("first", requested=1))
    second = asyncio.create_task(budget.lease("second", requested=1))

    await asyncio.sleep(0.3)
    assert first.done() != second.done()
    assert budget.granted == 2

    escaped = first.result() if first.done() else second.result()
    assert escaped.stall_escape is True
    await budget.release(escaped.lease_id)

    other = second if first.done() else first
    remaining = await asyncio.wait_for(other, 2)
    assert remaining.stall_escape is True

    await budget.release(remaining.lease_id)
    await budget.release(holder.lease_id)
    assert budget.granted == 0
