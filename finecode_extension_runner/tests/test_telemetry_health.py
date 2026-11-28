"""Health summaries report loss without becoming loss: one record per change,
at most one per minute, WARNING only when something was actually lost.

Each loss cause is injected through the real counting path (full queue,
failed export, sampled-out span, suppressed late record, contained failure),
and the emission is driven through the real emitter, so a summary that hides
a loss -- or a loss that spams a record -- fails here, not in production.
"""

from __future__ import annotations

import collections.abc
import typing

import pytest
from loguru import logger

from finecode_extension_runner import logs as er_logs
from finecode_extension_runner.shared_telemetry import providers as shared_providers
from finecode_extension_runner.shared_telemetry.health import (
    HealthEmitter,
    HealthTracker,
    register_health_metrics,
)

HEALTH_GROUP = "finecode_extension_runner.shared_telemetry.health"


@pytest.fixture()
def health() -> HealthTracker:
    return HealthTracker(process_role="er")


@pytest.fixture()
def captured() -> collections.abc.Iterator[list[dict[str, typing.Any]]]:
    """All loguru records, with the sink removed afterwards."""
    records: list[dict[str, typing.Any]] = []
    sink_id = logger.add(lambda message: records.append(message.record))
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _health_records(records: list[dict]) -> list[dict]:
    return [r for r in records if r["name"] == HEALTH_GROUP]


def _emit_first(health: HealthTracker) -> HealthEmitter:
    emitter = HealthEmitter(health)
    emitter.attach()
    emitter.emit_initial()
    return emitter


def test_first_summary_after_setup_reports_unavailable_without_loss(
    health: HealthTracker, captured: list[dict]
) -> None:
    """The first summary names every unavailable signal and stays INFO: an
    unavailable signal with no lost records is a status, not an incident."""
    for signal in ("traces", "metrics", "logs"):
        health.set_availability(signal, "unavailable", "invalid_setting")

    _emit_first(health)

    summaries = _health_records(captured)
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "INFO"
    assert summaries[0]["message"].startswith("telemetry health summary: ")
    assert "no evidence lost" in summaries[0]["message"]
    extras = summaries[0]["extra"]
    assert extras["health_sampler"] == "parentbased_always_on"
    assert extras["health_traces_availability"] == "unavailable"
    assert extras["health_traces_unavailable_reason"] == "invalid_setting"
    assert extras["health_traces_sampled_out"] == 0


async def test_no_summary_while_nothing_changes(
    health: HealthTracker, captured: list[dict]
) -> None:
    """A summary is evidence of change: silence means steady state."""
    emitter = _emit_first(health)
    assert len(_health_records(captured)) == 1

    emitter._schedule()
    await asyncio_sleep_zero()
    await asyncio_sleep_zero()

    assert len(_health_records(captured)) == 1


async def asyncio_sleep_zero() -> None:
    import asyncio

    await asyncio.sleep(0)


async def test_change_inside_window_emits_once_at_window_end(
    health: HealthTracker, captured: list[dict]
) -> None:
    """A change inside the minute schedules one emission at the window end,
    not a record per loss."""
    first_emit_at = None
    emitter = _emit_first(health)
    assert len(_health_records(captured)) == 1
    first_emit_at = emitter._last_emit_at

    health.count("traces", "queue_overflow")
    await asyncio_sleep_zero()
    await asyncio_sleep_zero()
    assert len(_health_records(captured)) == 1
    assert emitter._timer is not None

    health.count("traces", "queue_overflow")
    await asyncio_sleep_zero()
    await asyncio_sleep_zero()
    assert len(_health_records(captured)) == 1

    emitter._last_emit_at = first_emit_at - 61.0
    emitter._timer_fired()
    await asyncio_sleep_zero()
    assert len(_health_records(captured)) == 2
    assert "lost 2" in _health_records(captured)[-1]["message"]


