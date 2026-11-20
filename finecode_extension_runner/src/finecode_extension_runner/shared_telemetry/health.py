"""Loss counting and health snapshots shared by the WM and ER processes.

Every telemetry signal (traces, metrics, logs, and the ER lag monitor) can lose
evidence: sampling drops it (S3), a full queue drops it (S5), a failed export
drops it (S6), filtering or late-stage suppression drops it (S7), or a
contained observer failure drops it (S10). :class:`HealthTracker` counts those
losses per signal without logging — the counting path must never re-enter the
OTLP log sink — and renders the bounded summary the health emission reads.

Cause codes follow the diagnostic-health taxonomy (S3/S5/S6/S7/S10); see the
observability guide.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import datetime
import os
import threading
import time
import uuid
from typing import Any, Literal, NotRequired, TypedDict, cast

from loguru import logger

__all__ = [
    "EMIT_INTERVAL_SEC",
    "ErSetupCategory",
    "ErSetupCounts",
    "HealthEmitter",
    "HealthSummary",
    "HealthTracker",
    "LateAdmissionFlags",
    "LogGroupLevel",
    "LossCause",
    "LossCounts",
    "SetupFailureKind",
    "SignalHealth",
    "SignalSetupStatus",
    "SignalsHealth",
    "TelemetrySignal",
    "register_health_metrics",
]

TelemetrySignal = Literal["traces", "metrics", "logs", "lag_monitor"]

LossCause = Literal[
    "sampled_out", "queue_overflow", "export_failed", "suppressed", "contained_failure"
]
#                     S3             S5               S6              S7            S10

SignalSetupStatus = Literal["disabled", "active", "unavailable"]

SetupFailureKind = Literal[
    "invalid_setting",
    "import_error",
    "provider_error",
    "sdk_disabled",
    "monitor_start_failed",
    "monitor_sampler_failed",
]

ErSetupCategory = Literal[
    "all_active", "partly_unavailable", "all_unavailable", "disabled"
]

_LOSS_CAUSES: tuple[LossCause, ...] = (
    "sampled_out",
    "queue_overflow",
    "export_failed",
    "suppressed",
    "contained_failure",
)

_SIGNALS: tuple[TelemetrySignal, ...] = ("traces", "metrics", "logs", "lag_monitor")

_MAX_COUNT = 2**63 - 1


class LossCounts(TypedDict):
    sampled_out: int
    queue_overflow: int
    export_failed: int
    suppressed: int
    contained_failure: int


class SignalHealth(TypedDict):
    availability: SignalSetupStatus
    unavailable_reason: NotRequired[SetupFailureKind]
    counts: LossCounts


class LogGroupLevel(TypedDict):
    group: str
    level: str


class LateAdmissionFlags(TypedDict):
    contention: bool
    clock_failure: bool


class ErSetupCounts(TypedDict):
    all_active: int
    partly_unavailable: int
    all_unavailable: int
    disabled: int


class SignalsHealth(TypedDict):
    traces: SignalHealth
    metrics: SignalHealth
    logs: SignalHealth
    lag_monitor: NotRequired[SignalHealth]


class HealthSummary(TypedDict):
    process_role: Literal["wm", "er"]
    process_incarnation: str
    pid: int
    reported_at: str
    sampler: str
    log_levels: list[LogGroupLevel]
    signals: SignalsHealth
    late_admission_flags: LateAdmissionFlags
    er_setup_counts: NotRequired[ErSetupCounts]


class HealthTracker:
    """Thread-safe loss counters and availability flags for one process.

    All methods are non-blocking and never log. The optional ``on_change``
    callback fires after any state change (outside the internal lock, under its
    own guard); the health emission uses it to schedule a summary. The callback
    itself must be non-blocking.
    """

    def __init__(
        self,
        *,
        process_role: Literal["wm", "er"],
        process_incarnation: str | None = None,
        on_change: collections.abc.Callable[[], None] | None = None,
        read_log_levels: collections.abc.Callable[[], list[LogGroupLevel]]
        | None = None,
    ) -> None:
        self._process_role = process_role
        self._process_incarnation = (
            process_incarnation if process_incarnation is not None else uuid.uuid4().hex
        )
        self._on_change = on_change
        self._read_log_levels = read_log_levels
        self._lock = threading.Lock()
        self._counts: dict[TelemetrySignal, dict[LossCause, int]] = {
            signal: dict.fromkeys(_LOSS_CAUSES, 0) for signal in _SIGNALS
        }
        self._availability: dict[TelemetrySignal, SignalSetupStatus] = dict.fromkeys(
            _SIGNALS, "disabled"
        )
        self._reasons: dict[TelemetrySignal, SetupFailureKind | None] = dict.fromkeys(
            _SIGNALS
        )
        self._contention = False
        self._clock_failure = False
        self._er_setup_counts: dict[ErSetupCategory, int] = {
            "all_active": 0,
            "partly_unavailable": 0,
            "all_unavailable": 0,
            "disabled": 0,
        }

    @property
    def process_incarnation(self) -> str:
        """Fresh hex id per process start, shared by spans and summaries."""
        return self._process_incarnation

    def count(self, signal: TelemetrySignal, cause: LossCause, n: int = 1) -> None:
        """Add a loss count, saturating at 2**63 - 1."""
        if n <= 0:
            return
        with self._lock:
            current = self._counts[signal][cause]
            self._counts[signal][cause] = min(_MAX_COUNT, current + n)
        self._notify_changed()

    def set_availability(
        self,
        signal: TelemetrySignal,
        status: SignalSetupStatus,
        reason: SetupFailureKind | None = None,
    ) -> None:
        """Record a signal's setup outcome."""
        with self._lock:
            self._availability[signal] = status
            self._reasons[signal] = reason
        self._notify_changed()

    def availability(self, signal: TelemetrySignal) -> SignalSetupStatus:
        """Current setup outcome of one signal (a single dict read, no lock)."""
        return self._availability[signal]

    def set_flag(self, name: Literal["contention", "clock_failure"]) -> None:
        """Set a sticky late-admission flag; flags never clear."""
        with self._lock:
            if name == "contention":
                self._contention = True
            else:
                self._clock_failure = True
        self._notify_changed()

    def set_on_change(
        self, callback: collections.abc.Callable[[], None] | None
    ) -> None:
        """Install or remove the change hook the emission reads."""
        with self._lock:
            self._on_change = callback

    def set_log_level_reader(
        self,
        reader: collections.abc.Callable[[], list[LogGroupLevel]] | None,
    ) -> None:
        """Install or remove the effective-level reader the summary reads."""
        with self._lock:
            self._read_log_levels = reader

    def note_er_setup(self, category: ErSetupCategory) -> None:
        """Count one ER's setup outcome (WM only)."""
        with self._lock:
            self._er_setup_counts[category] = min(
                _MAX_COUNT, self._er_setup_counts[category] + 1
            )
        self._notify_changed()

    def release_er_setup(self, category: ErSetupCategory) -> None:
        """Release one ER's setup outcome when that runner stops or is abandoned."""
        with self._lock:
            self._er_setup_counts[category] = max(
                0, self._er_setup_counts[category] - 1
            )
        self._notify_changed()

    def snapshot(self) -> HealthSummary:
        """Render the current health summary. Never logs."""
        with self._lock:
            signals: SignalsHealth = {
                "traces": self._signal_health("traces"),
                "metrics": self._signal_health("metrics"),
                "logs": self._signal_health("logs"),
            }
            if self._process_role == "er":
                signals["lag_monitor"] = self._signal_health("lag_monitor")
            flags: LateAdmissionFlags = {
                "contention": self._contention,
                "clock_failure": self._clock_failure,
            }
            er_counts: ErSetupCounts = {
                "all_active": self._er_setup_counts["all_active"],
                "partly_unavailable": self._er_setup_counts["partly_unavailable"],
                "all_unavailable": self._er_setup_counts["all_unavailable"],
                "disabled": self._er_setup_counts["disabled"],
            }
        summary: HealthSummary = {
            "process_role": self._process_role,
            "process_incarnation": self._process_incarnation,
            "pid": os.getpid(),
            "reported_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "sampler": os.environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on"),
            "log_levels": self._current_log_levels(),
            "signals": signals,
            "late_admission_flags": flags,
        }
        if self._process_role == "wm":
            summary["er_setup_counts"] = er_counts
        return summary

    def _signal_health(self, signal: TelemetrySignal) -> SignalHealth:
        health: SignalHealth = {
            "availability": self._availability[signal],
            "counts": {
                "sampled_out": self._counts[signal]["sampled_out"],
                "queue_overflow": self._counts[signal]["queue_overflow"],
                "export_failed": self._counts[signal]["export_failed"],
                "suppressed": self._counts[signal]["suppressed"],
                "contained_failure": self._counts[signal]["contained_failure"],
            },
        }
        reason = self._reasons[signal]
        if reason is not None:
            health["unavailable_reason"] = reason
        return health

    def _current_log_levels(self) -> list[LogGroupLevel]:
        if self._read_log_levels is None:
            return []
        try:
            return self._read_log_levels()
        except Exception:  # noqa: BLE001 - a failing level reader must not break the snapshot
            return []

    def _notify_changed(self) -> None:
        callback = self._on_change
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback()


