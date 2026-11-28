"""Phase spans must record elapsed and thread CPU separately, attribute
counts explicitly, and never change the work being timed.

Each span carries the outcome, the elapsed/thread-CPU split with a reason
whenever a reading is unavailable, and the input/completion counts. A fault
inside the timing (span creation, attribute recording, span end) is counted
once and leaves the body's result or exception unchanged.
"""

from __future__ import annotations

import collections.abc
import unittest.mock as mock

import pytest
from finecode_extension_api import phase_timing
from finecode_extension_api.phase_timing_runtime import set_phase_timing_binding

from finecode_extension_runner.shared_telemetry import phase_spans
from finecode_extension_runner.shared_telemetry.health import HealthTracker


@pytest.fixture()
def telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> collections.abc.Iterator[tuple[object, HealthTracker, object]]:
    """A real binding wired to an in-memory exporter and a fresh tracker."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    health = HealthTracker(process_role="er")
    binding = phase_spans.OtelPhaseTimingBinding(health=health)
    monkeypatch.setattr(
        otel_trace,
        "get_tracer",
        lambda name, *args, **kwargs: provider.get_tracer(name),
    )
    previous = phase_timing.get_phase_timing_binding()
    set_phase_timing_binding(binding)
    try:
        yield binding, health, exporter
    finally:
        set_phase_timing_binding(previous)


def _span(exporter, index: int = 0):
    spans = exporter.get_finished_spans()
    assert len(spans) > index
    return spans[index]


def test_happy_path_span_attributes(telemetry) -> None:
    """A timed phase records outcome, the elapsed/CPU split and its counts."""
    _binding, _health, exporter = telemetry
    scope = phase_timing.synchronous_phase(
        "render_config_dump", input_counts=lambda: {"top_level_keys": 3}
    )
    with scope:
        scope.completion_counts(lambda: {"output_chars": 10})

    span = _span(exporter)
    assert span.name == "phase/render_config_dump"
    attrs = dict(span.attributes or {})
    assert attrs["finecode.phase.label"] == "render_config_dump"
    assert attrs["finecode.phase.outcome"] == "returned"
    assert isinstance(attrs["finecode.phase.elapsed_ns"], int)
    assert attrs["finecode.phase.elapsed_ns"] >= 0
    assert isinstance(attrs["finecode.phase.thread_cpu_ns"], int)
    assert attrs["finecode.phase.thread_cpu_ns"] >= 0
    assert attrs["finecode.phase.completion"] == "available"
    assert attrs["finecode.phase.count.top_level_keys"] == 3
    assert attrs["finecode.phase.count.output_chars"] == 10
    assert "finecode.phase.counts_unavailable" not in attrs


def test_raised_outcome_recorded(telemetry) -> None:
    """A raising body keeps its exception and records the raised outcome."""
    _binding, _health, exporter = telemetry
    marker = RuntimeError("original")
    with pytest.raises(RuntimeError) as exc_info:
        with phase_timing.synchronous_phase("render"):
            raise marker
    assert exc_info.value is marker
    assert _span(exporter).attributes["finecode.phase.outcome"] == "raised"


def test_cancelled_outcome_recorded(telemetry) -> None:
    """Cancellation propagates and records the cancelled outcome."""
    import asyncio

    _binding, _health, exporter = telemetry
    with pytest.raises(asyncio.CancelledError):
        with phase_timing.synchronous_phase("render"):
            raise asyncio.CancelledError()
    assert _span(exporter).attributes["finecode.phase.outcome"] == "cancelled"


def test_missing_counts_are_explicitly_unavailable(telemetry) -> None:
    """Counts never set are named as unavailable, never zero."""
    _binding, _health, exporter = telemetry
    with phase_timing.synchronous_phase("render"):
        pass
    attrs = dict(_span(exporter).attributes or {})
    assert attrs["finecode.phase.completion"] == "unavailable"
    assert attrs["finecode.phase.counts_unavailable"] == "input,completion"
    assert "finecode.phase.count.top_level_keys" not in attrs


def test_resolve_failure_counts_once_and_leaves_no_span(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime that cannot be constructed counts once; the body still runs."""
    binding, health, exporter = telemetry
    monkeypatch.setattr(
        binding, "resolve", lambda: (_ for _ in ()).throw(RuntimeError("no runtime"))
    )
    with phase_timing.synchronous_phase("render"):
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert len(exporter.get_finished_spans()) == 0
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1


