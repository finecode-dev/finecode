"""Periodic resource-usage reporter."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import time
import typing

import click

from finecode.wm_client import ApiError, ApiMethodNotFoundError, ApiServerError

__all__ = [
    "DEFAULT_INTERVAL_SEC",
    "ENV_VAR",
    "LAG_WINDOW_DEFAULT_SEC",
    "MAX_INTERVAL_SEC",
    "SUMMARY_TIMEOUT_SEC",
    "InvalidResourceUsageInterval",
    "ReporterState",
    "format_line",
    "format_peaks",
    "format_table",
    "lag_window_for",
    "parse_interval",
    "periodic",
    "resolve_interval",
]

DEFAULT_INTERVAL_SEC = 15.0
MAX_INTERVAL_SEC = 600.0
LAG_WINDOW_DEFAULT_SEC = 30.0
ENV_VAR = "FINECODE_RESOURCE_USAGE_INTERVAL"
SUMMARY_TIMEOUT_SEC = 5.0


class InvalidResourceUsageInterval(Exception):
    pass


def parse_interval(
    value: str | float, *, source: str, zero_disables: bool
) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidResourceUsageInterval(
            f"{source}: invalid interval {value!r} — expected a number of seconds "
            f"in (0, {MAX_INTERVAL_SEC}]"
        ) from exc
    if not math.isfinite(parsed):
        raise InvalidResourceUsageInterval(
            f"{source}: invalid interval {value!r} — expected a finite number of "
            f"seconds in (0, {MAX_INTERVAL_SEC}]"
        )
    if parsed == 0:
        if zero_disables:
            return None
        raise InvalidResourceUsageInterval(
            f"{source}: invalid interval 0 — expected seconds in (0, "
            f"{MAX_INTERVAL_SEC}]"
        )
    if parsed < 0 or parsed > MAX_INTERVAL_SEC:
        raise InvalidResourceUsageInterval(
            f"{source}: invalid interval {value!r} — expected seconds in (0, "
            f"{MAX_INTERVAL_SEC}]"
        )
    return parsed


def lag_window_for(interval_sec: float) -> float:
    return max(LAG_WINDOW_DEFAULT_SEC, interval_sec)


def resolve_interval(
    *,
    flag_value: str | float | None,
    disabled: bool,
    environ: typing.Mapping[str, str],
) -> float | None:
    if flag_value is not None and disabled:
        raise InvalidResourceUsageInterval(
            "--resource-usage and --no-resource-usage are mutually exclusive"
        )
    if disabled:
        return None
    if flag_value is not None:
        return parse_interval(flag_value, source="--resource-usage", zero_disables=True)
    env_value = environ.get(ENV_VAR)
    if env_value:
        return parse_interval(env_value, source=ENV_VAR, zero_disables=True)
    if environ.get("CI"):
        return DEFAULT_INTERVAL_SEC
    return None


@dataclasses.dataclass
class ReporterState:
    stopped: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    stop_reason: (
        typing.Literal["unsupported", "disconnected", "failed", "output_closed"] | None
    ) = None
    error: BaseException | None = None


def _gb(mb: float | None) -> str:
    if mb is None:
        return "n/a"
    return f"{mb / 1024:.1f}"


def _fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "n/a"
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.1f}s"


def _fmt_uptime(sec: float | None) -> str:
    if sec is None:
        return "n/a"
    total = int(sec)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _fmt_age(sec: float | None) -> str:
    if sec is None:
        return "n/a"
    total = int(sec)
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_line(snapshot: dict, elapsed: float) -> str:
    projects = snapshot.get("projects", {})
    runners = snapshot.get("runners", {})
    work = snapshot.get("workSlots", {})
    startup = snapshot.get("startupSlots", {})
    host = snapshot.get("host", {})
    wm = snapshot.get("wm", {})
    total = projects.get("total")
    running = projects.get("running")
    active = projects.get("active")
    er_running = runners.get("running")
    er_active = runners.get("active")
    er_starting = runners.get("starting")
    work_used = work.get("used")
    work_total = work.get("total")
    work_waiting = work.get("waiting")
    escape = work.get("stallEscape")
    startup_used = startup.get("used")
    startup_total = startup.get("total")
    startup_waiting = startup.get("waiting")

    cgroup = host.get("cgroup")
    if cgroup is not None and cgroup.get("memoryMaxMb") is not None:
        mem_used = cgroup.get("memoryCurrentMb")
        mem_limit = cgroup.get("memoryMaxMb")
        mem_tag = " (cgroup)"
        if mem_used is None or mem_limit is None:
            mem_part = "n/a"
        else:
            mem_part = f"{_gb(mem_used)}G/{_gb(mem_limit)}G{mem_tag}"
    else:
        mem_total = host.get("memTotalMb")
        mem_available = host.get("memAvailableMb")
        if mem_total is None or mem_available is None:
            mem_part = "n/a"
        else:
            mem_part = f"{_gb(mem_total - mem_available)}G/{_gb(mem_total)}G"
    swap_used = host.get("swapUsedMb")
    swap_total = host.get("swapTotalMb")
    if swap_used is None or swap_total is None:
        swap_part = "n/a"
    else:
        swap_part = f"{_gb(swap_used)}G/{_gb(swap_total)}G"
    lag_max = wm.get("loopLagMaxMs")
    lag_part = "n/a" if lag_max is None else f"{lag_max / 1000:.1f}s"

    def _n(value) -> str:
        return "n/a" if value is None else str(value)

    work_escape = ", escape" if escape else ""
    return (
        f"[resources] t=+{int(elapsed)}s projects {_n(active)} act/{_n(running)} "
        f"run/{_n(total)} · ER {_n(er_running)} run/{_n(er_active)} act/"
        f"{_n(er_starting)} start · work {_n(work_used)}/{_n(work_total)} "
        f"(+{_n(work_waiting)} wait{work_escape}) · startup "
        f"{_n(startup_used)}/{_n(startup_total)} (+{_n(startup_waiting)} wait) · "
        f"mem {mem_part} swap {swap_part} · lag {lag_part}"
    )


def format_peaks(snapshot: dict) -> str:
    peaks = snapshot.get("peaks", {})
    wm = snapshot.get("wm", {})
    return (
        f"[resources] peaks: ER {_n(peaks.get('runnersRunning'))} run/"
        f"{_n(peaks.get('runnersStarting'))} start · projects "
        f"{_n(peaks.get('projectsActive'))} act · work "
        f"{_n(peaks.get('workSlotsUsed'))} used/{_n(peaks.get('workSlotsWaiting'))} "
        f"wait · startup {_n(peaks.get('startupSlotsWaiting'))} wait · swap max "
        f"{_gb(peaks.get('hostSwapUsedMb'))}G · mem avail min "
        f"{_gb(peaks.get('hostMemAvailableMinMb'))}G · WM pid {_n(wm.get('pid'))} "
        f"up {_fmt_uptime(wm.get('uptimeSec'))}"
    )


def _n(value) -> str:
    return "n/a" if value is None else str(value)


def format_table(info: dict, snapshot: dict) -> str:
    wm = snapshot.get("wm", {})
    projects = snapshot.get("projects", {})
    runners = snapshot.get("runners", {})
    work = snapshot.get("workSlots", {})
    startup = snapshot.get("startupSlots", {})
    budget = snapshot.get("budget", {})
    flights = snapshot.get("inFlightRuns", [])
    host = snapshot.get("host", {})
    peaks = snapshot.get("peaks", {})
    processes = snapshot.get("processes")

    version = info.get("version", "n/a")
    info_clients = info.get("clients", [])
    client_count = wm.get("connectedClients")
    lag_ms = wm.get("loopLagMs")
    lag_max = wm.get("loopLagMaxMs")
    lag_window = wm.get("loopLagWindowSec")
    lag_pending = wm.get("loopLagPendingMs")
    lines = []
    lines.append(
        f"WM pid {_n(wm.get('pid'))} · version {version} · up "
        f"{_fmt_uptime(wm.get('uptimeSec'))} · clients {_n(client_count)} "
        f"({', '.join(info_clients)}) · lag {_fmt_ms(lag_ms)} "
        f"(max {_fmt_ms(lag_max)} / {_n(lag_window)}s, pending {_fmt_ms(lag_pending)})"
    )
    lines.append(
        f"projects   {_n(projects.get('total'))} total · "
        f"{_n(projects.get('running'))} running · {_n(projects.get('active'))} active"
    )
    by_status = runners.get("byStatus", {})
    bracket_parts = [
        f"{name} {count}"
        for name, count in by_status.items()
        if name != "RUNNING" and count
    ]
    bracket = f"   [{(' · '.join(bracket_parts))}]" if bracket_parts else ""
    lines.append(
        f"runners    {_n(runners.get('running'))} running · "
        f"{_n(runners.get('starting'))} starting · {_n(runners.get('active'))} active"
        f"{bracket}"
    )
    by_env = runners.get("byEnv", {})
    env_parts = [
        f"{env} {counts.get('running')} run/{counts.get('active')} act"
        for env, counts in sorted(by_env.items())
    ]
    lines.append(f"by env     {(' · '.join(env_parts)) if env_parts else 'n/a'}")
    holders = work.get("holders", [])
    holder_parts = [
        f"{holder.get('runner')}→{holder.get('slots')}" for holder in holders
    ]
    escape_part = " · stall escape" if work.get("stallEscape") else ""
    holder_part = f"   holders: {', '.join(holder_parts)}" if holder_parts else ""
    lines.append(
        f"work       {_n(work.get('used'))}/{_n(work.get('total'))} used · "
        f"{_n(work.get('free'))} free · {_n(work.get('waiting'))} waiting"
        f"{escape_part}{holder_part}"
    )
    lines.append(
        f"startup    {_n(startup.get('used'))}/{_n(startup.get('total'))} used · "
        f"{_n(startup.get('free'))} free · {_n(startup.get('waiting'))} waiting"
    )
    lines.append(
        f"budget     {_n(budget.get('total'))} total ({budget.get('source', 'n/a')})"
    )
    timestamp = snapshot.get("timestamp", 0)
    shown = flights[:10]
    flight_parts = [
        f"{run.get('action')} {run.get('project')} "
        f"{_fmt_age(timestamp - run.get('startedAt', timestamp))}"
        for run in shown
    ]
    more = f" (+{len(flights) - 10} more)" if len(flights) > 10 else ""
    lines.append(
        f"in flight  {len(flights)} runs: "
        f"{(' · '.join(flight_parts)) if flight_parts else 'n/a'}{more}"
    )
    cgroup = host.get("cgroup")
    if cgroup is None:
        cgroup_part = "n/a"
    else:
        swap_current = cgroup.get("swapCurrentMb")
        swap_str = f" (swap {_gb(swap_current)}G)" if swap_current is not None else ""
        cgroup_part = (
            f"{_gb(cgroup.get('memoryCurrentMb'))}G / "
            f"{_gb(cgroup.get('memoryMaxMb'))}G{swap_str}"
        )
    psi = host.get("psi", {})
    lines.append(
        f"host       mem {_gb(host.get('memAvailableMb'))} avail / "
        f"{_gb(host.get('memTotalMb'))} · cgroup {cgroup_part} · swap "
        f"{_gb(host.get('swapUsedMb'))} / {_gb(host.get('swapTotalMb'))} · PSI mem "
        f"{_n(psi.get('memoryFullAvg10'))} io {_n(psi.get('ioFullAvg10'))} cpu "
        f"{_n(psi.get('cpuSomeAvg10'))} · load {_n(host.get('load1m'))} / "
        f"{_n(host.get('cpuCount'))} CPUs"
    )
    hook = " · hooks failed" if peaks.get("hookFailed") else ""
    lines.append(
        f"peaks      ER {_n(peaks.get('runnersRunning'))} run / "
        f"{_n(peaks.get('runnersStarting'))} start · projects "
        f"{_n(peaks.get('projectsActive'))} active · work "
        f"{_n(peaks.get('workSlotsUsed'))} used / "
        f"{_n(peaks.get('workSlotsWaiting'))} wait · startup "
        f"{_n(peaks.get('startupSlotsWaiting'))} wait · swap max "
        f"{_gb(peaks.get('hostSwapUsedMb'))}G · mem avail min "
        f"{_gb(peaks.get('hostMemAvailableMinMb'))}G{hook}"
    )
    if processes is not None and not isinstance(processes, dict):
        pass
    if isinstance(processes, dict) and "error" not in processes:
        total_rss = processes.get("totalRssMb")
        total_swap = processes.get("totalSwapMb")
        wm_proc = processes.get("wm", {})
        untracked = processes.get("untracked", [])
        top_runners = (processes.get("runners", []) or [])[:10]
        top_parts = [
            f"{row.get('runner')} {_gb(row.get('rssMb'))}G "
            f"({row.get('processCount')} procs)"
            for row in top_runners
        ]
        lines.append(
            f"processes  total {_gb(total_rss)}G rss + {_gb(total_swap)}G swap · "
            f"WM {_gb(wm_proc.get('rssMb'))}G · untracked {len(untracked)} · "
            f"top runners: {(', '.join(top_parts)) if top_parts else 'n/a'}"
        )
    return "\n".join(lines)


def _emit_stderr(line: str) -> None:
    try:
        isatty = click.get_text_stream("stderr").isatty()
    except Exception:
        isatty = False
    prefix = "\r\033[K" if isatty else ""
    click.echo(f"{prefix}{line}", err=True)


def _safe_status(line: str, emit_status: typing.Callable[[str], None]) -> None:
    try:
        emit_status(line)
    except BrokenPipeError:
        raise
    except Exception:
        pass


async def _reap_poll(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
        await asyncio.wait({task})
    if task.cancelled():
        return
    task.exception()


async def _run_loop(
    state: ReporterState,
    poll: typing.Callable[[], typing.Coroutine[typing.Any, typing.Any, dict]],
    interval_sec: float,
    *,
    emit: typing.Callable[[str], None],
    emit_status: typing.Callable[[str], None],
    render: typing.Callable[[dict, float], str],
    start: float,
    clock: typing.Callable[[], float],
    paused: asyncio.Event | None,
    stop_on_disconnect: bool,
) -> None:
    def _emit_line(line: str) -> None:
        if paused is not None and not paused.is_set():
            return
        emit(line)

    def _emit_status_line(line: str) -> None:
        if paused is not None and not paused.is_set():
            return
        emit_status(line)

    next_tick = start + interval_sec
    poll_task: asyncio.Task | None = None
    poll_started: float = 0.0
    try:
        while True:
            if poll_task is None:
                delay = next_tick - clock()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    poll_task = asyncio.create_task(poll())
                    poll_started = clock()
                    next_tick = max(next_tick, poll_started) + interval_sec
                except BrokenPipeError:
                    raise
                except Exception as exc:
                    _safe_status(
                        f"[resources] reporter error: {type(exc).__name__}: {exc}",
                        emit_status,
                    )
                    next_tick += interval_sec
                    poll_task = None
                    continue
            timeout = next_tick - clock()
            timeout = max(timeout, 0)
            done, _pending = await asyncio.wait({poll_task}, timeout=timeout)
            now = clock()
            if not done:
                try:
                    _emit_status_line(
                        f"[resources] t=+{int(now - start)}s no answer for "
                        f"{now - poll_started:.2f}s"
                    )
                except BrokenPipeError:
                    raise
                except Exception as exc:
                    _safe_status(
                        f"[resources] reporter error: {type(exc).__name__}: {exc}",
                        emit_status,
                    )
                next_tick += interval_sec
                continue
            try:
                snapshot = poll_task.result()
            except ApiMethodNotFoundError:
                try:
                    _emit_status_line("[resources] not supported by this server")
                except BrokenPipeError:
                    raise
                except Exception as inner:
                    _safe_status(
                        f"[resources] reporter error: {type(inner).__name__}: {inner}",
                        emit_status,
                    )
                state.stop_reason = "unsupported"
                state.stopped.set()
                return
            except ApiServerError as exc:
                try:
                    _emit_status_line(
                        f"[resources] t=+{int(now - start)}s poll failed: {exc}"
                    )
                except BrokenPipeError:
                    raise
                except Exception as inner:
                    _safe_status(
                        f"[resources] reporter error: {type(inner).__name__}: {inner}",
                        emit_status,
                    )
                poll_task = None
                continue
            except (ConnectionError, RuntimeError) as exc:
                try:
                    _emit_status_line(
                        f"[resources] t=+{int(now - start)}s no connection: {exc}"
                    )
                except BrokenPipeError:
                    raise
                except Exception as inner:
                    _safe_status(
                        f"[resources] reporter error: {type(inner).__name__}: {inner}",
                        emit_status,
                    )
                if stop_on_disconnect:
                    state.stop_reason = "disconnected"
                    state.stopped.set()
                    return
                poll_task = None
                continue
            except ApiError as exc:
                try:
                    _emit_status_line(
                        f"[resources] t=+{int(now - start)}s poll failed: {exc}"
                    )
                except BrokenPipeError:
                    raise
                except Exception as inner:
                    _safe_status(
                        f"[resources] reporter error: {type(inner).__name__}: {inner}",
                        emit_status,
                    )
                poll_task = None
                continue
            except BrokenPipeError:
                raise
            except Exception as exc:
                _safe_status(
                    f"[resources] reporter error: {type(exc).__name__}: {exc}",
                    emit_status,
                )
                poll_task = None
                continue
            finally:
                if poll_task is not None and poll_task.done():
                    pass
            poll_task = None
            try:
                try:
                    line = render(snapshot, now - start)
                except Exception as exc:
                    _safe_status(
                        f"[resources] reporter error: {type(exc).__name__}: {exc}",
                        emit_status,
                    )
                else:
                    _emit_line(line)
            except BrokenPipeError:
                raise
            except Exception as exc:
                _safe_status(
                    f"[resources] reporter error: {type(exc).__name__}: {exc}",
                    emit_status,
                )
    finally:
        if poll_task is not None:
            await _reap_poll(poll_task)


@contextlib.asynccontextmanager
async def periodic(
    poll: typing.Callable[[], typing.Coroutine[typing.Any, typing.Any, dict]],
    interval_sec: float | None,
    *,
    emit: typing.Callable[[str], None] = _emit_stderr,
    emit_status: typing.Callable[[str], None] | None = None,
    render: typing.Callable[[dict, float], str] = format_line,
    summary: bool = True,
    stop_on_disconnect: bool = False,
    summary_timeout_sec: float = SUMMARY_TIMEOUT_SEC,
    clock: typing.Callable[[], float] = time.monotonic,
    paused: asyncio.Event | None = None,
) -> typing.AsyncGenerator[ReporterState, None]:
    if emit_status is None:
        emit_status = emit
    state = ReporterState()
    if interval_sec is None:
        yield state
        return
    if (
        not isinstance(interval_sec, (int, float))
        or isinstance(interval_sec, bool)
        or not math.isfinite(interval_sec)
        or interval_sec <= 0
    ):
        raise ValueError(
            f"invalid reporter interval {interval_sec!r} — expected seconds in "
            f"(0, {MAX_INTERVAL_SEC}]"
        )
    start = clock()

    async def _loop_wrapper() -> None:
        await _run_loop(
            state,
            poll,
            interval_sec,
            emit=emit,
            emit_status=emit_status,
            render=render,
            start=start,
            clock=clock,
            paused=paused,
            stop_on_disconnect=stop_on_disconnect,
        )

    loop_task: asyncio.Task = asyncio.create_task(_loop_wrapper())

    def _done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        if isinstance(exc, BrokenPipeError):
            state.stop_reason = "output_closed"
            state.stopped.set()
            return
        try:
            _safe_status(
                f"[resources] reporter error: {type(exc).__name__}: {exc}",
                emit_status,
            )
        except BrokenPipeError:
            state.stop_reason = "output_closed"
            state.stopped.set()
            return
        state.stop_reason = "failed"
        state.error = exc
        state.stopped.set()

    loop_task.add_done_callback(_done)

    async def _emit_summary() -> None:
        try:
            snapshot = await asyncio.wait_for(poll(), summary_timeout_sec)
        except (asyncio.CancelledError, KeyboardInterrupt):
            return
        except Exception as exc:
            try:
                if paused is None or paused.is_set():
                    emit_status(
                        f"[resources] peaks unavailable: {type(exc).__name__}: {exc}"
                    )
            except Exception:
                pass
            return
        try:
            try:
                line = format_peaks(snapshot)
            except Exception as exc:
                _safe_status(
                    f"[resources] reporter error: {type(exc).__name__}: {exc}",
                    emit_status,
                )
            else:
                if paused is None or paused.is_set():
                    try:
                        emit_status(line)
                    except Exception:
                        _safe_status(
                            "[resources] reporter error: emit failed",
                            emit_status,
                        )
        except Exception as exc:
            _safe_status(
                f"[resources] reporter error: {type(exc).__name__}: {exc}",
                emit_status,
            )

    try:
        yield state
    except (asyncio.CancelledError, KeyboardInterrupt):
        loop_task.cancel()
        await asyncio.wait({loop_task})
        raise
    except BaseException:
        loop_task.cancel()
        await asyncio.wait({loop_task})
        if summary and state.stop_reason is None:
            await _emit_summary()
        raise
    else:
        loop_task.cancel()
        await asyncio.wait({loop_task})
        if summary and state.stop_reason is None:
            await _emit_summary()