EMIT_INTERVAL_SEC = 60.0
"""Minimum spacing between health summaries; a change inside the window waits for its end."""


def _change_key(summary: HealthSummary) -> tuple[object, object, object]:
    """The trigger set: signal health, admission flags and ER setup counts.

    Timestamps, incarnation, sampler and log levels ride along in the record
    but never trigger one.
    """
    return (
        summary["signals"],
        summary["late_admission_flags"],
        summary.get("er_setup_counts"),
    )


_KNOWN_SIGNALS: tuple[TelemetrySignal, ...] = (
    "traces",
    "metrics",
    "logs",
    "lag_monitor",
)


def _iter_signals(
    summary: HealthSummary,
) -> collections.abc.Iterator[tuple[str, SignalHealth]]:
    """The signals present in this summary (the WM has no lag monitor)."""
    signals = summary["signals"]
    for name in _KNOWN_SIGNALS:
        health = cast("SignalHealth | None", signals.get(name))
        if health is not None:
            yield name, health


def _counts_list(health: SignalHealth) -> dict[str, int]:
    """Loss counts by cause; the TypedDict holds exactly these integer fields."""
    return cast("dict[str, int]", health["counts"])


def _loss_total(summary: HealthSummary) -> int:
    return sum(
        value
        for _, signal in _iter_signals(summary)
        for value in _counts_list(signal).values()
    )


