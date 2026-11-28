"""Provider setup tests: per-signal isolation without retries or double registration.

A failure in one signal's setup must leave it ``unavailable`` with a bounded
reason while the others still activate, and a failed or completed attempt must
never run again in the same process — the OTel SDK forbids replacing providers
once set. Queue-full drops and failed exports count against the matching
signal's health; the counting path never logs.
"""

from __future__ import annotations

import collections
import collections.abc
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from finecode_extension_runner.shared_telemetry import providers
from finecode_extension_runner.shared_telemetry.health import HealthTracker


@pytest.fixture(autouse=True)
def _reset_provider_attempts() -> collections.abc.Iterator[None]:
    providers._attempted.clear()
    providers._statuses.clear()
    providers._reasons.clear()
    yield
    providers._attempted.clear()
    providers._statuses.clear()
    providers._reasons.clear()


def _tracker() -> HealthTracker:
    return HealthTracker(process_role="er")


def test_unparsable_endpoint_leaves_all_signals_unavailable() -> None:
    tracker = _tracker()

    result = providers.setup_providers(
        service_name="test",
        resource_attrs={"service.name": "test"},
        endpoint="not-an-endpoint",
        health=tracker,
    )

    assert (result.traces, result.metrics, result.logs) == (
        "unavailable",
        "unavailable",
        "unavailable",
    )
    assert result.reasons == {
        "traces": "invalid_setting",
        "metrics": "invalid_setting",
        "logs": "invalid_setting",
    }
    summary = tracker.snapshot()
    assert summary["signals"]["traces"]["availability"] == "unavailable"
    assert summary["signals"]["traces"].get("unavailable_reason") == "invalid_setting"
    # A validation failure is not an attempt: nothing was registered twice-able.
    assert providers._attempted == set()


def test_sdk_disabled_leaves_all_signals_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    tracker = _tracker()

    result = providers.setup_providers(
        service_name="test",
        resource_attrs={"service.name": "test"},
        endpoint="http://127.0.0.1:4317",
        health=tracker,
    )

    assert (result.traces, result.metrics, result.logs) == (
        "unavailable",
        "unavailable",
        "unavailable",
    )
    assert result.reasons["traces"] == "sdk_disabled"


def test_failing_signal_leaves_others_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising trace setup must not stop metrics/logs or retry later."""
    attempts: list[str] = []

    def _raise_traces(**kwargs: Any) -> str:
        attempts.append("traces")
        raise RuntimeError("boom")

    def _fake_metrics(**kwargs: Any) -> str:
        attempts.append("metrics")
        return "active"

    def _fake_logs(**kwargs: Any) -> str:
        attempts.append("logs")
        return "active"

    monkeypatch.setattr(providers, "_setup_traces", _raise_traces)
    monkeypatch.setattr(providers, "_setup_metrics", _fake_metrics)
    monkeypatch.setattr(providers, "_setup_logs", _fake_logs)
    tracker = _tracker()

    result = providers.setup_providers(
        service_name="test",
        resource_attrs={"service.name": "test"},
        endpoint="http://127.0.0.1:4317",
        health=tracker,
    )

    assert result.traces == "unavailable"
    assert result.reasons["traces"] == "provider_error"
    assert (result.metrics, result.logs) == ("active", "active")
    assert tracker.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 0

    second = providers.setup_providers(
        service_name="test",
        resource_attrs={"service.name": "test"},
        endpoint="http://127.0.0.1:4317",
        health=tracker,
    )

    assert attempts == ["traces", "metrics", "logs"]
    assert second.traces == "unavailable"


def test_import_error_maps_to_import_error_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_import(**kwargs: Any) -> str:
        raise ImportError("no module")

    def _fake_active(**kwargs: Any) -> str:
        return "active"

    monkeypatch.setattr(providers, "_setup_traces", _raise_import)
    monkeypatch.setattr(providers, "_setup_metrics", _fake_active)
    monkeypatch.setattr(providers, "_setup_logs", _fake_active)

    result = providers.setup_providers(
        service_name="test",
        resource_attrs={"service.name": "test"},
        endpoint="http://127.0.0.1:4317",
        health=_tracker(),
    )

    assert result.traces == "unavailable"
    assert result.reasons["traces"] == "import_error"


def test_failed_export_counts_s6_and_returns_failure() -> None:
    tracker = _tracker()

    class _Failing:
        def export(self, _spans: Any) -> SpanExportResult:
            return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            pass

    exporter = providers._FailureCountingSpanExporter(_Failing(), tracker)

    assert exporter.export([]) is SpanExportResult.FAILURE
    assert tracker.snapshot()["signals"]["traces"]["counts"]["export_failed"] == 1


def test_raising_export_counts_s6_and_returns_failure() -> None:
    tracker = _tracker()

    class _Raising:
        def export(self, _spans: Any) -> SpanExportResult:
            raise RuntimeError("a setup failure must never log its message")

        def shutdown(self) -> None:
            pass

    exporter = providers._FailureCountingSpanExporter(_Raising(), tracker)

    assert exporter.export([]) is SpanExportResult.FAILURE
    assert tracker.snapshot()["signals"]["traces"]["counts"]["export_failed"] == 1


def test_full_queue_counts_s5_and_drops_without_delegating() -> None:
    tracker = _tracker()
    calls: list[str] = []

    class _Delegate:
        _batch_processor = type(
            "_Batch",
            (),
            {
                "_queue": collections.deque([1], maxlen=1),
                "_max_queue_size": 1,
            },
        )()

        def on_end(self, _span: Any) -> None:
            calls.append("on_end")

    processor = providers._QueueCountingSpanProcessor(_Delegate(), tracker)
    processor.on_end(object())

    assert calls == []
    assert tracker.snapshot()["signals"]["traces"]["counts"]["queue_overflow"] == 1


def test_changed_sdk_internals_delegate_without_counting() -> None:
    tracker = _tracker()
    calls: list[str] = []

    class _Delegate:
        def on_end(self, _span: Any) -> None:
            calls.append("on_end")

    processor = providers._QueueCountingSpanProcessor(_Delegate(), tracker)
    processor.on_end(object())

    assert calls == ["on_end"]
    assert tracker.snapshot()["signals"]["traces"]["counts"]["queue_overflow"] == 0
