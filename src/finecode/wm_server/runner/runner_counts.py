"""The one runner-counting implementation and runner-status filter."""

from __future__ import annotations

import dataclasses

from loguru import logger

from finecode.wm_server import context, domain

__all__ = [
    "EnvCounts",
    "RunnerCounts",
    "count_runners",
    "live_runner_pids",
    "record_runner_peaks",
]


@dataclasses.dataclass(frozen=True)
class EnvCounts:
    running: int
    active: int


@dataclasses.dataclass(frozen=True)
class RunnerCounts:
    by_status: dict[domain.ExtensionRunnerStatus, int]
    running: int
    starting: int
    active: int
    startup_slots_used: int
    startup_slots_waiting: int
    projects_running: int
    by_env: dict[str, EnvCounts]


def count_runners(ws_context: context.WorkspaceContext) -> RunnerCounts:
    by_status: dict[domain.ExtensionRunnerStatus, int] = dict.fromkeys(
        domain.ExtensionRunnerStatus, 0
    )
    running = 0
    starting = 0
    active = 0
    startup_slots_used = 0
    startup_slots_waiting = 0
    projects_running = 0
    env_running: dict[str, int] = {}
    env_active: dict[str, int] = {}

    for runners_by_env in ws_context.ws_projects_extension_runners.values():
        project_has_running = False
        for env_name, runner in runners_by_env.items():
            by_status[runner.status] += 1
            if runner.status is domain.ExtensionRunnerStatus.RUNNING:
                running += 1
                project_has_running = True
                env_running[env_name] = env_running.get(env_name, 0) + 1
            elif runner.status in (
                domain.ExtensionRunnerStatus.INITIALIZING,
                domain.ExtensionRunnerStatus.REPAIRING,
            ):
                starting += 1
            if runner.active_requests > 0:
                active += 1
                env_active[env_name] = env_active.get(env_name, 0) + 1
            if runner.startup_slot_release is not None:
                startup_slots_used += 1
            if runner.awaiting_startup_slot:
                startup_slots_waiting += 1
        if project_has_running:
            projects_running += 1

    by_env: dict[str, EnvCounts] = {}
    for env_name in set(env_running) | set(env_active):
        env_run = env_running.get(env_name, 0)
        env_act = env_active.get(env_name, 0)
        if env_run > 0 or env_act > 0:
            by_env[env_name] = EnvCounts(running=env_run, active=env_act)

    dump_stats = ws_context.action_meta_dump_stats
    startup_slots_used += dump_stats.running
    startup_slots_waiting += dump_stats.waiting

    return RunnerCounts(
        by_status=by_status,
        running=running,
        starting=starting,
        active=active,
        startup_slots_used=startup_slots_used,
        startup_slots_waiting=startup_slots_waiting,
        projects_running=projects_running,
        by_env=by_env,
    )


def live_runner_pids(
    ws_context: context.WorkspaceContext,
) -> list[tuple[str, int]]:
    targets: list[tuple[str, int]] = []
    for runners_by_env in ws_context.ws_projects_extension_runners.values():
        for runner in runners_by_env.values():
            if runner.status not in (
                domain.ExtensionRunnerStatus.INITIALIZING,
                domain.ExtensionRunnerStatus.RUNNING,
                domain.ExtensionRunnerStatus.REPAIRING,
            ):
                continue
            pid = runner.client.pid if runner.client is not None else None
            if pid is None:
                continue
            targets.append((runner.readable_id, pid))
    return targets


def record_runner_peaks(ws_context: context.WorkspaceContext) -> None:
    try:
        counts = count_runners(ws_context)
        ws_context.resource_peaks.raise_runner_counts(
            running=counts.running,
            starting=counts.starting,
            startup_slots_waiting=counts.startup_slots_waiting,
        )
    except Exception:
        if not ws_context.resource_peaks.hook_failed:
            logger.exception("resource peak hook failed")
            ws_context.resource_peaks.hook_failed = True