def _digest(summary: HealthSummary) -> str:
    """Human-readable one-liner; the structured fields carry the detail."""
    total = _loss_total(summary)
    if total == 0:
        states = ", ".join(
            f"{name} {health['availability']}"
            for name, health in _iter_signals(summary)
        )
        headline = f"no evidence lost ({states})"
    else:
        parts = []
        for name, health in _iter_signals(summary):
            lost = sum(_counts_list(health).values())
            if not lost and health["availability"] == "active":
                continue
            detail = f"{name} {health['availability']}"
            reason = health.get("unavailable_reason")
            if reason is not None:
                detail += f" ({reason})"
            if lost:
                detail += f" lost {lost}"
            parts.append(detail)
        headline = f"{total} evidence records lost ({'; '.join(parts)})"
    flags = [name for name, set_ in summary["late_admission_flags"].items() if set_]
    if flags:
        headline += f"; admission flags: {', '.join(flags)}"
    headline += f"; sampler {summary['sampler']}"
    return f"telemetry health summary: {headline}"


def _extras(summary: HealthSummary) -> dict[str, Any]:
    """Flat scalar OTel attributes for the summary record."""
    extras: dict[str, Any] = {
        "health_sampler": summary["sampler"],
        "health_reported_at": summary["reported_at"],
    }
    for name, health in _iter_signals(summary):
        extras[f"health_{name}_availability"] = health["availability"]
        reason = health.get("unavailable_reason")
        if reason is not None:
            extras[f"health_{name}_unavailable_reason"] = reason
        for cause, value in _counts_list(health).items():
            extras[f"health_{name}_{cause}"] = value
    counts = summary.get("er_setup_counts")
    if counts is not None:
        for category, value in counts.items():
            extras[f"health_er_setup_{category}"] = value
    flags = summary["late_admission_flags"]
    extras["health_late_contention"] = flags["contention"]
    extras["health_late_clock_failure"] = flags["clock_failure"]
    levels = summary["log_levels"]
    if levels:
        extras["health_log_levels"] = ",".join(
            f"{entry['group']}={entry['level']}" for entry in levels
        )
    return extras


