"""Event-loop lag sampling mechanics shared by the WM and ER monitors.

The WM's ``EventLoopLagMonitor`` and the ER's ``ErLagMonitor`` both watch one
thing: how late their own asyncio loop runs behind its due time. The episode
accounting (one warning per cooldown, one recovery summary, a bounded sample
window) is identical in both; only the log messages and the surrounding process
state differ. This module owns the shared mechanics; each adapter owns its
messages, thresholds and lifecycle.

Sampling is diagnostic only: it changes no scheduling, and nothing depends on
it running.

This package is a leaf: stdlib plus psutil only.
"""

from __future__ import annotations

import asyncio
import collections
import collections.abc
import dataclasses
import os
import time
from typing import Any, Literal

try:
    import resource
except ImportError:  # not available on Windows
    resource: Any = None

__all__ = [
    "EpisodeSummary",
    "HostLoad",
    "LagBaseline",
    "LagEpisodeTracker",
    "LagEvent",
    "LagEventKind",
    "LagSettings",
    "ProcessUsage",
    "UsageDelta",
    "arm_lag_baseline",
    "read_host_load",
    "read_process_usage",
    "ready_callbacks",
    "run_lag_sampler",
]

LagEventKind = Literal["healthy", "warning", "suppressed", "recovered"]


@dataclasses.dataclass(frozen=True)
class LagSettings:
    """How often to sample and when a lag episode warns."""

    interval_sec: float
    threshold_sec: float
    cooldown_sec: float
    window_sec: float

    @property
    def retained_samples(self) -> int:
        """Upper bound on samples kept: the window plus the one being taken."""
        return round(self.window_sec / self.interval_sec) + 1


