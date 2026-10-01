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


async def test_cancelled_waiting_lease_leaves_no_record() -> None:
    """A cancelled waiter must not linger as a phantom in usage reports.

    A leftover waiting record would make the resource snapshot report a
    waiter that no longer exists, misleading an operator into waiting on a
    queue that is actually empty.
    """
    budget = ProcessBudget(size=1)
    holder = await budget.lease("holder", requested=1)

    waiter = asyncio.create_task(budget.lease("cancelled_runner", requested=1))
    await asyncio.sleep(0)
    assert not waiter.done()
    assert budget.snapshot().waiting == 1

    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)

    assert budget.snapshot().waiting == 0
    assert all(
        lease.runner_id != "cancelled_runner" for lease in budget._leases.values()
    )
    assert "cancelled_runner" not in budget._leases_by_runner

    await budget.release(holder.lease_id)


async def test_waiting_counter_tracks_ungranted_records() -> None:
    """The waiting count must match the leases still queued for a slot.

    An operator reading the snapshot relies on it to tell a genuinely idle
    budget from one with queued work; a drift in either direction hides the
    queue or invents one.
    """
    budget = ProcessBudget(size=1)
    holder = await budget.lease("holder", requested=1)
    assert budget.snapshot().waiting == 0

    waiter = asyncio.create_task(budget.lease("waiter", requested=1))
    await asyncio.sleep(0)
    assert budget.snapshot().waiting == 1
    assert sum(1 for lease in budget._leases.values() if lease.granted == 0) == 1

    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert budget.snapshot().waiting == 0

    waiter2 = asyncio.create_task(budget.lease("waiter2", requested=1))
    await asyncio.sleep(0)
    assert budget.snapshot().waiting == 1
    await budget.reclaim_for_runner("waiter2")
    with pytest.raises(RuntimeError, match="reclaimed"):
        await waiter2
    assert budget.snapshot().waiting == 0
    assert "waiter2" not in budget._leases_by_runner

    await budget.release(holder.lease_id)
    assert budget.snapshot().waiting == 0


async def test_cancelled_lease_queued_for_lock_leaves_no_record() -> None:
    """A lease cancelled before reaching the wait point must clean up too.

    The same phantom-waiter misreport happens when cancellation lands while
    the lease is still queued for the budget lock rather than parked inside
    it, so both paths need the cleanup.
    """
    budget = ProcessBudget(size=1)
    await budget._condition.acquire()
    try:
        first = asyncio.create_task(budget.lease("queued1", requested=1))
        second = asyncio.create_task(budget.lease("queued2", requested=1))
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    finally:
        budget._condition.release()
    assert budget.snapshot().waiting == 0
    assert "queued1" not in budget._leases_by_runner
    assert "queued2" not in budget._leases_by_runner


async def test_stall_escape_snapshot_reports_escape_and_overshoot() -> None:
    """An escaped budget must be visible as over-budget in the snapshot.

    Without the escape flag an operator cannot tell a legitimately
    over-budget snapshot from a broken allocator.
    """
    budget = ProcessBudget(size=1, stall_escape_sec=0.05)
    holder = await budget.lease("holder", requested=1)

    waiter = asyncio.create_task(budget.lease("waiter", requested=1))
    escaped = await asyncio.wait_for(waiter, 2)

    snapshot = budget.snapshot()
    assert snapshot.stall_escape is True
    assert snapshot.granted == 2
    assert snapshot.size == 1

    await budget.release(escaped.lease_id)
    await budget.release(holder.lease_id)


async def test_snapshot_holders_sum_per_runner_and_omit_waiters() -> None:
    """Holders must show who owns granted slots, not who is waiting.

    Listing a waiter as a holder would send an operator to ask a run that
    owns nothing to release slots it does not have.
    """
    budget = ProcessBudget(size=3)
    first = await budget.lease("runner_a", requested=1)
    second = await budget.lease("runner_a", requested=1)
    third = await budget.lease("runner_b", requested=1)

    waiter = asyncio.create_task(budget.lease("runner_c", requested=1))
    await asyncio.sleep(0)
    assert not waiter.done()

    snapshot = budget.snapshot()
    assert snapshot.waiting == 1
    assert dict(snapshot.holders) == {"runner_a": 2, "runner_b": 1}
    assert snapshot.holders[0] == ("runner_a", 2)

    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    await budget.release(first.lease_id)
    await budget.release(second.lease_id)
    await budget.release(third.lease_id)


async def test_snapshot_peaks_survive_release() -> None:
    """A burst between polls must still show in peaks after it drains.

    Polling misses bursts that start and end between samples; without stored
    peaks an operator would never see the queue that actually stalled the run.
    """
    budget = ProcessBudget(size=1, stall_escape_sec=3600.0)
    holder = await budget.lease("holder", requested=1)

    first = asyncio.create_task(budget.lease("waiter1", requested=1))
    second = asyncio.create_task(budget.lease("waiter2", requested=1))
    await asyncio.sleep(0.1)
    assert budget.snapshot().waiting == 2

    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    await budget.release(holder.lease_id)

    snapshot = budget.snapshot()
    assert snapshot.granted == 0
    assert snapshot.waiting == 0
    assert snapshot.peak_granted == 1
    assert snapshot.peak_waiting == 2


async def test_nested_lease_never_raises_waiting_peak() -> None:
    """A nested grant on an exhausted budget is progress, not waiting.

    Counting it would make every nested fan-out look like a queued budget
    and drown the real waiters operators need to see.
    """
    budget = ProcessBudget(size=1)
    holder = await budget.lease("holder", requested=1)

    inner = await budget.lease("inner", requested=4, nested=True)

    snapshot = budget.snapshot()
    assert snapshot.granted == 2
    assert snapshot.waiting == 0
    assert snapshot.peak_waiting == 0
    assert snapshot.peak_granted == 2

    await budget.release(inner.lease_id)
    await budget.release(holder.lease_id)


async def test_snapshot_observes_leases_queued_for_lock() -> None:
    """Leases queued for the lock count as waiting once observed.

    They have not reached a wait point yet, but a snapshot reader can already
    see them waiting, so the peak must be stored to stay monotonic after they
    are granted without ever parking.
    """
    budget = ProcessBudget(size=2)
    await budget._condition.acquire()
    try:
        first = asyncio.create_task(budget.lease("queued1", requested=1))
        second = asyncio.create_task(budget.lease("queued2", requested=1))
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        snapshot = budget.snapshot()
        assert snapshot.waiting == 2
        assert snapshot.peak_waiting == 2
    finally:
        budget._condition.release()

    first_lease = await first
    second_lease = await second
    snapshot = budget.snapshot()
    assert snapshot.waiting == 0
    assert snapshot.peak_waiting == 2

    await budget.release(first_lease.lease_id)
    await budget.release(second_lease.lease_id)
