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
from finecode.wm_server.runner import elicitation_bridge


@contextlib.asynccontextmanager
async def track(
    ws_context: context.WorkspaceContext,
    *,
    run_id: str,
    action_name: str,
    project_path: pathlib.Path,
    cancellable: bool = False,
) -> typing.AsyncIterator[None]:
    """Register a run for the duration of the block.

    Removal is in a ``finally``, so it covers every way a run can end —
    completion, failure, cancellation, and the runner dying underneath it. An
    entry that outlived its run would refuse every future recovery of its
    project, which is a worse failure than the one this prevents.

    ``cancellable`` marks a run the WM started on its own behalf, whose result
    is re-derivable and which no caller awaits: a config reload drops it and
    proceeds rather than refusing on it (``blocking_runs`` below, ADR-0080).
    Every other run stays untouched — ADR-0079's refusal is exactly what
    protects a *user's own* long-running action from having its runners
    replaced underneath it.
    """
    runs = ws_context.in_flight_runs.setdefault(project_path, {})
    runs[run_id] = domain.InFlightRun(
        run_id=run_id,
        action_name=action_name,
        project_path=project_path,
        started_at=time.time(),
        cancellable=cancellable,
    )
    # The same block also binds the run to the client that started it, for
    # ADR-0082's addressing. Deliberately here rather than beside each dispatch:
    # the two facts share a key and a lifetime exactly — a run that can be
    # refused a recovery is precisely a run that can still ask a question — and
    # a separate registration would be one more thing to forget at the next
    # dispatch site somebody adds.
    with elicitation_bridge.bind_run(run_id):
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

    ADR-0080's amendment lives here: a ``cancellable`` run is dropped from
    consideration *before* anything else, regardless of
    ``kill_in_flight_runs`` — it never refuses a reload on its own, because
    its caller is the WM rather than the user the refusal exists to protect,
    and cancelling it costs nothing. Only once every remaining run is one of
    those is the project treated as idle; a single user-started run alongside
    several cancellable ones still refuses, naming only the run that actually
    matters.
    """
    runs = runs_in_project(ws_context, project_path)
    if not runs:
        return []
    if kill_in_flight_runs:
        discard_project(ws_context, project_path)
        return []
    blocking = [run for run in runs if not run.cancellable]
    if not blocking:
        _discard_cancellable(ws_context, project_path)
        return []
    return blocking


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


def _discard_cancellable(
    ws_context: context.WorkspaceContext, project_path: pathlib.Path
) -> None:
    """Drop *project_path*'s remaining runs, all of them cancellable, so the
    reload proceeds and takes them with it (ADR-0080).

    Deliberately not ``discard_project`` (and not its warning): that path is
    for a caller *overriding* a refusal it was told about, which is worth a
    log line. This one is the refusal never firing in the first place —
    cancelling a re-derivable run nobody awaits is the expected, silent case,
    not an event an operator needs to see.
    """
    project_runs = ws_context.in_flight_runs.get(project_path)
    if not project_runs:
        return
    for run_id in list(project_runs):
        project_runs.pop(run_id, None)
    del ws_context.in_flight_runs[project_path]