def test_warning_iff_loss_or_flag(health: HealthTracker, captured: list[dict]) -> None:
    """WARNING means evidence was lost or admission degraded -- nothing else."""
    emitter = _emit_first(health)
    assert _health_records(captured)[-1]["level"].name == "INFO"

    health.set_availability("traces", "unavailable", "provider_error")
    emitter._last_emit_at -= 61.0
    emitter._recheck()
    assert _health_records(captured)[-1]["level"].name == "INFO"

    health.count("logs", "export_failed")
    emitter._last_emit_at -= 61.0
    emitter._recheck()
    assert _health_records(captured)[-1]["level"].name == "WARNING"

    fresh = HealthTracker(process_role="er")
    emitter2 = HealthEmitter(fresh)
    emitter2.attach()
    emitter2.emit_initial()
    fresh.set_flag("contention")
    emitter2._last_emit_at -= 61.0
    emitter2._recheck()
    assert _health_records(captured)[-1]["level"].name == "WARNING"


def test_queue_overflow_counts_s5_and_skips_delegate(
    health: HealthTracker,
) -> None:
    """A full batch queue drops the record and counts it, instead of growing."""

    class _Batch:
        _queue: typing.ClassVar[list[object | None]] = [None]
        _max_queue_size = 1

    class _Processor:
        _batch_processor = _Batch()

        def __init__(self) -> None:
            self.ended: list[object] = []

        def on_end(self, span: object) -> None:
            self.ended.append(span)

    delegate = _Processor()
    counting = shared_providers._QueueCountingSpanProcessor(delegate, health)
    counting.on_end(object())

    assert health.snapshot()["signals"]["traces"]["counts"]["queue_overflow"] == 1
    assert delegate.ended == []


def test_queue_overflow_logs_counts_s5(health: HealthTracker) -> None:
    """A full log batch queue drops the record and counts it."""

    class _Batch:
        _queue: typing.ClassVar[list[object | None]] = [None]
        _max_queue_size = 1

    class _Processor:
        _batch_processor = _Batch()

        def __init__(self) -> None:
            self.emitted: list[object] = []

        def on_emit(self, record: object) -> None:
            self.emitted.append(record)

    delegate = _Processor()
    counting = shared_providers._QueueCountingLogProcessor(delegate, health)
    counting.on_emit(object())

    assert health.snapshot()["signals"]["logs"]["counts"]["queue_overflow"] == 1
    assert delegate.emitted == []


def test_failed_export_counts_s6_without_raising(health: HealthTracker) -> None:
    """A failing exporter counts the loss for its signal and never raises."""

    class _Failing:
        def export(self, _batch: object) -> typing.NoReturn:
            raise RuntimeError("sink down")

    for signal, wrapper in (
        ("traces", shared_providers._FailureCountingSpanExporter),
        ("logs", shared_providers._FailureCountingLogExporter),
    ):
        counting = wrapper(_Failing(), health)
        counting.export(object())
        assert health.snapshot()["signals"][signal]["counts"]["export_failed"] == 1


def test_failed_metric_export_counts_s6(health: HealthTracker) -> None:
    """Metric export failures count S6 under metrics only."""
    from opentelemetry.sdk.metrics.export import MetricExportResult

    class _FailingResult:
        def export(self, _batch: object, **kwargs: object) -> object:
            return MetricExportResult.FAILURE

    counting = shared_providers._FailureCountingMetricExporter(_FailingResult(), health)
    counting.export(object())
    counts = health.snapshot()["signals"]["metrics"]["counts"]
    assert counts["export_failed"] == 1
    assert health.snapshot()["signals"]["traces"]["counts"]["export_failed"] == 0


def test_sampled_out_span_counts_s3(health: HealthTracker) -> None:
    """A span the sampler drops counts S3 and opens nothing."""
    from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver

    provider = TracerProvider(sampler=ALWAYS_OFF)
    real_get_tracer = otel_trace.get_tracer
    otel_trace.get_tracer = lambda name, *a, **k: provider.get_tracer(name)
    try:
        health.set_availability("traces", "active")
        observer = OtelHopObserver(role="er", health=health)
        token = observer.open_caller(
            HopIdentity(
                connection_id="c",
                role="caller",
                request_id=1,
                method="m",
            ),
            "peer",
            capture_stage_time(),
        )
    finally:
        otel_trace.get_tracer = real_get_tracer
    assert token is None
    assert health.snapshot()["signals"]["traces"]["counts"]["sampled_out"] == 1


