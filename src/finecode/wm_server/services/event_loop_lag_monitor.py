# docs: docs/guides/wm-server-internals.md
"""Periodic sampling of the WM event loop's own scheduling lag.

The WM server is a single-threaded asyncio process. When its loop is starved of
CPU — by external pressure, by the ER subprocesses it just spawned, or by a long
synchronous callback of its own — every trivial RPC it owes a client is late,
and the only evidence elsewhere is an ER-side timeout. The loop's own clock is
the one place that delay is visible from inside the WM, so this module samples
it and records the delay together with the state that makes it attributable.

Diagnostic only: it changes no scheduling, and nothing depends on it running.
See ``docs/guides/wm-server-internals.md``, "Event-loop lag monitor".

The sampling mechanics live in :mod:`finecode_common.lag`; this module is the
WM adapter: thresholds, messages, WM process state and lifecycle.
"""

from __future__ import annotations

import asyncio
import dataclasses

from finecode_common.lag import (
    EpisodeSummary,
    HostLoad,
    LagEpisodeTracker,
    LagSettings,
    ProcessUsage,
    UsageDelta,
    arm_lag_baseline,
    read_host_load,
    read_process_usage,
    ready_callbacks,
    run_lag_sampler,
)
from loguru import logger

from finecode.wm_server import context, host_pressure
from finecode.wm_server.runner import runner_counts

__all__ = [
    "LAG_WINDOW_DEFAULT_SEC",
    "LAG_WINDOW_MAX_SEC",
    "SAMPLE_INTERVAL_SEC",
    "WARN_COOLDOWN_SEC",
    "WARN_THRESHOLD_SEC",
    "EventLoopLagMonitor",
    "HostLoad",
    "LagContext",
    "LagSummary",
    "ProcessUsage",
    "read_host_load",
    "read_process_usage",
    "snapshot_context",
]

SAMPLE_INTERVAL_SEC = 0.5
"""How often the loop is sampled for lag."""

WARN_THRESHOLD_SEC = 1.0
"""Lag above this is recorded as a warning."""

WARN_COOLDOWN_SEC = 30.0
"""Minimum spacing between warnings while lag stays over the threshold."""

LAG_WINDOW_DEFAULT_SEC = 30.0
"""Default window for the recent-lag maximum."""

LAG_WINDOW_MAX_SEC = 600.0
"""Longest window a caller may ask for; bounds the sample deque."""


@dataclasses.dataclass(frozen=True)
class LagContext:
    """The WM state a lag sample is read against.

    ``runners_starting`` counts ERs mid-startup or mid-repair and
    ``runners_running`` those accepting requests; the two phases draw on
    different halves of the one combined subprocess budget (ADR-0093), so a lag
    reading beside a large starting count points at ER spawn fan-out while one
    beside a saturated ``budget_granted`` points at subprocess work.
    """

    runners_starting: int
    runners_running: int
    budget_granted: int
    budget_total: int
    in_flight_runs: int


@dataclasses.dataclass(frozen=True)
class LagSummary:
    latest_ms: int | None
    max_recent_ms: int | None
    pending_ms: int | None
    window_sec: float


def snapshot_context(ws_context: context.WorkspaceContext) -> LagContext:
    """Summarise the WM state a lag sample should be read against.

    Reads only: it never acquires the process budget's condition and never
    leases, so sampling a starved loop cannot itself wait on anything.
    """
    counts = runner_counts.count_runners(ws_context)
    budget = ws_context.process_budget.snapshot()
    return LagContext(
        runners_starting=counts.starting,
        runners_running=counts.running,
        budget_granted=budget.granted,
        budget_total=budget.size,
        in_flight_runs=sum(len(runs) for runs in ws_context.in_flight_runs.values()),
    )


def _fmt(value: object) -> str:
    return "n/a" if value is None else str(value)


def _wm_usage_fields(usage: UsageDelta, prefix: str = "") -> dict[str, int | None]:
    """Usage extras under the historical ``wm_cpu_*`` names.

    The shared delta reports ``process_cpu_*`` so the ER monitor can reuse it;
    the WM records keep the names existing dashboards and tests read.
    """
    renamed: dict[str, int | None] = {}
    for key, value in usage.fields().items():
        if key.startswith("process_cpu_"):
            key = "wm_cpu_" + key[len("process_cpu_") :]
        renamed[f"{prefix}{key}"] = value
    return renamed


