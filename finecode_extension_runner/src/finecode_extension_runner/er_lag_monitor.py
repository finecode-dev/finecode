"""ER endpoint event-loop lag monitor.

One instance per endpoint-enabled ER process, owned by :class:`ErServer`. It
samples the ER loop's own scheduling lag and reports it together with the
state that makes it attributable, reusing the shared mechanics in
:mod:`finecode_common.lag`. Only messages and process state live here.

Diagnostic only: it changes no scheduling, and nothing depends on it running.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os

from finecode_common.host_readers import HostPressure, read_host_pressure
from finecode_common.lag import (
    EpisodeSummary,
    HostLoad,
    LagBaseline,
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

from finecode_extension_runner import er_telemetry, global_state
from finecode_extension_runner.shared_telemetry.health import (
    HealthTracker,
    SetupFailureKind,
    SignalSetupStatus,
    TelemetrySignal,
)

__all__ = [
    "COOLDOWN_SEC",
    "INTERVAL_SEC",
    "THRESHOLD_SEC",
    "WINDOW_SEC",
    "ErLagMonitor",
]

INTERVAL_SEC = 0.5
"""How often the ER loop is sampled for lag."""

THRESHOLD_SEC = 1.0
"""Lag above this is recorded as a warning."""

COOLDOWN_SEC = 30.0
"""Minimum spacing between warnings while lag stays over the threshold."""

WINDOW_SEC = 600.0
"""Sample window; bounds retained samples to 1,201."""

_LAG_SIGNAL: TelemetrySignal = "lag_monitor"

_UNSET = "unavailable"


def _fmt(value: object) -> str:
    return "n/a" if value is None else str(value)


def _identity_extras() -> dict[str, object]:
    """pid/project/env at use time; ``"unavailable"`` when unset."""
    project = global_state.project_dir_path
    env = global_state.env_name
    return {
        "pid": os.getpid(),
        "project_path": str(project) if project is not None else _UNSET,
        "env_name": env or _UNSET,
    }


class ErLagMonitor:
    """Sample the ER loop's lag and report threshold crossings.

    One instance per endpoint-enabled ER, driven by :meth:`arm`/:meth:`start`
    on the loop it measures. At most one warning per episode per cooldown, one
    recovery summary when the episode ends, and one interruption summary when
    shutdown stops the sampler mid-episode.
    """

    def __init__(self, *, health: HealthTracker | None = None) -> None:
        if health is None:
            health = er_telemetry._health
        self._health = health
        self._settings = LagSettings(
            interval_sec=INTERVAL_SEC,
            threshold_sec=THRESHOLD_SEC,
            cooldown_sec=COOLDOWN_SEC,
            window_sec=WINDOW_SEC,
        )
        self._tracker = LagEpisodeTracker(self._settings)
        self._baseline: LagBaseline | None = None
        self._task: asyncio.Task[None] | None = None
        self._terminated: asyncio.Future[None] | None = None
        self._stopped = False
        self._availability: SignalSetupStatus = "disabled"
        self._last_usage: ProcessUsage | None = None

    @property
    def availability(self) -> SignalSetupStatus:
        """The monitor's setup outcome for health summaries."""
        return self._availability

    @property
    def terminated(self) -> asyncio.Future[None]:
        """Resolved when the sampler task ends, or when :meth:`stop` runs."""
        if self._terminated is None:
            self._terminated = asyncio.get_running_loop().create_future()
        return self._terminated

    def arm(self) -> None:
        """Record the sampling baseline on the running loop.

        Synchronous: adds no await beyond the surrounding ``initialize``.
        """
        try:
            loop = asyncio.get_running_loop()
            if self._terminated is None:
                self._terminated = loop.create_future()
            self._baseline = arm_lag_baseline(loop, self._settings)
        except Exception:
            self._mark_unavailable("monitor_start_failed")
            self._resolve_terminated()
            raise

    def start(self) -> None:
        """Create the sampler task with an explicit clean execution context.

        The task runs under a fresh :class:`contextvars.Context` so the
        creating request's trace never leaks into lag records.
        """
        try:
            loop = asyncio.get_running_loop()
            if self._terminated is None:
                self._terminated = loop.create_future()
            if self._baseline is None:
                self._baseline = arm_lag_baseline(loop, self._settings)
            self._task = loop.create_task(self._run(), context=contextvars.Context())
            self._task.add_done_callback(self._on_task_done)
            self._mark_active()
        except Exception:
            self._mark_unavailable("monitor_start_failed")
            self._resolve_terminated()
            raise

    async def stop(self) -> None:
        """Release the sampler task; idempotent.

        Never waits on export. An episode open at shutdown is reported as
        interrupted, never as recovered.
        """
        if self._stopped:
            return
        self._stopped = True
        task, self._task = self._task, None
        if self._terminated is None:
            with contextlib.suppress(RuntimeError):
                self._terminated = asyncio.get_running_loop().create_future()
        self._interrupt_open_episode()
        if task is not None and not task.done():
            _ = task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._resolve_terminated()

    async def _run(self) -> None:
        assert self._baseline is not None
        try:
            await run_lag_sampler(
                self._settings,
                baseline=self._baseline,
                on_sample=self._on_sample,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - terminal: the monitor stops once, never restarts
            self._mark_unavailable("monitor_sampler_failed")
            self._baseline = None
            # The terminal record itself must not raise.
            with contextlib.suppress(Exception):
                logger.exception(
                    "ER event loop lag monitor stopped after an unexpected error"
                )

    def _on_sample(self, now: float, lag: float) -> None:
        usage = self._read_usage()
        event = self._tracker.observe(now=now, lag=lag, usage=usage)
        if event.kind == "warning":
            self._warn(lag=lag, window=event.window)
        elif event.kind == "suppressed":
            self._health.count(_LAG_SIGNAL, "suppressed")
        elif event.kind == "recovered":
            assert event.episode is not None
            self._recover(event.episode)

    def _read_usage(self) -> ProcessUsage:
        try:
            usage = read_process_usage()
        except Exception:  # noqa: BLE001 - a raising reader leaves fields degraded, never stops sampling
            self._health.count(_LAG_SIGNAL, "contained_failure")
            if self._last_usage is not None:
                return self._last_usage
            usage = ProcessUsage(
                cpu_sec=0.0, voluntary_switches=None, involuntary_switches=None
            )
        self._last_usage = usage
        return usage

    def _warn(self, *, lag: float, window: UsageDelta) -> None:
        try:
            host = read_host_load()
        except Exception:  # noqa: BLE001 - fields unavailable, sampling continues
            host = HostLoad(load_1m=None, cpu_count=None)
            self._health.count(_LAG_SIGNAL, "contained_failure")
        try:
            pressure = read_host_pressure()
        except Exception:  # noqa: BLE001 - fields unavailable, sampling continues
            pressure = HostPressure(
                mem_available_mb=None,
                swap_used_mb=None,
                psi_memory_full_avg10=None,
                psi_io_full_avg10=None,
                psi_cpu_some_avg10=None,
            )
            self._health.count(_LAG_SIGNAL, "contained_failure")
        try:
            ready = ready_callbacks()
        except Exception:  # noqa: BLE001 - fields unavailable, sampling continues
            ready = None
            self._health.count(_LAG_SIGNAL, "contained_failure")

        lag_ms = round(lag * 1000)
        threshold_ms = round(self._settings.threshold_sec * 1000)
        load_1m = None if host.load_1m is None else round(host.load_1m, 2)
        logger.bind(
            lag_ms=lag_ms,
            threshold_ms=threshold_ms,
            **window.fields(),
            load_1m=load_1m,
            cpu_count=host.cpu_count,
            ready_callbacks=ready,
            **pressure.fields(),
            **_identity_extras(),
        ).warning(
            f"ER event loop lag exceeded threshold: lag={lag_ms}ms"
            f" (threshold {threshold_ms}ms);"
            f" {window.describe(process='ER')} (process-wide, all threads);"
            f" host load 1m={_fmt(load_1m)} on {_fmt(host.cpu_count)} CPUs;"
            f" ready callbacks={_fmt(ready)};"
            f" {pressure.describe()}"
        )

    def _recover(self, episode: EpisodeSummary) -> None:
        logger.bind(
            starvation_ms=episode.starvation_ms,
            lagging_samples=episode.lagging_samples,
            max_lag_ms=episode.max_lag_ms,
            suppressed_warnings=episode.suppressed_warnings,
            **episode.usage.fields(prefix="episode_"),
            **_identity_extras(),
        ).info(
            f"ER event loop lag recovered after {episode.starvation_ms}ms:"
            f" {episode.lagging_samples} lagging samples,"
            f" max lag={episode.max_lag_ms}ms"
            f" ({episode.suppressed_warnings} warnings suppressed); over the episode"
            f" {episode.usage.describe(process='ER')} (process-wide, all threads)"
        )

    def _interrupt_open_episode(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            usage = read_process_usage()
        except Exception:  # noqa: BLE001 - fall back to the last reading for the closing summary
            usage = self._last_usage
            if usage is None:
                return
        episode = self._tracker.interrupt(now=loop.time(), usage=usage)
        if episode is None:
            return
        # The closing summary must not fail shutdown.
        with contextlib.suppress(Exception):
            logger.bind(
                starvation_ms=episode.starvation_ms,
                lagging_samples=episode.lagging_samples,
                max_lag_ms=episode.max_lag_ms,
                suppressed_warnings=episode.suppressed_warnings,
                **episode.usage.fields(prefix="episode_"),
                **_identity_extras(),
            ).info(
                f"ER event loop lag episode interrupted after {episode.starvation_ms}ms:"
                f" {episode.lagging_samples} lagging samples,"
                f" max lag={episode.max_lag_ms}ms"
                f" ({episode.suppressed_warnings} warnings suppressed)"
            )

    def _on_task_done(self, _task: asyncio.Task[None]) -> None:
        self._resolve_terminated()

    def _resolve_terminated(self) -> None:
        if self._terminated is not None and not self._terminated.done():
            self._terminated.set_result(None)

    def _mark_active(self) -> None:
        self._availability = "active"
        self._health.set_availability(_LAG_SIGNAL, "active")

    def _mark_unavailable(self, reason: SetupFailureKind) -> None:
        self._availability = "unavailable"
        self._health.set_availability(_LAG_SIGNAL, "unavailable", reason)