def test_hop_cap_suppression_counts_s7(health: HealthTracker) -> None:
    """Past the active-hop cap, opens are refused and counted, never tracked."""
    from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider

    from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver

    provider = TracerProvider()
    real_get_tracer = otel_trace.get_tracer
    otel_trace.get_tracer = lambda name, *a, **k: provider.get_tracer(name)
    try:
        health.set_availability("traces", "active")
        observer = OtelHopObserver(role="er", health=health)
        tokens = [
            observer.open_caller(
                HopIdentity(connection_id="c", role="caller", request_id=i, method="m"),
                "peer",
                capture_stage_time(),
            )
            for i in range(1025)
        ]
    finally:
        otel_trace.get_tracer = real_get_tracer
    assert tokens[-1] is None
    assert all(token is not None for token in tokens[:-1])
    assert health.snapshot()["signals"]["traces"]["counts"]["suppressed"] == 1


def test_span_creation_failure_counts_s10(health: HealthTracker) -> None:
    """A fault inside span creation is contained and counted once."""
    from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time
    from opentelemetry import trace as otel_trace

    from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver

    real_get_tracer = otel_trace.get_tracer

    def _raising(name: str, *args: object, **kwargs: object) -> typing.NoReturn:
        raise RuntimeError("tracer boom")

    otel_trace.get_tracer = _raising
    try:
        health.set_availability("traces", "active")
        observer = OtelHopObserver(role="er", health=health)
        token = observer.open_caller(
            HopIdentity(connection_id="c", role="caller", request_id=1, method="m"),
            "peer",
            capture_stage_time(),
        )
    finally:
        otel_trace.get_tracer = real_get_tracer
    assert token is None
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1


def test_raising_change_hook_does_not_break_counting(
    health: HealthTracker, captured: list[dict]
) -> None:
    """A raising change hook is suppressed; clearing it restores emission."""

    def _raise() -> typing.NoReturn:
        raise RuntimeError("hook boom")

    health.set_on_change(_raise)
    health.count("traces", "queue_overflow")

    emitter = HealthEmitter(health)
    emitter.attach()
    emitter.emit_initial()
    summaries = _health_records(captured)
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "WARNING"


def test_disabled_process_writes_nothing(
    health: HealthTracker, captured: list[dict]
) -> None:
    """Without setup there is no emitter and no timer: losses only count."""
    health.count("traces", "queue_overflow")
    health.set_availability("traces", "unavailable", "invalid_setting")

    assert _health_records(captured) == []


def test_warning_group_hides_info_but_not_loss(
    health: HealthTracker,
) -> None:
    """When the summary group sits at WARNING, INFO summaries stay local but
    loss summaries still reach the WARNING sink."""
    group = HEALTH_GROUP
    er_logs.set_log_level_for_group(group, er_logs.LogLevel.WARNING)
    records: list[dict] = []
    sink_id = logger.add(
        lambda message: records.append(message.record),
        level="TRACE",
        filter=er_logs.filter_logs,
    )
    try:
        emitter = _emit_first(health)
        assert _health_records(records) == []

        health.count("logs", "export_failed")
        emitter._last_emit_at -= 61.0
        emitter._recheck()
        summaries = _health_records(records)
        assert len(summaries) == 1
        assert summaries[0]["level"].name == "WARNING"
    finally:
        logger.remove(sink_id)
        er_logs.reset_log_level_for_group(group)


def test_metric_mirror_counts_and_availability(health: HealthTracker) -> None:
    """The metric mirror reports per-signal loss counts and availability."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register_health_metrics(provider.get_meter("test"), health)

    health.set_availability("traces", "active")
    health.count("traces", "queue_overflow")
    health.count("traces", "queue_overflow")
    health.count("logs", "export_failed")

    points: dict[tuple[str, tuple], float] = {}
    for resource_metrics in reader.get_metrics_data().resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                for point in metric.data.data_points:
                    key = (metric.name, tuple(sorted(point.attributes.items())))
                    points[key] = point.value

    assert (
        points[
            (
                "finecode.telemetry.evidence_loss",
                (("cause", "queue_overflow"), ("signal", "traces")),
            )
        ]
        == 2
    )
    assert (
        points[
            (
                "finecode.telemetry.evidence_loss",
                (("cause", "export_failed"), ("signal", "logs")),
            )
        ]
        == 1
    )
    assert points[("finecode.telemetry.signal_available", (("signal", "traces"),))] == 1
    assert (
        points[("finecode.telemetry.signal_available", (("signal", "metrics"),))] == 0
    )
