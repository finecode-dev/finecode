"""WM health summaries: first summary after setup, change-triggered emission,
per-ER setup accounting, and the S3/S5/S6/S7/S10 injections under WM signals.

The emission mechanics are shared with the ER; these tests pin the WM side:
the per-ER category counts, the INFO record for incomplete ER setups, and
loss injection through the WM's observer and wrappers.
"""

from __future__ import annotations

import collections.abc
import typing
from types import SimpleNamespace

import pytest
from finecode_extension_runner.shared_telemetry import providers as shared_providers
from finecode_extension_runner.shared_telemetry.health import (
    HealthEmitter,
    HealthTracker,
    register_health_metrics,
)
from loguru import logger

from finecode import telemetry as wm_telemetry
from finecode.wm_server.runner import runner_manager
from finecode_extension_runner import logs as shared_logs

HEALTH_GROUP = "finecode_extension_runner.shared_telemetry.health"


@pytest.fixture()
def health(monkeypatch: pytest.MonkeyPatch) -> HealthTracker:
    """A fresh WM tracker installed as the process tracker."""
    fresh = HealthTracker(
        process_role="wm", read_log_levels=wm_telemetry._required_group_levels
    )
    monkeypatch.setattr(wm_telemetry, "_health", fresh)
    monkeypatch.setattr(wm_telemetry, "_emitter", None)
    return fresh


@pytest.fixture()
def captured() -> collections.abc.Iterator[list[dict[str, typing.Any]]]:
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


def _fake_runner(traces: str, metrics: str, logs: str) -> typing.Any:
    return SimpleNamespace(
        readable_id="runner-e1",
        telemetry_setup=SimpleNamespace(traces=traces, metrics=metrics, logs=logs),
        telemetry_setup_category=None,
    )


def test_first_summary_reports_unavailable_without_loss(
    health: HealthTracker, captured: list[dict]
) -> None:
    """The first WM summary names every unavailable signal and stays INFO."""
    for signal in ("traces", "metrics", "logs"):
        health.set_availability(signal, "unavailable", "sdk_disabled")

    _emit_first(health)

    summaries = _health_records(captured)
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "INFO"
    assert "no evidence lost" in summaries[0]["message"]
    extras = summaries[0]["extra"]
    assert extras["health_er_setup_all_active"] == 0
    assert extras["health_er_setup_disabled"] == 0


def test_summary_has_no_per_er_detail(health: HealthTracker) -> None:
    """The WM summary carries fixed-size ER category counts, never per-ER detail."""
    health.note_er_setup("partly_unavailable")
    summary = health.snapshot()

    assert set(summary["signals"]) == {"traces", "metrics", "logs"}
    assert summary["er_setup_counts"] == {
        "all_active": 0,
        "partly_unavailable": 1,
        "all_unavailable": 0,
        "disabled": 0,
    }


def test_per_er_info_only_for_unavailable(
    health: HealthTracker, captured: list[dict]
) -> None:
    """One INFO record per ER start with an unavailable signal; silence for
    disabled or fully active setups."""
    _emit_first(health)
    assert len(_health_records(captured)) == 1

    runner_manager._note_er_telemetry_setup(_fake_runner("active", "active", "active"))
    runner_manager._note_er_telemetry_setup(
        _fake_runner("disabled", "disabled", "disabled")
    )
    assert len(_health_records(captured)) == 1

    runner_manager._note_er_telemetry_setup(
        _fake_runner("active", "unavailable", "active")
    )
    assert len(_health_records(captured)) == 1
    incomplete = [
        r for r in captured if r["message"] == "ER telemetry setup incomplete"
    ]
    assert len(incomplete) == 1
    assert incomplete[0]["extra"]["runner_id"] == "runner-e1"
    assert incomplete[0]["extra"]["metrics"] == "unavailable"

    assert health.snapshot()["er_setup_counts"] == {
        "all_active": 1,
        "partly_unavailable": 1,
        "all_unavailable": 0,
        "disabled": 1,
    }


def test_release_on_stop_and_abandon(health: HealthTracker) -> None:
    """The ER category count is released when the runner stops or is abandoned."""
    runner = _fake_runner("active", "unavailable", "active")
    runner_manager._note_er_telemetry_setup(runner)
    assert health.snapshot()["er_setup_counts"]["partly_unavailable"] == 1

    runner_manager._release_er_telemetry_setup(runner)
    assert health.snapshot()["er_setup_counts"]["partly_unavailable"] == 0

    runner_manager._release_er_telemetry_setup(runner)
    assert health.snapshot()["er_setup_counts"]["partly_unavailable"] == 0


def test_sampled_out_parent_counts_s3(health: HealthTracker) -> None:
    """An unsampled incoming parent isolates the receiver and counts S3."""
    from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
    from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    provider = TracerProvider(sampler=ALWAYS_OFF)
    real_get_tracer = otel_trace.get_tracer
    otel_trace.get_tracer = lambda name, *a, **k: provider.get_tracer(name)
    try:
        health.set_availability("traces", "active")
        observer = OtelHopObserver(role="wm", health=health)
        token = observer.open_receiver(
            HopIdentity(connection_id="c", role="receiver", request_id=1, method="m"),
            None,
            capture_stage_time(),
        )
    finally:
        otel_trace.get_tracer = real_get_tracer
    assert token is None
    assert health.snapshot()["signals"]["traces"]["counts"]["sampled_out"] == 1