class HealthEmitter:
    """Write bounded health summaries: once at setup, then on change, at most
    once per minute.

    The emitter never awaits export and never logs per loss -- the summary is
    the only record. Attach and emit only for processes whose setup was
    attempted; disabled processes stay silent with no timer.
    """

    def __init__(self, health: HealthTracker) -> None:
        self._health = health
        self._loop: asyncio.AbstractEventLoop | None = None
        self._armed = False
        self._last_key: object = None
        self._last_emit_at = 0.0
        self._timer: asyncio.TimerHandle | None = None

    def attach(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Install the change hook and capture the owning loop when there is one.

        Without a running loop the hook is still installed; the loop is
        captured lazily by the first change observed on one, so setup paths
        that run before the loop starts still get ongoing summaries.
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is not None:
            self._loop = loop
        self._health.set_on_change(self._schedule)

    def detach(self) -> None:
        """Remove the change hook and drop any pending emission."""
        self._health.set_on_change(None)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def emit_initial(self) -> None:
        """Write the first summary when the setup attempt completes."""
        self._armed = True
        self._write(self._health.snapshot())

    def _schedule(self) -> None:
        """Change hook: runs on any thread, never blocks, never logs."""
        if not self._armed:
            return
        loop = self._loop
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
            if loop is None:
                return
            self._loop = loop
        with contextlib.suppress(RuntimeError):
            _ = loop.call_soon_threadsafe(self._recheck)

    def _recheck(self) -> None:
        if not self._armed:
            return
        summary = self._health.snapshot()
        if _change_key(summary) == self._last_key:
            return
        delay = EMIT_INTERVAL_SEC - (time.monotonic() - self._last_emit_at)
        if delay > 0:
            if self._timer is None and self._loop is not None:
                with contextlib.suppress(RuntimeError):
                    self._timer = self._loop.call_later(delay, self._timer_fired)
            return
        self._write(summary)

    def _timer_fired(self) -> None:
        self._timer = None
        self._recheck()

    def _write(self, summary: HealthSummary) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._last_key = _change_key(summary)
        self._last_emit_at = time.monotonic()
        level = (
            "WARNING"
            if _loss_total(summary) > 0 or any(summary["late_admission_flags"].values())
            else "INFO"
        )
        logger.bind(**_extras(summary)).log(level, _digest(summary))


def register_health_metrics(meter: Any, health: HealthTracker) -> None:
    """Mirror loss counts and availability as OTel instruments (no logs)."""
    from opentelemetry.metrics import Observation

    def _loss_observations(_options: Any) -> collections.abc.Iterator[Observation]:
        try:
            summary = health.snapshot()
        except Exception:  # noqa: BLE001 - a metric callback must not raise
            return
        for signal, signal_health in _iter_signals(summary):
            for cause, value in _counts_list(signal_health).items():
                yield Observation(value, {"signal": signal, "cause": cause})

    def _availability_observations(
        _options: Any,
    ) -> collections.abc.Iterator[Observation]:
        try:
            summary = health.snapshot()
        except Exception:  # noqa: BLE001 - a metric callback must not raise
            return
        for signal, signal_health in _iter_signals(summary):
            yield Observation(
                1 if signal_health["availability"] == "active" else 0,
                {"signal": signal},
            )

    meter.create_observable_counter(
        "finecode.telemetry.evidence_loss",
        callbacks=[_loss_observations],
        unit="{record}",
        description="Telemetry evidence lost per signal and cause.",
    )
    meter.create_observable_gauge(
        "finecode.telemetry.signal_available",
        callbacks=[_availability_observations],
        unit="{state}",
        description="Whether a telemetry signal is currently available.",
    )
