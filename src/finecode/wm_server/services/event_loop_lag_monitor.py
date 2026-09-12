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
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time

from loguru import logger

from finecode.wm_server import context, domain

try:
    import resource
except ImportError:  # not available on Windows
    resource = None  # type: ignore[assignment]

__all__ = [
    "SAMPLE_INTERVAL_SEC",
    "WARN_COOLDOWN_SEC",
    "WARN_THRESHOLD_SEC",
    "EventLoopLagMonitor",
    "HostLoad",
    "LagContext",
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
class ProcessUsage:
    """Cumulative resource counters of the WM process at one instant.

    Only differences between two readings mean anything. CPU time covers every
    thread of the WM, not only the loop thread. The context-switch counters are
    ``None`` where ``getrusage`` is unavailable.
    """

    cpu_sec: float
    voluntary_switches: int | None
    involuntary_switches: int | None


@dataclasses.dataclass(frozen=True)
class HostLoad:
    """How contended the machine was when a warning was taken.

    ``load_1m`` is ``None`` where the OS has no load average.
    """

    load_1m: float | None
    cpu_count: int | None


def read_process_usage() -> ProcessUsage:
    if resource is None:
        return ProcessUsage(
            cpu_sec=time.process_time(),
            voluntary_switches=None,
            involuntary_switches=None,
        )
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return ProcessUsage(
        cpu_sec=time.process_time(),
        voluntary_switches=usage.ru_nvcsw,
        involuntary_switches=usage.ru_nivcsw,
    )


def read_host_load() -> HostLoad:
    try:
        load_1m: float | None = os.getloadavg()[0]
    except (AttributeError, OSError):
        load_1m = None
    try:
        cpu_count: int | None = len(os.sched_getaffinity(0))
    except AttributeError:
        cpu_count = os.cpu_count()
    return HostLoad(load_1m=load_1m, cpu_count=cpu_count)


def _ready_callbacks() -> int | None:
    """Callbacks queued to run on the current loop, if the loop exposes them.

    Many short callbacks queued at once add up to lag without any single one
    being long, which this count tells apart from one long callback. It reads
    a private attribute of the stdlib loop, so other loops report ``None``.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    ready = getattr(loop, "_ready", None)
    return len(ready) if ready is not None else None


def snapshot_context(ws_context: context.WorkspaceContext) -> LagContext:
    """Summarise the WM state a lag sample should be read against.

    Reads only: it never acquires the process budget's condition and never
    leases, so sampling a starved loop cannot itself wait on anything.
    """
    runners_starting = 0
    runners_running = 0
    for runners_by_env in ws_context.ws_projects_extension_runners.values():
        for runner in runners_by_env.values():
            if runner.status in (
                domain.ExtensionRunnerStatus.INITIALIZING,
                domain.ExtensionRunnerStatus.REPAIRING,
            ):
                runners_starting += 1
            elif runner.status == domain.ExtensionRunnerStatus.RUNNING:
                runners_running += 1

    budget = ws_context.process_budget
    return LagContext(
        runners_starting=runners_starting,
        runners_running=runners_running,
        budget_granted=budget.granted,
        budget_total=budget.size,
        in_flight_runs=sum(len(runs) for runs in ws_context.in_flight_runs.values()),
    )


def _delta(current: int | None, previous: int | None) -> int | None:
    if current is None or previous is None:
        return None
    return current - previous


def _fmt(value: object) -> str:
    return "n/a" if value is None else str(value)


@dataclasses.dataclass(frozen=True)
class _UsageDelta:
    """What the WM process consumed between two samples."""

    window_ms: int | None = None
    wm_cpu_ms: int | None = None
    wm_cpu_pct: int | None = None
    voluntary_switches: int | None = None
    involuntary_switches: int | None = None

    @classmethod
    def between(
        cls,
        start_now: float | None,
        start_usage: ProcessUsage | None,
        now: float,
        usage: ProcessUsage,
    ) -> _UsageDelta:
        if start_now is None or start_usage is None:
            return cls()
        window_ms = round((now - start_now) * 1000)
        wm_cpu_ms = round((usage.cpu_sec - start_usage.cpu_sec) * 1000)
        return cls(
            window_ms=window_ms,
            wm_cpu_ms=wm_cpu_ms,
            wm_cpu_pct=round(wm_cpu_ms * 100 / window_ms) if window_ms > 0 else None,
            voluntary_switches=_delta(
                usage.voluntary_switches, start_usage.voluntary_switches
            ),
            involuntary_switches=_delta(
                usage.involuntary_switches, start_usage.involuntary_switches
            ),
        )

    def fields(self, prefix: str = "") -> dict[str, int | None]:
        return {
            f"{prefix}{field.name}": getattr(self, field.name)
            for field in dataclasses.fields(self)
        }

    def describe(self) -> str:
        return (
            f"WM CPU {_fmt(self.wm_cpu_ms)}ms over {_fmt(self.window_ms)}ms window"
            f" ({_fmt(self.wm_cpu_pct)}%),"
            f" context switches involuntary=+{_fmt(self.involuntary_switches)}"
            f" voluntary=+{_fmt(self.voluntary_switches)}"
        )


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
        self._interval_sec = interval_sec
        self._threshold_sec = threshold_sec
        self._cooldown_sec = cooldown_sec
        # Set when the episode's first warning is emitted, cleared when lag drops
        # back below the threshold: None means no episode is open.
        self._episode_started_at: float | None = None
        self._last_warn_at: float | None = None
        # Samples suppressed since the episode opened, reported once when it ends.
        self._suppressed: int = 0
        # Aggregates over the whole episode, reported once when it ends: the
        # warnings show only the samples the cooldown let through, and the
        # suppressed ones are often the worst.
        self._episode_samples: int = 0
        self._episode_max_lag: float = 0.0
        self._episode_start_now: float | None = None
        self._episode_start_usage: ProcessUsage | None = None
        # The previous sample, so a warning can report what the WM process
        # consumed over the window that ended late.
        self._prev_now: float | None = None
        self._prev_usage: ProcessUsage | None = None

    async def run(self, ws_context: context.WorkspaceContext) -> None:
        """Sample until cancelled.

        ``next_due`` is absolute and re-armed from the observed wake time rather
        than incremented, so a long block leaves one late sample behind instead
        of a burst of immediate ones chasing the schedule that was missed.
        """
        loop = asyncio.get_running_loop()
        next_due = loop.time() + self._interval_sec
        self._prev_now = loop.time()
        self._prev_usage = read_process_usage()
        try:
            while True:
                await asyncio.sleep(max(0.0, next_due - loop.time()))
                now = loop.time()
                self.observe(ws_context, now=now, lag=now - next_due)
                next_due = now + self._interval_sec
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed sample must be logged, not left on the task
            # Terminal and loud: the task reference is held in a module global,
            # so an exception left sitting on it would not be reported until
            # that reference is dropped.
            logger.exception(
                "WM event loop lag monitor stopped after an unexpected error"
            )

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
        prev_now, prev_usage = self._prev_now, self._prev_usage
        self._prev_now, self._prev_usage = now, usage

        if lag < self._threshold_sec:
            if self._episode_started_at is None:
                return
            self._recover(now=now, usage=usage)
            return

        if self._episode_started_at is None:
            # The episode's window starts where its first late sample's window
            # did, so its totals cover the whole stall.
            self._episode_start_now = prev_now
            self._episode_start_usage = prev_usage
        self._episode_samples += 1
        self._episode_max_lag = max(self._episode_max_lag, lag)

        if (
            self._last_warn_at is not None
            and now - self._last_warn_at < self._cooldown_sec
        ):
            self._suppressed += 1
            return

        self._warn(
            ws_context,
            now=now,
            lag=lag,
            usage=usage,
            prev_now=prev_now,
            prev_usage=prev_usage,
        )
        if self._episode_started_at is None:
            self._episode_started_at = now
        self._last_warn_at = now

    def _recover(self, *, now: float, usage: ProcessUsage) -> None:
        assert self._episode_started_at is not None
        starvation_ms = round((now - self._episode_started_at) * 1000)
        max_lag_ms = round(self._episode_max_lag * 1000)
        episode = _UsageDelta.between(
            self._episode_start_now, self._episode_start_usage, now, usage
        )
        logger.bind(
            starvation_ms=starvation_ms,
            suppressed_warnings=self._suppressed,
            lagging_samples=self._episode_samples,
            max_lag_ms=max_lag_ms,
            **episode.fields(prefix="episode_"),
        ).info(
            f"WM event loop lag recovered after {starvation_ms}ms:"
            f" {self._episode_samples} lagging samples, max lag={max_lag_ms}ms"
            f" ({self._suppressed} warnings suppressed); over the episode"
            f" {episode.describe()}"
        )
        self._episode_started_at = None
        self._last_warn_at = None
        self._suppressed = 0
        self._episode_samples = 0
        self._episode_max_lag = 0.0
        self._episode_start_now = None
        self._episode_start_usage = None

    def _warn(
        self,
        ws_context: context.WorkspaceContext,
        *,
        now: float,
        lag: float,
        usage: ProcessUsage,
        prev_now: float | None,
        prev_usage: ProcessUsage | None,
    ) -> None:
        lag_context = snapshot_context(ws_context)
        host = read_host_load()
        ready_callbacks = _ready_callbacks()

        # The window runs from the previous sample to this late one. WM CPU
        # close to the window means the WM itself was busy (its own synchronous
        # code, or a thread of its own holding the GIL); CPU near zero with a
        # rising involuntary-switch count means the WM was runnable but the OS
        # gave the CPU to other processes; CPU near zero with many voluntary
        # switches means it was blocked waiting (e.g. synchronous I/O).
        window = _UsageDelta.between(prev_now, prev_usage, now, usage)

        lag_ms = round(lag * 1000)
        threshold_ms = round(self._threshold_sec * 1000)
        load_1m = None if host.load_1m is None else round(host.load_1m, 2)
        logger.bind(
            lag_ms=lag_ms,
            threshold_ms=threshold_ms,
            **window.fields(),
            load_1m=load_1m,
            cpu_count=host.cpu_count,
            ready_callbacks=ready_callbacks,
            runners_starting=lag_context.runners_starting,
            runners_running=lag_context.runners_running,
            budget_granted=lag_context.budget_granted,
            budget_total=lag_context.budget_total,
            in_flight_runs=lag_context.in_flight_runs,
        ).warning(
            f"WM event loop lag exceeded threshold: lag={lag_ms}ms"
            f" (threshold {threshold_ms}ms);"
            f" {window.describe()};"
            f" host load 1m={_fmt(load_1m)} on {_fmt(host.cpu_count)} CPUs;"
            f" ready callbacks={_fmt(ready_callbacks)};"
            f" runners starting={lag_context.runners_starting}"
            f" running={lag_context.runners_running};"
            f" process budget {lag_context.budget_granted}/{lag_context.budget_total};"
            f" in-flight runs={lag_context.in_flight_runs}"
        )
