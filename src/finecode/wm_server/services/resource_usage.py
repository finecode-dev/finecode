"""Read-only snapshot of what the WM is doing and what it costs."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import time

from finecode.wm_server import context, host_pressure, process_footprint
from finecode.wm_server.runner import runner_counts
from finecode.wm_server.services import event_loop_lag_monitor
from finecode.wm_server.services.event_loop_lag_monitor import LagSummary

__all__ = [
    "WmProcessInfo",
    "build_snapshot",
    "read_processes",
]

_footprint_walk_running = False


@dataclasses.dataclass(frozen=True)
class WmProcessInfo:
    pid: int
    uptime_sec: float | None
    connected_clients: int
    lag: LagSummary | None


def build_snapshot(ws_context: context.WorkspaceContext, wm: WmProcessInfo) -> dict:
    now = time.time()
    counts = runner_counts.count_runners(ws_context)
    budget = ws_context.process_budget.snapshot()
    active_projects = sum(1 for runs in ws_context.in_flight_runs.values() if runs)
    total_projects = len(ws_context.ws_projects)

    meminfo = host_pressure.read_meminfo()
    cgroup = host_pressure.read_cgroup_memory()
    pressure = host_pressure.read_host_pressure()
    load = event_loop_lag_monitor.read_host_load()

    ws_context.resource_peaks.observe_host(
        swap_used_mb=meminfo.swap_used_mb,
        mem_available_mb=meminfo.mem_available_mb,
        psi_memory_full_avg10=pressure.psi_memory_full_avg10,
    )
    peaks = ws_context.resource_peaks
    memory_pressure_reasons = host_pressure.memory_pressure_reasons(
        psi_memory_full_avg10=pressure.psi_memory_full_avg10,
        mem_total_mb=meminfo.mem_total_mb,
        mem_available_mb=meminfo.mem_available_mb,
        swap_total_mb=meminfo.swap_total_mb,
        swap_used_mb=meminfo.swap_used_mb,
    )
    if memory_pressure_reasons is None:
        memory_pressure_json = None
    else:
        memory_pressure_json = {
            "active": bool(memory_pressure_reasons),
            "reasons": list(memory_pressure_reasons),
        }

    if wm.lag is None:
        loop_lag_ms = None
        loop_lag_max_ms = None
        loop_lag_pending_ms = None
        loop_lag_window_sec = None
    else:
        loop_lag_ms = wm.lag.latest_ms
        loop_lag_max_ms = wm.lag.max_recent_ms
        loop_lag_pending_ms = wm.lag.pending_ms
        loop_lag_window_sec = wm.lag.window_sec

    by_status = {status.name: counts.by_status[status] for status in counts.by_status}
    by_env = {
        env: {"running": env_counts.running, "active": env_counts.active}
        for env, env_counts in counts.by_env.items()
    }

    startup_total = ws_context.subprocess_budgets.startup_cap
    startup_used = counts.startup_slots_used
    startup_waiting = counts.startup_slots_waiting
    dump_stats = ws_context.action_meta_dump_stats

    runs: list[dict] = []
    for runs_by_id in ws_context.in_flight_runs.values():
        for run in runs_by_id.values():
            runs.append(
                {
                    "runId": run.run_id,
                    "action": run.action_name,
                    "project": str(run.project_path),
                    "startedAt": run.started_at,
                    "cancellable": run.cancellable,
                }
            )
    runs.sort(key=lambda item: item["startedAt"])

    if cgroup is None:
        cgroup_json = None
    else:
        cgroup_json = {
            "memoryMaxMb": cgroup.memory_max_mb,
            "memoryCurrentMb": cgroup.memory_current_mb,
            "swapCurrentMb": cgroup.swap_current_mb,
        }

    return {
        "timestamp": now,
        "wm": {
            "pid": wm.pid,
            "uptimeSec": wm.uptime_sec,
            "connectedClients": wm.connected_clients,
            "loopLagMs": loop_lag_ms,
            "loopLagMaxMs": loop_lag_max_ms,
            "loopLagPendingMs": loop_lag_pending_ms,
            "loopLagWindowSec": loop_lag_window_sec,
        },
        "projects": {
            "total": total_projects,
            "running": counts.projects_running,
            "active": active_projects,
        },
        "runners": {
            "byStatus": by_status,
            "running": counts.running,
            "starting": counts.starting,
            "active": counts.active,
            "byEnv": by_env,
        },
        "budget": {
            "total": ws_context.subprocess_budgets.total,
            "source": ws_context.subprocess_budgets.total_source,
        },
        "workSlots": {
            "total": budget.size,
            "used": budget.granted,
            "free": max(0, budget.size - budget.granted),
            "waiting": budget.waiting,
            "stallEscape": budget.stall_escape,
            "holders": [
                {"runner": runner_id, "slots": slots}
                for runner_id, slots in budget.holders
            ],
        },
        "startupSlots": {
            "total": startup_total,
            "used": startup_used,
            "free": max(0, startup_total - startup_used),
            "waiting": startup_waiting,
        },
        "actionMetaDumps": {
            "running": dump_stats.running,
            "waiting": dump_stats.waiting,
            "spawned": dump_stats.spawned,
            "ok": dump_stats.ok,
            "skew": dump_stats.skew,
            "envUnusable": dump_stats.env_unusable,
            "timeout": dump_stats.timeout,
            "skewFromCache": dump_stats.skew_from_cache,
        },
        "inFlightRuns": runs,
        "peaks": {
            "runnersRunning": max(peaks.runners_running, counts.running),
            "runnersStarting": max(peaks.runners_starting, counts.starting),
            "projectsActive": max(peaks.projects_active, active_projects),
            "workSlotsUsed": budget.peak_granted,
            "workSlotsWaiting": budget.peak_waiting,
            "startupSlotsWaiting": max(peaks.startup_slots_waiting, startup_waiting),
            "hostSwapUsedMb": peaks.host_swap_used_mb,
            "hostMemAvailableMinMb": peaks.host_mem_available_min_mb,
            "hostPsiMemoryFullMax": peaks.host_psi_memory_full_max,
            "hookFailed": peaks.hook_failed,
        },
        "host": {
            "memTotalMb": meminfo.mem_total_mb,
            "memAvailableMb": meminfo.mem_available_mb,
            "swapTotalMb": meminfo.swap_total_mb,
            "swapUsedMb": meminfo.swap_used_mb,
            "cgroup": cgroup_json,
            "psi": {
                "memoryFullAvg10": pressure.psi_memory_full_avg10,
                "ioFullAvg10": pressure.psi_io_full_avg10,
                "cpuSomeAvg10": pressure.psi_cpu_some_avg10,
            },
            "load1m": load.load_1m,
            "cpuCount": load.cpu_count,
            "memoryPressure": memory_pressure_json,
        },
        "processes": None,
    }


def _walk_finished(future: asyncio.Future) -> None:
    global _footprint_walk_running
    _footprint_walk_running = False
    if future.cancelled():
        return
    future.exception()


async def read_processes(ws_context: context.WorkspaceContext, wm_pid: int) -> dict:
    global _footprint_walk_running
    if _footprint_walk_running:
        return {"error": "process walk already in progress"}
    targets = [
        process_footprint.FootprintTarget(runner_id=runner_id, pid=pid)
        for runner_id, pid in runner_counts.live_runner_pids(ws_context)
    ]
    loop = asyncio.get_running_loop()
    try:
        walk = loop.run_in_executor(
            None,
            functools.partial(process_footprint.read_footprint, wm_pid, targets),
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    _footprint_walk_running = True
    walk.add_done_callback(_walk_finished)
    try:
        footprint = await asyncio.shield(walk)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return _footprint_to_json(footprint)


def _footprint_to_json(footprint: process_footprint.Footprint) -> dict:
    def _mb(kb: int) -> float:
        return kb // 1024

    def _tree_json(tree: process_footprint.TreeFootprint, extra: dict) -> dict:
        swap_mb = None if tree.swap_kb is None else _mb(tree.swap_kb)
        return {
            **extra,
            "processCount": tree.process_count,
            "rssMb": _mb(tree.rss_kb),
            "swapMb": swap_mb,
        }

    runners = [
        _tree_json(
            tree,
            {"runner": runner_id, "pid": tree.pid},
        )
        for runner_id, tree in footprint.runners.items()
    ]
    runners.sort(key=lambda row: row["rssMb"] + (row["swapMb"] or 0), reverse=True)
    untracked = [_tree_json(tree, {"pid": tree.pid}) for tree in footprint.untracked]
    untracked.sort(key=lambda row: row["pid"])
    total_rss = (
        footprint.wm.rss_kb
        + sum(tree.rss_kb for tree in footprint.runners.values())
        + sum(tree.rss_kb for tree in footprint.untracked)
    )
    swaps = [footprint.wm.swap_kb]
    swaps.extend(tree.swap_kb for tree in footprint.runners.values())
    swaps.extend(tree.swap_kb for tree in footprint.untracked)
    if any(swap is None for swap in swaps):
        total_swap = None
    else:
        total_swap = sum(swap for swap in swaps if swap is not None)
    return {
        "wm": _tree_json(footprint.wm, {"pid": footprint.wm.pid}),
        "runners": runners,
        "untracked": untracked,
        "totalRssMb": _mb(total_rss),
        "totalSwapMb": None if total_swap is None else _mb(total_swap),
    }
