"""The register of action runs that have been dispatched and not yet finished.

Recovery replaces a project's runners, which kills whatever those runners are
executing — leaving the caller of the killed run with a transport failure it
cannot attribute, and, for an action with side effects, no way to know whether
they happened. Recovery refuses instead, and this is what it consults
(ADR-0079).

Kept in memory and always maintained: the WAL records the same lifecycle, but it
is write-only and disabled by default, and a correctness mechanism cannot depend
on a diagnostic feature being switched on.
"""

from __future__ import annotations

import contextlib
import pathlib
import time
import typing

from loguru import logger

from finecode.wm_server import context, domain


@contextlib.asynccontextmanager
async def track(
    ws_context: context.WorkspaceContext,
    *,
    run_id: str,
    action_name: str,
    project_path: pathlib.Path,
) -> typing.AsyncIterator[None]:
    """Register a run for the duration of the block.

    Removal is in a ``finally``, so it covers every way a run can end —
    completion, failure, cancellation, and the runner dying underneath it. An
    entry that outlived its run would refuse every future recovery of its
    project, which is a worse failure than the one this prevents.
    """
    runs = ws_context.in_flight_runs.setdefault(project_path, {})
    runs[run_id] = domain.InFlightRun(
        run_id=run_id,
        action_name=action_name,
        project_path=project_path,
        started_at=time.time(),
    )
    try:
        yield
    finally:
        project_runs = ws_context.in_flight_runs.get(project_path)
        if project_runs is not None:
            project_runs.pop(run_id, None)
            if not project_runs:
                del ws_context.in_flight_runs[project_path]


def runs_in_project(
    ws_context: context.WorkspaceContext, project_path: pathlib.Path
) -> list[domain.InFlightRun]:
    return list(ws_context.in_flight_runs.get(project_path, {}).values())


def as_json(runs: list[domain.InFlightRun]) -> list[dict]:
    """The runs a refusal is naming, in the shape clients see them."""
    return [
        {"runId": run.run_id, "action": run.action_name, "startedAt": run.started_at}
        for run in runs
    ]


def describe(runs: list[domain.InFlightRun]) -> str:
    """A refusal message that names what it is waiting on.

    "Busy" supports none of the three things a caller can do about it: retry,
    target a different project, or override.
    """
    return ", ".join(f"{run.action_name} (run {run.run_id})" for run in runs)


def blocking_runs(
    ws_context: context.WorkspaceContext,
    project_path: pathlib.Path,
    *,
    kill_in_flight_runs: bool,
) -> list[domain.InFlightRun]:
    """The runs that stand in the way of replacing this project's runners.

    Empty when the project is idle, and empty when the caller has explicitly
    accepted killing what is running — the override exists because a hung run is
    indistinguishable from a working one, and restarting its runner is the usual
    remedy (ADR-0079 rule 5).
    """
    runs = runs_in_project(ws_context, project_path)
    if not runs:
        return []
    if kill_in_flight_runs:
        discard_project(ws_context, project_path)
        return []
    return runs


def refusal_message(project_path: pathlib.Path, runs: list[domain.InFlightRun]) -> str:
    return (
        f"Recovery of {project_path} would kill {len(runs)} run(s) in flight: "
        f"{describe(runs)}. Retry once they finish, recover a different project, "
        f"or pass killInFlightRuns=true to replace the runners anyway."
    )


def discard_project(
    ws_context: context.WorkspaceContext, project_path: pathlib.Path
) -> None:
    """Forget every run of a project whose runners are being replaced anyway.

    Called only when a caller has overridden the refusal: those runs are about
    to be killed by the replacement, and their own ``finally`` may not run —
    the ER process carrying them is gone.
    """
    dropped = ws_context.in_flight_runs.pop(project_path, {})
    if dropped:
        logger.warning(
            f"Recovery of {project_path} is killing {len(dropped)} in-flight run(s): "
            f"{describe(list(dropped.values()))}"
        )
