"""Tracker tests: one warning per cooldown, one summary per episode, bounded state.

The episode accounting is shared by the WM and ER monitors, so it is tested
once here against fixed clocks: below-threshold samples stay silent, the first
late sample warns, further late samples inside the cooldown suppress, the next
below-threshold sample recovers with episode totals, and an interruption
mid-episode still yields a summary. The sampler re-arms from the observed wake
time (no burst after a stall) and the deque never exceeds the window.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from finecode_common.lag import (
    LagEpisodeTracker,
    LagSettings,
    ProcessUsage,
    arm_lag_baseline,
    run_lag_sampler,
)

_SETTINGS = LagSettings(
    interval_sec=0.5, threshold_sec=1.0, cooldown_sec=30.0, window_sec=600.0
)


def _usage(cpu_sec: float = 0.0) -> ProcessUsage:
    return ProcessUsage(cpu_sec=cpu_sec, voluntary_switches=0, involuntary_switches=0)


def test_healthy_samples_stay_silent() -> None:
    tracker = LagEpisodeTracker(_SETTINGS)

    event = tracker.observe(now=100.0, lag=0.1, usage=_usage())

    assert event.kind == "healthy"
    assert event.episode is None


def test_first_late_sample_warns_and_cooldown_suppresses() -> None:
    tracker = LagEpisodeTracker(_SETTINGS)
    _ = tracker.observe(now=100.0, lag=0.1, usage=_usage())

    warning = tracker.observe(now=100.5, lag=1.6, usage=_usage(cpu_sec=1.0))

    assert warning.kind == "warning"
    assert warning.episode is None
    assert warning.window.process_cpu_ms == 1000

    suppressed = tracker.observe(now=101.0, lag=1.2, usage=_usage(cpu_sec=1.5))

    assert suppressed.kind == "suppressed"


def test_recovery_reports_episode_totals() -> None:
    tracker = LagEpisodeTracker(_SETTINGS)
    _ = tracker.observe(now=100.0, lag=0.1, usage=_usage())
    _ = tracker.observe(now=100.5, lag=1.6, usage=_usage(cpu_sec=0.5))
    _ = tracker.observe(now=101.0, lag=1.2, usage=_usage(cpu_sec=0.6))

    event = tracker.observe(now=101.5, lag=0.1, usage=_usage(cpu_sec=2.0))

    assert event.kind == "recovered"
    assert event.episode is not None
    assert event.episode.starvation_ms == 1000
    assert event.episode.lagging_samples == 2
    assert event.episode.max_lag_ms == 1600
    assert event.episode.suppressed_warnings == 1
    assert event.episode.usage.window_ms == 1500
    assert event.episode.usage.process_cpu_ms == 2000

    # A new late sample after recovery opens a fresh episode.
    assert (
        tracker.observe(now=102.0, lag=1.6, usage=_usage(cpu_sec=2.5)).kind == "warning"
    )


def test_interrupt_closes_open_episode_and_is_none_when_healthy() -> None:
    tracker = LagEpisodeTracker(_SETTINGS)

    assert tracker.interrupt(now=100.0, usage=_usage()) is None

    _ = tracker.observe(now=100.0, lag=0.1, usage=_usage())
    _ = tracker.observe(now=100.5, lag=1.6, usage=_usage(cpu_sec=0.5))

    summary = tracker.interrupt(now=101.5, usage=_usage(cpu_sec=1.0))

    assert summary is not None
    assert summary.lagging_samples == 1
    assert summary.max_lag_ms == 1600


def test_samples_pruned_beyond_window() -> None:
    settings = LagSettings(
        interval_sec=0.5, threshold_sec=100.0, cooldown_sec=30.0, window_sec=600.0
    )
    tracker = LagEpisodeTracker(settings)
    _ = tracker.observe(now=0.0, lag=1.0, usage=_usage())
    _ = tracker.observe(now=601.0, lag=0.1, usage=_usage())

    assert len(tracker._samples) == 1
    assert len(tracker._samples) <= settings.retained_samples


def test_retained_samples_bound_matches_settings() -> None:
    assert _SETTINGS.retained_samples == 1201


def test_recent_reports_latest_and_window_max() -> None:
    tracker = LagEpisodeTracker(_SETTINGS)
    _ = tracker.observe(now=100.0, lag=0.1, usage=_usage())
    _ = tracker.observe(now=100.5, lag=2.4, usage=_usage())
    _ = tracker.observe(now=101.0, lag=0.05, usage=_usage())

    latest_ms, max_recent_ms = tracker.recent(now=101.0, window_sec=30.0)

    assert latest_ms == 50
    assert max_recent_ms == 2400


async def test_sampler_rearms_from_observed_wake_without_burst() -> None:
    """A long block leaves one late sample behind, not a catch-up burst.

    Without re-arming from the wake time, the sleeper would fire immediately
    for every interval missed during the stall.
    """
    settings = LagSettings(
        interval_sec=0.05, threshold_sec=100.0, cooldown_sec=30.0, window_sec=600.0
    )
    loop = asyncio.get_running_loop()
    baseline = arm_lag_baseline(loop, settings)
    seen: list[tuple[float, float]] = []

    async def _run() -> None:
        await run_lag_sampler(
            settings,
            baseline=baseline,
            on_sample=lambda now, lag: seen.append((now, lag)),
        )

    task = asyncio.create_task(_run())
    try:
        await asyncio.sleep(0.12)
        time.sleep(0.3)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
        await asyncio.sleep(0.12)
    finally:
        _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert seen, "the sampler must have taken samples"
    assert sum(1 for _, lag in seen if lag > 0.2) == 1
