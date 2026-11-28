"""OTel runtime for synchronous-phase timing (ER side).

The API module owns the scope orchestration and failure containment; this
module owns the clocks, the span and its attributes. It never logs and emits
no event or metric per phase: the span is the record.
"""

from __future__ import annotations

import threading
import time
import typing

from finecode_extension_api.phase_timing_runtime import (
    CountsKind,
    IPhaseObservation,
    IPhaseTimingRuntime,
    PhaseCounts,
    PhaseOutcome,
    PhaseTimingBinding,
)

from finecode_extension_runner import er_telemetry
from finecode_extension_runner.shared_telemetry.health import HealthTracker

__all__ = [
    "OtelPhaseTimingBinding",
]

if typing.TYPE_CHECKING:
    from opentelemetry.trace import Span

_perf_counter_ns = time.perf_counter_ns
_thread_time_ns = getattr(time, "thread_time_ns", None)
_time_ns = time.time_ns


def _read_perf_ns() -> tuple[int | None, str | None]:
    """Elapsed clock reading; the reason when it fails."""
    try:
        return _perf_counter_ns(), None
    except AttributeError:
        return None, "clock_unavailable"
    except Exception:  # noqa: BLE001 - a broken clock degrades, never raises
        return None, "clock_error"


def _read_thread_ns() -> tuple[int | None, str | None]:
    """Calling-thread CPU reading; the reason when it fails."""
    if _thread_time_ns is None:
        return None, "clock_unavailable"
    try:
        return _thread_time_ns(), None
    except Exception:  # noqa: BLE001 - a broken clock degrades, never raises
        return None, "clock_error"


def _read_wall_ns() -> int | None:
    try:
        return _time_ns()
    except Exception:  # noqa: BLE001 - span times fall back to now
        return None


class _Observation(IPhaseObservation):
    """One timed phase: clocks read in ``start``/``stop``, span closed in ``close``."""

    def __init__(self, span: Span, health: HealthTracker) -> None:
        self._span = span
        self._health = health
        self._input: PhaseCounts | None = None
        self._completion: PhaseCounts | None = None
        self._input_set = False
        self._completion_set = False
        self._start_perf: int | None = None
        self._start_perf_reason: str | None = None
        self._start_thread: int | None = None
        self._start_thread_reason: str | None = None
        self._start_ident: int | None = None
        self._stop_perf: int | None = None
        self._stop_perf_reason: str | None = None
        self._stop_thread: int | None = None
        self._stop_thread_reason: str | None = None
        self._stop_ident: int | None = None
        self._stop_wall: int | None = None
        self._attrs_failed = False

    def set_counts(self, kind: CountsKind, counts: PhaseCounts | None) -> None:
        if kind == "input":
            self._input = counts
            self._input_set = True
        else:
            self._completion = counts
            self._completion_set = True

    def start(self) -> None:
        self._start_perf, self._start_perf_reason = _read_perf_ns()
        self._start_thread, self._start_thread_reason = _read_thread_ns()
        self._start_ident = threading.get_ident()

    def stop(self) -> None:
        self._stop_perf, self._stop_perf_reason = _read_perf_ns()
        self._stop_thread, self._stop_thread_reason = _read_thread_ns()
        self._stop_ident = threading.get_ident()
        self._stop_wall = _read_wall_ns()

    def close(self, outcome: PhaseOutcome) -> None:
        elapsed_ns, elapsed_reason = self._elapsed()
        cpu_ns, cpu_reason = self._thread_cpu()
        self._set("finecode.phase.outcome", outcome)
        if elapsed_ns is None:
            # _elapsed pairs a missing reading with a reason; the fallback is unreachable.
            self._set(
                "finecode.phase.elapsed_unavailable_reason",
                elapsed_reason or "clock_error",
            )
        else:
            self._set("finecode.phase.elapsed_ns", elapsed_ns)
        if cpu_ns is None:
            # _thread_cpu pairs a missing reading with a reason; the fallback is unreachable.
            self._set(
                "finecode.phase.thread_cpu_unavailable_reason",
                cpu_reason or "clock_error",
            )
        else:
            # A genuine zero stays: only unavailable is omitted.
            self._set("finecode.phase.thread_cpu_ns", cpu_ns)
        if self._completion_set and self._completion is not None:
            self._set("finecode.phase.completion", "available")
        else:
            self._set("finecode.phase.completion", "unavailable")
        unavailable: list[str] = []
        for kind, counts, was_set in (
            ("input", self._input, self._input_set),
            ("completion", self._completion, self._completion_set),
        ):
            if not was_set or counts is None:
                unavailable.append(kind)
            else:
                for name, value in counts.items():
                    self._set(f"finecode.phase.count.{name}", value)
        if unavailable:
            self._set("finecode.phase.counts_unavailable", ",".join(unavailable))
        self._span.end(end_time=self._stop_wall)

    def abort(self) -> None:
        self._span.end()

    def _set(self, key: str, value: str | int) -> None:
        try:
            self._span.set_attribute(key, value)
        except Exception:  # noqa: BLE001 - one bad attribute must not lose the span
            if not self._attrs_failed:
                self._attrs_failed = True
                self._health.count("traces", "contained_failure")

    def _elapsed(self) -> tuple[int | None, str | None]:
        if self._start_perf is None:
            return None, self._start_perf_reason or "clock_error"
        if self._stop_perf is None:
            return None, self._stop_perf_reason or "clock_error"
        delta = self._stop_perf - self._start_perf
        if delta < 0:
            return None, "negative_delta"
        return delta, None

    def _thread_cpu(self) -> tuple[int | None, str | None]:
        if self._start_thread is None:
            return None, self._start_thread_reason or "clock_error"
        if self._stop_thread is None:
            return None, self._stop_thread_reason or "clock_error"
        if self._start_ident != self._stop_ident:
            return None, "thread_changed"
        delta = self._stop_thread - self._start_thread
        if delta < 0:
            return None, "negative_delta"
        return delta, None


class _Runtime(IPhaseTimingRuntime):
    def __init__(self, tracer: typing.Any, health: HealthTracker) -> None:
        self._tracer = tracer
        self._health = health

    def open(self, label: str) -> IPhaseObservation:
        from opentelemetry import context, trace

        ambient = context.get_current()
        current = trace.get_current_span(ambient)
        parent = ambient if current.get_span_context().is_valid else None
        span = self._tracer.start_span(
            f"phase/{label}", context=parent, start_time=_read_wall_ns()
        )
        observation = _Observation(span, self._health)
        observation._set("finecode.phase.label", label)
        return observation


class OtelPhaseTimingBinding(PhaseTimingBinding):
    """Installable binding that times phases with OTel spans."""

    def __init__(self, *, health: HealthTracker | None = None) -> None:
        self._health = health if health is not None else er_telemetry._health
        self._runtime: _Runtime | None = None

    def resolve(self) -> IPhaseTimingRuntime:
        if self._runtime is None:
            from opentelemetry import trace

            tracer = trace.get_tracer("finecode.er")
            self._runtime = _Runtime(tracer, self._health)
        return self._runtime

    def report_contained_failure(self) -> None:
        self._health.count("traces", "contained_failure")
