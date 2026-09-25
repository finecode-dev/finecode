"""A run may declare how the process budget treats its ERs' leases (ADR-0094).

The classifier lives at dispatch, not in the action: a run that declares
``RunBudget(waits=True)`` is held to the budget, while an unmarked run keeps
the ER's own nesting flag — and a nested lease is always granted at least one
slot.  These tests pin that wiring and the bounds it produces.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import (
    in_flight_runs,
    prepare_envs_service,
    process_budget,
)


def _make_context(project_path: Path, *, size: int = 4) -> context.WorkspaceContext:
    ws_context = context.WorkspaceContext(ws_dirs_paths=[project_path])
    ws_context.process_budget = process_budget.ProcessBudget(size, stall_escape_sec=60)
    return ws_context


async def _lease_as_er(
    ws_context: context.WorkspaceContext,
    project_path: Path,
    run_id: str,
    budget: domain.RunBudget,
    leases: list,
) -> None:
    """Lease exactly as an ER does: request the machine budget, flag nesting."""
    async with in_flight_runs.track(
        ws_context,
        run_id=run_id,
        action_name="create_envs",
        project_path=project_path,
        budget=budget,
        origin=None,
    ):
        requested, nested = runner_manager.resolve_lease_terms(
            ws_context, requested=7, nested=True, run_id=run_id
        )
        lease = await ws_context.process_budget.lease(
            f"runner-{run_id}", requested=requested, nested=nested
        )
        leases.append(lease)
        await asyncio.sleep(0.05)
        await ws_context.process_budget.release(lease.lease_id)


async def _sample_grants(
    ws_context: context.WorkspaceContext, samples: list[int]
) -> None:
    while True:
        samples.append(ws_context.process_budget.granted)
        await asyncio.sleep(0.002)


async def test_prepare_envs_shaped_fan_out_never_exceeds_the_work_budget(
    tmp_path: Path,
) -> None:
    """A prepare-envs fan-out that declares a waiting budget must hold at most
    W projects' worth of slots, not W per project.

    Without this, a large workspace's env creation over-subscribes the machine
    and starves the WM's event loop until healthy envs miss their check.
    """
    ws_context = _make_context(tmp_path, size=4)
    leases: list = []
    samples: list[int] = []
    sampler = asyncio.create_task(_sample_grants(ws_context, samples))

    budget = prepare_envs_service.project_fan_out_budget(4, 12)
    tasks = [
        asyncio.create_task(
            _lease_as_er(ws_context, tmp_path, f"run-{i}", budget, leases)
        )
        for i in range(12)
    ]
    await asyncio.gather(*tasks)
    sampler.cancel()
    await asyncio.gather(sampler, return_exceptions=True)

    assert samples
    assert max(samples) == 4
    assert all(lease.granted == 1 for lease in leases)
    assert ws_context.process_budget.granted == 0


async def test_unmarked_fan_out_is_not_bounded_by_the_budget(
    tmp_path: Path,
) -> None:
    """An unmarked run keeps today's behaviour: a nested lease overshoots.

    This is the honest bound the docs must state — only runs that declare a
    waiting budget are held to it.
    """
    ws_context = _make_context(tmp_path, size=4)
    leases: list = []

    budget = domain.RunBudget()
    tasks = [
        asyncio.create_task(
            _lease_as_er(ws_context, tmp_path, f"run-{i}", budget, leases)
        )
        for i in range(12)
    ]
    await asyncio.sleep(0.02)
    over_grant = ws_context.process_budget.granted
    await asyncio.gather(*tasks)

    assert over_grant > 4
    assert ws_context.process_budget.granted == 0


async def test_track_stores_the_declared_budget(tmp_path: Path) -> None:
    """The declared budget must ride on the in-flight entry the lease handler
    looks up by the ER's run id.
    """
    ws_context = _make_context(tmp_path)
    declared = domain.RunBudget(waits=True, max_slots=2)

    async with in_flight_runs.track(
        ws_context,
        run_id="run-1",
        action_name="create_envs",
        project_path=tmp_path,
        budget=declared,
        origin=None,
    ):
        entry = ws_context.in_flight_runs[tmp_path]["run-1"]
        assert entry.budget is declared


def test_resolve_lease_terms_passes_through_for_unknown_run(tmp_path: Path) -> None:
    """A lease with no matching in-flight run must not be changed."""
    ws_context = _make_context(tmp_path)

    assert runner_manager.resolve_lease_terms(
        ws_context, requested=7, nested=False, run_id="absent"
    ) == (7, False)


def test_resolve_lease_terms_passes_through_when_run_id_is_none(
    tmp_path: Path,
) -> None:
    """A lease that names no run must not be changed."""
    ws_context = _make_context(tmp_path)

    assert runner_manager.resolve_lease_terms(
        ws_context, requested=7, nested=True, run_id=None
    ) == (7, True)


async def test_never_waits_overrides_the_er_flag(tmp_path: Path) -> None:
    """A run marked ``waits=False`` must escape even if the ER flagged it root."""
    ws_context = _make_context(tmp_path)

    async with in_flight_runs.track(
        ws_context,
        run_id="run-1",
        action_name="dispatch",
        project_path=tmp_path,
        budget=domain.RunBudget(waits=False),
        origin=None,
    ):
        assert runner_manager.resolve_lease_terms(
            ws_context, requested=7, nested=False, run_id="run-1"
        ) == (7, True)


async def test_waits_overrides_the_er_flag(tmp_path: Path) -> None:
    """A run marked ``waits=True`` must wait even if the ER flagged it nested."""
    ws_context = _make_context(tmp_path)

    async with in_flight_runs.track(
        ws_context,
        run_id="run-1",
        action_name="create_envs",
        project_path=tmp_path,
        budget=domain.RunBudget(waits=True),
        origin=None,
    ):
        assert runner_manager.resolve_lease_terms(
            ws_context, requested=7, nested=True, run_id="run-1"
        ) == (7, False)


async def test_max_slots_caps_the_requested_width(tmp_path: Path) -> None:
    """A declared ``max_slots`` bounds what the lease asks for."""
    ws_context = _make_context(tmp_path)

    async with in_flight_runs.track(
        ws_context,
        run_id="run-1",
        action_name="create_envs",
        project_path=tmp_path,
        budget=domain.RunBudget(waits=True, max_slots=2),
        origin=None,
    ):
        assert runner_manager.resolve_lease_terms(
            ws_context, requested=7, nested=False, run_id="run-1"
        ) == (2, False)


def test_project_fan_out_budget_shares_the_work_cap() -> None:
    """The fan-out's per-project share keeps about W projects in flight.

    With more projects than slots every member takes one slot; with no more
    projects than slots a single project still gets the whole work cap.
    """
    for project_count, expected_max_slots in [(1, 4), (2, 2), (73, 1), (0, 1)]:
        budget = prepare_envs_service.project_fan_out_budget(4, project_count)
        assert budget.waits is True
        assert budget.max_slots == expected_max_slots
