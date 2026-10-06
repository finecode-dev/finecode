"""The WM leases work slots per unit of work and pushes no gate target.

Each lease carries the run it belongs to in the debug log so an over-grant can
be traced to the run that holds it, and an unknown run logs placeholders
rather than failing the lease.
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.runner import (
    _internal_client_types,
    runner_client,
    runner_manager,
)
from finecode.wm_server.services import prepare_envs_service
from finecode.wm_server.services.process_budget import ProcessBudget
from finecode.wm_server.testing import make_running_runner


class _Records:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: object) -> None:
        self.messages.append(str(message))


def _ws_context(dir_path: Path) -> context.WorkspaceContext:
    return context.WorkspaceContext(ws_dirs_paths=[dir_path])


async def test_lease_returns_grant_and_logs_its_run(tmp_path: Path) -> None:
    """A lease records which run holds it, so a stuck slot has an owner."""
    ws_context = _ws_context(tmp_path)
    run = domain.InFlightRun(
        run_id="r1",
        action_name="lint",
        project_path=tmp_path,
        started_at=time.time(),
    )
    ws_context.in_flight_runs[tmp_path] = {"r1": run}
    runner = make_running_runner(working_dir_path=tmp_path)
    params = _internal_client_types.LeaseProcessBudgetParams(
        requested=1, nested=False, run_id="r1"
    )

    records = _Records()
    sink = logger.add(records)
    try:
        result = await runner_manager.lease_for_runner(ws_context, runner, params)
    finally:
        logger.remove(sink)

    assert result.lease_id
    assert result.granted >= 1
    leases = [m for m in records.messages if "Process budget lease run=r1" in m]
    assert len(leases) == 1
    assert "action=lint" in leases[0]
    assert f"project={tmp_path}" in leases[0]
    assert "requested=1 nested=False" in leases[0]


async def test_unknown_run_id_logs_placeholders(tmp_path: Path) -> None:
    """A lease without a tracked run still grants; it just has no owner to name."""
    ws_context = _ws_context(tmp_path)
    runner = make_running_runner(working_dir_path=tmp_path)
    params = _internal_client_types.LeaseProcessBudgetParams(
        requested=1, nested=False, run_id="nope"
    )

    records = _Records()
    sink = logger.add(records)
    try:
        result = await runner_manager.lease_for_runner(ws_context, runner, params)
    finally:
        logger.remove(sink)

    assert result.lease_id
    leases = [m for m in records.messages if "Process budget lease run=nope" in m]
    assert len(leases) == 1
    assert "action=?" in leases[0]
    assert "project=?" in leases[0]


def test_no_gate_target_push_helpers_remain() -> None:
    """The WM no longer tells ERs what target to use — leases are per unit."""
    assert not hasattr(runner_client, "update_process_budget")
    assert not hasattr(ProcessBudget, "target_for_runner")


def test_run_budget_is_gone() -> None:
    """Per-dispatch budget compensation has no owner left to read it."""
    assert not hasattr(domain, "RunBudget")
    assert not hasattr(runner_manager, "resolve_lease_terms")
    assert not hasattr(prepare_envs_service, "project_fan_out_budget")
    assert "budget" not in domain.InFlightRun.__dataclass_fields__