@dataclasses.dataclass(frozen=True)
class ProcessUsage:
    """Cumulative resource counters of the process at one instant.

    Only differences between two readings mean anything. CPU time covers every
    thread of the process, not only the loop thread. The context-switch
    counters are ``None`` where ``getrusage`` is unavailable.
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


def ready_callbacks() -> int | None:
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


def _delta(current: int | None, previous: int | None) -> int | None:
    if current is None or previous is None:
        return None
    return current - previous


def _fmt(value: object) -> str:
    return "n/a" if value is None else str(value)


@dataclasses.dataclass(frozen=True)
class UsageDelta:
    """What the process consumed between two samples."""

    window_ms: int | None = None
    process_cpu_ms: int | None = None
    process_cpu_pct: int | None = None
    voluntary_switches: int | None = None
    involuntary_switches: int | None = None

    @classmethod
    def between(
        cls,
        start_now: float | None,
        start_usage: ProcessUsage | None,
        now: float,
        usage: ProcessUsage,
    ) -> UsageDelta:
        if start_now is None or start_usage is None:
            return cls()
        window_ms = round((now - start_now) * 1000)
        process_cpu_ms = round((usage.cpu_sec - start_usage.cpu_sec) * 1000)
        return cls(
            window_ms=window_ms,
            process_cpu_ms=process_cpu_ms,
            process_cpu_pct=round(process_cpu_ms * 100 / window_ms)
            if window_ms > 0
            else None,
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

    def describe(self, *, process: str = "WM") -> str:
        return (
            f"{process} CPU {_fmt(self.process_cpu_ms)}ms"
            f" over {_fmt(self.window_ms)}ms window"
            f" ({_fmt(self.process_cpu_pct)}%),"
            f" context switches involuntary=+{_fmt(self.involuntary_switches)}"
            f" voluntary=+{_fmt(self.voluntary_switches)}"
        )


@dataclasses.dataclass(frozen=True)
class EpisodeSummary:
    """Totals over one lag episode, reported once when it ends."""

    starvation_ms: int
    lagging_samples: int
    max_lag_ms: int
    suppressed_warnings: int
    usage: UsageDelta


@dataclasses.dataclass(frozen=True)
class LagEvent:
    """What one sample did to the episode state."""

    kind: LagEventKind
    lag_sec: float
    window: UsageDelta
    episode: EpisodeSummary | None


class LagEpisodeTracker:
    """Turn a stream of lag samples into a bounded warning/recovery sequence.

    At most one warning per episode per cooldown, and one summary when the
    episode ends. Sample storage is bounded by the settings window. The tracker
    never touches loop tasks, futures or transports: expiry and shutdown are
    the adapter's job.
    """

    def __init__(self, settings: LagSettings) -> None:
        self._settings = settings
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
        # The previous sample, so a warning can report what the process
        # consumed over the window that ended late.
        self._prev_now: float | None = None
        self._prev_usage: ProcessUsage | None = None
        self._samples: collections.deque[tuple[float, float]] = collections.deque()

    @property
    def settings(self) -> LagSettings:
        return self._settings

    def observe(self, *, now: float, lag: float, usage: ProcessUsage) -> LagEvent:
        """Record one lag sample.

        ``now`` is the loop clock's current time and ``lag`` the delay of this
        sample behind its due time. ``usage`` is the process's counters at
        ``now``; the adapter reads them so tests can supply fixed values.
        """
        prev_now, prev_usage = self._prev_now, self._prev_usage
        self._prev_now, self._prev_usage = now, usage
        self._samples.append((now, lag))
        cutoff = now - self._settings.window_sec
        while self._samples and self._samples[0][0] < cutoff:
            _ = self._samples.popleft()

        window = UsageDelta.between(prev_now, prev_usage, now, usage)

        if lag < self._settings.threshold_sec:
            if self._episode_started_at is None:
                return LagEvent(
                    kind="healthy", lag_sec=lag, window=window, episode=None
                )
            episode = self._close_episode(now=now, usage=usage)
            return LagEvent(
                kind="recovered", lag_sec=lag, window=window, episode=episode
            )

        if self._episode_started_at is None:
            # The episode's window starts where its first late sample's window
            # did, so its totals cover the whole stall.
            self._episode_start_now = prev_now
            self._episode_start_usage = prev_usage
        self._episode_samples += 1
        self._episode_max_lag = max(self._episode_max_lag, lag)

        if (
            self._last_warn_at is not None
            and now - self._last_warn_at < self._settings.cooldown_sec
        ):
            self._suppressed += 1
            return LagEvent(kind="suppressed", lag_sec=lag, window=window, episode=None)

        if self._episode_started_at is None:
            self._episode_started_at = now
        self._last_warn_at = now
        return LagEvent(kind="warning", lag_sec=lag, window=window, episode=None)

    def interrupt(self, *, now: float, usage: ProcessUsage) -> EpisodeSummary | None:
        """Close the open episode without a below-threshold sample.

        Shutdown stops the sampler mid-episode, so there is no recovery sample
        coming; the adapter reports this summary as an interruption, never as a
        recovery. Returns ``None`` when no episode is open.
        """
        if self._episode_started_at is None:
            return None
        return self._close_episode(now=now, usage=usage)

    def recent(self, *, now: float, window_sec: float) -> tuple[int | None, int | None]:
        """Latest and window-max lag in milliseconds over retained samples."""
        latest_ms: int | None = None
        max_recent_ms: int | None = None
        if self._samples:
            latest_ms = round(self._samples[-1][1] * 1000)
            cutoff = now - window_sec
            recent = [lag for sample_now, lag in self._samples if sample_now >= cutoff]
            if recent:
                max_recent_ms = round(max(recent) * 1000)
        return latest_ms, max_recent_ms

    def _close_episode(self, *, now: float, usage: ProcessUsage) -> EpisodeSummary:
        assert self._episode_started_at is not None
        episode = EpisodeSummary(
            starvation_ms=round((now - self._episode_started_at) * 1000),
            lagging_samples=self._episode_samples,
            max_lag_ms=round(self._episode_max_lag * 1000),
            suppressed_warnings=self._suppressed,
            usage=UsageDelta.between(
                self._episode_start_now, self._episode_start_usage, now, usage
            ),
        )
        self._episode_started_at = None
        self._last_warn_at = None
        self._suppressed = 0
        self._episode_samples = 0
        self._episode_max_lag = 0.0
        self._episode_start_now = None
        self._episode_start_usage = None
        return episode


@dataclasses.dataclass(frozen=True)
class LagBaseline:
    """Starting point for the sampler: first due time and previous wake time."""

    next_due: float
    prev_now: float


def arm_lag_baseline(
    loop: asyncio.AbstractEventLoop, settings: LagSettings
) -> LagBaseline:
    """Arm the first sample one interval out from the loop's current time."""
    now = loop.time()
    return LagBaseline(next_due=now + settings.interval_sec, prev_now=now)


async def run_lag_sampler(
    settings: LagSettings,
    *,
    baseline: LagBaseline,
    on_sample: collections.abc.Callable[[float, float], None],
) -> None:
    """Sample until cancelled.

    ``next_due`` is absolute and re-armed from the observed wake time rather
    than incremented, so a long block leaves one late sample behind instead
    of a burst of immediate ones chasing the schedule that was missed.
    Unexpected errors from ``on_sample`` propagate to the caller; adapters own
    their terminal logging.
    """
    loop = asyncio.get_running_loop()
    next_due = baseline.next_due
    while True:
        await asyncio.sleep(max(0.0, next_due - loop.time()))
        now = loop.time()
        on_sample(now, now - next_due)
        next_due = now + settings.interval_sec