def test_open_failure_counts_once_and_leaves_no_span(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Span creation failing counts once and opens nothing."""
    binding, health, exporter = telemetry
    runtime = binding.resolve()

    def _raise(label: str):
        raise RuntimeError("start_span boom")

    monkeypatch.setattr(runtime, "open", _raise)
    with phase_timing.synchronous_phase("render"):
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert len(exporter.get_finished_spans()) == 0
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1


def test_span_end_failure_counts_once(telemetry) -> None:
    """A failing span end still propagates through the scope's containment."""
    from opentelemetry.sdk.trace import Span

    binding, health, exporter = telemetry

    def _raise_end(self, end_time=None):
        raise RuntimeError("end boom")

    with mock.patch.object(Span, "end", _raise_end):
        with phase_timing.synchronous_phase("render"):
            outcome = "body-ran"
    assert outcome == "body-ran"
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1


def test_attribute_failure_still_ends_span(telemetry) -> None:
    """Bad attributes are contained one at a time; the span still ends."""
    from opentelemetry.sdk.trace import Span

    _binding, health, exporter = telemetry

    def _raise_attr(self, key, value=None):
        raise RuntimeError("attr boom")

    with mock.patch.object(Span, "set_attribute", _raise_attr):
        with phase_timing.synchronous_phase("render"):
            pass
    assert len(exporter.get_finished_spans()) == 1
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1


def test_unavailable_thread_clock_reports_reason_never_zero(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing thread clock omits the CPU reading with a reason."""
    _binding, _health, exporter = telemetry
    monkeypatch.setattr(phase_spans, "_thread_time_ns", None)
    with phase_timing.synchronous_phase("render"):
        pass
    attrs = dict(_span(exporter).attributes or {})
    assert "finecode.phase.thread_cpu_ns" not in attrs
    assert attrs["finecode.phase.thread_cpu_unavailable_reason"] == "clock_unavailable"
    assert isinstance(attrs["finecode.phase.elapsed_ns"], int)


def test_failing_thread_clock_reports_error(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising thread clock omits the CPU reading with an error reason."""
    _binding, _health, exporter = telemetry

    def _raise() -> int:
        raise RuntimeError("clock boom")

    monkeypatch.setattr(phase_spans, "_thread_time_ns", _raise)
    with phase_timing.synchronous_phase("render"):
        pass
    attrs = dict(_span(exporter).attributes or {})
    assert "finecode.phase.thread_cpu_ns" not in attrs
    assert attrs["finecode.phase.thread_cpu_unavailable_reason"] == "clock_error"


def test_thread_change_reports_reason(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread hop inside the phase invalidates the CPU attribution."""
    import threading

    _binding, _health, exporter = telemetry
    real_ident = threading.get_ident()
    calls = {"n": 0}

    def _fake_ident() -> int:
        calls["n"] += 1
        return 1 if calls["n"] == 1 else real_ident

    monkeypatch.setattr(threading, "get_ident", _fake_ident)
    with phase_timing.synchronous_phase("render"):
        pass
    attrs = dict(_span(exporter).attributes or {})
    assert "finecode.phase.thread_cpu_ns" not in attrs
    assert attrs["finecode.phase.thread_cpu_unavailable_reason"] == "thread_changed"


def test_negative_cpu_delta_reports_reason(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backwards thread clock is reported, never stored."""
    _binding, _health, exporter = telemetry
    readings = iter([200, 100])
    monkeypatch.setattr(phase_spans, "_thread_time_ns", lambda: next(readings))
    with phase_timing.synchronous_phase("render"):
        pass
    attrs = dict(_span(exporter).attributes or {})
    assert "finecode.phase.thread_cpu_ns" not in attrs
    assert attrs["finecode.phase.thread_cpu_unavailable_reason"] == "negative_delta"


def test_genuine_zero_cpu_is_kept(telemetry, monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero CPU delta is a real measurement, not a missing one."""
    _binding, _health, exporter = telemetry
    monkeypatch.setattr(phase_spans, "_thread_time_ns", lambda: 100)
    with phase_timing.synchronous_phase("render"):
        pass
    attrs = dict(_span(exporter).attributes or {})
    assert attrs["finecode.phase.thread_cpu_ns"] == 0
    assert "finecode.phase.thread_cpu_unavailable_reason" not in attrs


def test_ended_parent_is_allowed(telemetry) -> None:
    """A phase started under an ended span still parents to it."""
    from opentelemetry import context, trace

    _binding, _health, exporter = telemetry
    tracer = trace.get_tracer("test-parent")
    with tracer.start_as_current_span("parent") as parent:
        parent_context = context.get_current()
    parent_id = parent.get_span_context().span_id
    token = context.attach(parent_context)
    try:
        with phase_timing.synchronous_phase("render"):
            pass
    finally:
        context.detach(token)
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    span = by_name["phase/render"]
    assert span.parent is not None
    assert span.parent.span_id == parent_id


def test_no_valid_context_starts_a_root(telemetry) -> None:
    """With no ambient span the phase is a root, not a broken child."""
    _binding, _health, exporter = telemetry
    with phase_timing.synchronous_phase("render"):
        pass
    assert _span(exporter).parent is None


def test_construction_failure_is_retried_next_scope(
    telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed construction is not cached: the next scope tries again."""
    from opentelemetry import trace as otel_trace

    binding, health, exporter = telemetry
    attempts = {"n": 0}
    provider_tracer = otel_trace.get_tracer("finecode.er")

    def _flaky(_name: str, *args: object, **kwargs: object) -> object:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("provider boom")
        return provider_tracer

    monkeypatch.setattr(otel_trace, "get_tracer", _flaky)
    with phase_timing.synchronous_phase("render"):
        pass
    assert len(exporter.get_finished_spans()) == 0
    with phase_timing.synchronous_phase("render"):
        pass
    assert len(exporter.get_finished_spans()) == 1


def test_report_counts_s10(telemetry) -> None:
    """The binding's report path counts under traces."""
    binding, health, _exporter = telemetry
    binding.report_contained_failure()
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1