class EventLoopLagMonitor:
    """Sample the event loop's lag and report it when it crosses a threshold.

    One instance per WM process, driven by :meth:`run` on the loop it measures.
    The state kept between samples is what turns a stream of readings into a
    bounded log: at most one warning per episode per cooldown, and one recovery
    record when the episode ends.
    """

    def __init__(
        self,
        *,
        interval_sec: float = SAMPLE_INTERVAL_SEC,
        threshold_sec: float = WARN_THRESHOLD_SEC,
        cooldown_sec: float = WARN_COOLDOWN_SEC,
    ) -> None:
        self._settings = LagSettings(
            interval_sec=interval_sec,
            threshold_sec=threshold_sec,
            cooldown_sec=cooldown_sec,
            window_sec=LAG_WINDOW_MAX_SEC,
        )
        self._tracker = LagEpisodeTracker(self._settings)
        self._next_due: float | None = None
        self._host_sample_failed = False

    async def run(self, ws_context: context.WorkspaceContext) -> None:
        """Sample until cancelled.

        ``next_due`` is absolute and re-armed from the observed wake time rather
        than incremented, so a long block leaves one late sample behind instead
        of a burst of immediate ones chasing the schedule that was missed.
        """
        loop = asyncio.get_running_loop()
        baseline = arm_lag_baseline(loop, self._settings)
        self._next_due = baseline.next_due

        def _on_sample(now: float, lag: float) -> None:
            self.observe(ws_context, now=now, lag=lag)
            self._sample_host(ws_context)
            self._next_due = now + self._settings.interval_sec

        try:
            await run_lag_sampler(
                self._settings, baseline=baseline, on_sample=_on_sample
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed sample must be logged, not left on the task
            # Terminal and loud: the task reference is held in a module global,
            # so an exception left sitting on it would not be reported until
            # that reference is dropped.
            logger.exception(
                "WM event loop lag monitor stopped after an unexpected error"
            )
        finally:
            self._next_due = None

    def observe(
        self,
        ws_context: context.WorkspaceContext,
        *,
        now: float,
        lag: float,
        usage: ProcessUsage | None = None,
    ) -> None:
        """Record one lag sample.

        ``now`` is the loop clock's current time and ``lag`` the delay of this
        sample behind its due time; :meth:`run` supplies both from ``asyncio``'s
        loop clock. ``usage`` is the process's counters at ``now`` and is read
        here when omitted. They are parameters so the rate-limiting and
        recovery behaviour can be exercised without waiting on a real clock.
        """
        if usage is None:
            usage = read_process_usage()
        event = self._tracker.observe(now=now, lag=lag, usage=usage)
        if event.kind == "warning":
            self._warn(ws_context, lag=lag, window=event.window)
        elif event.kind == "recovered":
            assert event.episode is not None
            self._recover(episode=event.episode)

    def lag_summary(
        self, now: float, window_sec: float = LAG_WINDOW_DEFAULT_SEC
    ) -> LagSummary:
        latest_ms, max_recent_ms = self._tracker.recent(now=now, window_sec=window_sec)
        pending_ms: int | None = None
        if self._next_due is not None:
            pending_ms = round(max(0.0, now - self._next_due) * 1000)
        return LagSummary(
            latest_ms=latest_ms,
            max_recent_ms=max_recent_ms,
            pending_ms=pending_ms,
            window_sec=window_sec,
        )

    def _sample_host(self, ws_context: context.WorkspaceContext) -> None:
        try:
            meminfo = host_pressure.read_meminfo()
            ws_context.resource_peaks.observe_host(
                swap_used_mb=meminfo.swap_used_mb,
                mem_available_mb=meminfo.mem_available_mb,
                psi_memory_full_avg10=host_pressure.read_psi_memory_full_avg10(),
            )
        except Exception as exc:  # noqa: BLE001
            if not self._host_sample_failed:
                logger.warning(
                    f"WM event loop host sampling failed ({type(exc).__name__}); "
                    "lag sampling continues"
                )
                self._host_sample_failed = True

    def _recover(self, *, episode: EpisodeSummary) -> None:
        logger.bind(
            starvation_ms=episode.starvation_ms,
            suppressed_warnings=episode.suppressed_warnings,
            lagging_samples=episode.lagging_samples,
            max_lag_ms=episode.max_lag_ms,
            **_wm_usage_fields(episode.usage, prefix="episode_"),
        ).info(
            f"WM event loop lag recovered after {episode.starvation_ms}ms:"
            f" {episode.lagging_samples} lagging samples,"
            f" max lag={episode.max_lag_ms}ms"
            f" ({episode.suppressed_warnings} warnings suppressed); over the episode"
            f" {episode.usage.describe()}"
        )

    def _warn(
        self,
        ws_context: context.WorkspaceContext,
        *,
        lag: float,
        window: UsageDelta,
    ) -> None:
        lag_context = snapshot_context(ws_context)
        host = read_host_load()
        pressure = host_pressure.read_host_pressure()
        ready = ready_callbacks()

        lag_ms = round(lag * 1000)
        threshold_ms = round(self._settings.threshold_sec * 1000)
        load_1m = None if host.load_1m is None else round(host.load_1m, 2)
        logger.bind(
            lag_ms=lag_ms,
            threshold_ms=threshold_ms,
            **_wm_usage_fields(window),
            load_1m=load_1m,
            cpu_count=host.cpu_count,
            ready_callbacks=ready,
            runners_starting=lag_context.runners_starting,
            runners_running=lag_context.runners_running,
            budget_granted=lag_context.budget_granted,
            budget_total=lag_context.budget_total,
            in_flight_runs=lag_context.in_flight_runs,
            **pressure.fields(),
        ).warning(
            f"WM event loop lag exceeded threshold: lag={lag_ms}ms"
            f" (threshold {threshold_ms}ms);"
            f" {window.describe()};"
            f" host load 1m={_fmt(load_1m)} on {_fmt(host.cpu_count)} CPUs;"
            f" ready callbacks={_fmt(ready)};"
            f" runners starting={lag_context.runners_starting}"
            f" running={lag_context.runners_running};"
            f" process budget {lag_context.budget_granted}/{lag_context.budget_total};"
            f" in-flight runs={lag_context.in_flight_runs};"
            f" {pressure.describe()}"
        )