def test_always_off_sampler_named_in_summary(
    health: HealthTracker, captured: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `OTEL_TRACES_SAMPLER=always_off`, summaries name that sampler."""
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    _emit_first(health)

    summaries = _health_records(captured)
    assert len(summaries) == 1
    assert summaries[0]["extra"]["health_sampler"] == "always_off"


def test_filtered_group_shown_as_level(
    health: HealthTracker, captured: list[dict]
) -> None:
    """An INFO-filtered group is visible as a level, not as missing data."""
    shared_logs.set_log_level_for_group(HEALTH_GROUP, shared_logs.LogLevel.WARNING)
    try:
        _emit_first(health)
    finally:
        shared_logs.reset_log_level_for_group(HEALTH_GROUP)

    summaries = _health_records(captured)
    assert len(summaries) == 1
    assert f"{HEALTH_GROUP}=WARNING" in summaries[0]["extra"]["health_log_levels"]


def test_queue_overflow_and_failed_export_count_s5_s6(health: HealthTracker) -> None:
    """Queue-full drops count S5 and failed exports count S6, without raising."""

    class _Batch:
        _queue: typing.ClassVar[list[object | None]] = [None]
        _max_queue_size = 1

    class _Processor:
        _batch_processor = _Batch()

        def on_end(self, span: object) -> None:
            raise AssertionError("delegate must not run when full")

    shared_providers._QueueCountingSpanProcessor(_Processor(), health).on_end(object())
    assert health.snapshot()["signals"]["traces"]["counts"]["queue_overflow"] == 1

    class _Failing:
        def export(self, spans: object) -> typing.NoReturn:
            raise RuntimeError("sink down")

    shared_providers._FailureCountingSpanExporter(_Failing(), health).export(object())
    assert health.snapshot()["signals"]["traces"]["counts"]["export_failed"] == 1


def test_hop_cap_suppression_counts_s7(health: HealthTracker) -> None:
    """Past the active-hop cap, the WM observer refuses and counts S7."""
    from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
    from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    real_get_tracer = otel_trace.get_tracer
    otel_trace.get_tracer = lambda name, *a, **k: provider.get_tracer(name)
    try:
        health.set_availability("traces", "active")
        observer = OtelHopObserver(role="wm", health=health)
        last = None
        for i in range(1025):
            last = observer.open_caller(
                HopIdentity(connection_id="c", role="caller", request_id=i, method="m"),
                "peer",
                capture_stage_time(),
            )
    finally:
        otel_trace.get_tracer = real_get_tracer
    assert last is None
    assert health.snapshot()["signals"]["traces"]["counts"]["suppressed"] == 1


def test_span_creation_failure_counts_s10(health: HealthTracker) -> None:
    """A fault inside WM span creation is contained and counted once."""
    from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
    from finecode_jsonrpc.tracing import HopIdentity, capture_stage_time
    from opentelemetry import trace as otel_trace

    real_get_tracer = otel_trace.get_tracer

    def _raising(name: str, *args: object, **kwargs: object) -> typing.NoReturn:
        raise RuntimeError("tracer boom")

    otel_trace.get_tracer = _raising
    try:
        health.set_availability("traces", "active")
        observer = OtelHopObserver(role="wm", health=health)
        token = observer.open_caller(
            HopIdentity(connection_id="c", role="caller", request_id=1, method="m"),
            "peer",
            capture_stage_time(),
        )
    finally:
        otel_trace.get_tracer = real_get_tracer
    assert token is None
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1


def test_metric_mirror_wm_role(health: HealthTracker) -> None:
    """The mirror omits the lag monitor for the WM role."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register_health_metrics(provider.get_meter("test"), health)

    names: set[str] = set()
    for resource_metrics in reader.get_metrics_data().resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                names.add(metric.name)
                for point in metric.data.data_points:
                    assert point.attributes.get("signal") in (
                        "traces",
                        "metrics",
                        "logs",
                    )
    assert "finecode.telemetry.evidence_loss" in names
    assert "finecode.telemetry.signal_available" in names


def test_emit_initial_attaches_registers_and_writes(
    health: HealthTracker,
    captured: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setup-completion hook attaches the emitter, mirrors metrics when
    active, and writes the first summary."""
    from opentelemetry import metrics as otel_metrics

    monkeypatch.setattr(wm_telemetry, "_emitter", None)
    meters: list[object] = []
    real_get_meter = otel_metrics.get_meter
    monkeypatch.setattr(
        otel_metrics,
        "get_meter",
        lambda name, *a, **k: meters.append(name) or real_get_meter(name),
    )
    health.set_availability("metrics", "active")

    wm_telemetry.emit_health_summary_initial()

    assert wm_telemetry._emitter is not None
    assert "finecode.telemetry" in meters
    summaries = _health_records(captured)
    assert len(summaries) == 1
    assert "no evidence lost" in summaries[0]["message"]
    wm_telemetry._emitter.detach()
    monkeypatch.setattr(wm_telemetry, "_emitter", None)


def test_init_logger_without_endpoint_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an endpoint there is no setup attempt and no summary."""
    from finecode import logger_utils

    calls: list[None] = []
    monkeypatch.setattr(
        wm_telemetry, "emit_health_summary_initial", lambda: calls.append(None)
    )
    monkeypatch.setattr(
        logger_utils.logs,
        "save_logs_to_file",
        lambda *args, **kwargs: "/tmp/test-wm.log",
    )
    import logging as _logging

    monkeypatch.setattr(_logging, "basicConfig", lambda *args, **kwargs: None)
    monkeypatch.setattr(logger, "remove", lambda *args, **kwargs: None)

    logger_utils.init_logger("test-no-telemetry", otlp_endpoint=None)

    assert calls == []
