"""OTel provider setup shared by the WM and ER processes.

Each signal (traces, metrics, logs) is set up independently: an ordinary
``Exception`` from one leaves it ``unavailable`` and does not stop the others.
No signal is registered twice per process — one-attempt flags are set before
the attempt, and there is no retry. Queue-full drops increment S5 and failed
exports increment S6 for the matching signal (metrics: S6 only).

Wrappers never log: the counting path must not re-enter the OTLP log sink.
Setup failures record only the :class:`SetupFailureKind`, never the endpoint
value, a message or a traceback.

OTel imports stay function-local so merely importing this module (and the
telemetry-off path through it) never pays the SDK import cost.
"""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import os
import threading
import urllib.parse
from typing import Any, cast

from finecode_extension_runner.shared_telemetry.health import (
    HealthTracker,
    SetupFailureKind,
    SignalSetupStatus,
    TelemetrySignal,
)

__all__ = ["ProviderSetupResult", "setup_providers"]

_SIGNALS: tuple[TelemetrySignal, ...] = ("traces", "metrics", "logs")


@dataclasses.dataclass(frozen=True)
class ProviderSetupResult:
    traces: SignalSetupStatus
    metrics: SignalSetupStatus
    logs: SignalSetupStatus
    reasons: collections.abc.Mapping[TelemetrySignal, SetupFailureKind]


_attempted: set[TelemetrySignal] = set()
_statuses: dict[TelemetrySignal, SignalSetupStatus] = {}
_reasons: dict[TelemetrySignal, SetupFailureKind] = {}
_setup_lock = threading.Lock()


def _sdk_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true"


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Split an OTLP endpoint into (host, port), raising without the value."""
    parsed = urllib.parse.urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Invalid OTLP endpoint: expected host and port")
    return parsed.hostname, parsed.port


def _batch_queue_full(processor: Any) -> bool:
    """Whether the SDK batch processor's queue is full (best effort).

    Reads the SDK's private batch state; raises if its shape changed, in which
    case the caller delegates without counting rather than dropping the record.
    """
    batch = processor._batch_processor
    return bool(len(batch._queue) >= batch._max_queue_size)


class _QueueCountingSpanProcessor:
    """SpanProcessor that counts queue-full drops as S5/traces."""

    def __init__(self, delegate: Any, health: HealthTracker) -> None:
        self._delegate = delegate
        self._health = health

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        self._delegate.on_start(span, parent_context)

    def _on_ending(self, span: Any) -> None:
        # Called by the SDK at span.end(); the drop decision stays in on_end.
        self._delegate._on_ending(span)

    def on_end(self, span: Any) -> None:
        try:
            full = _batch_queue_full(self._delegate)
        except Exception:  # noqa: BLE001 - SDK private queue shape may change; then delegate without counting
            full = False
        if full:
            self._health.count("traces", "queue_overflow")
            return
        self._delegate.on_end(span)

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))


class _QueueCountingLogProcessor:
    """LogRecordProcessor that counts queue-full drops as S5/logs."""

    def __init__(self, delegate: Any, health: HealthTracker) -> None:
        self._delegate = delegate
        self._health = health

    def on_emit(self, log_record: Any) -> None:
        try:
            full = _batch_queue_full(self._delegate)
        except Exception:  # noqa: BLE001 - SDK private queue shape may change; then delegate without counting
            full = False
        if full:
            self._health.count("logs", "queue_overflow")
            return
        self._delegate.on_emit(log_record)

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))


class _FailureCountingSpanExporter:
    """SpanExporter that counts failed exports as S6/traces."""

    def __init__(self, delegate: Any, health: HealthTracker) -> None:
        self._delegate = delegate
        self._health = health

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            result = self._delegate.export(spans)
        except Exception:  # noqa: BLE001 - exporter failures count as S6, never propagate
            self._health.count("traces", "export_failed")
            return SpanExportResult.FAILURE
        if result is SpanExportResult.FAILURE:
            self._health.count("traces", "export_failed")
        return result

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._delegate.shutdown()


class _FailureCountingLogExporter:
    """LogRecordExporter that counts failed exports as S6/logs."""

    def __init__(self, delegate: Any, health: HealthTracker) -> None:
        self._delegate = delegate
        self._health = health

    def export(self, batch: Any) -> Any:
        from opentelemetry.sdk._logs.export import LogRecordExportResult

        try:
            result = self._delegate.export(batch)
        except Exception:  # noqa: BLE001 - exporter failures count as S6, never propagate
            self._health.count("logs", "export_failed")
            return LogRecordExportResult.FAILURE
        if result is LogRecordExportResult.FAILURE:
            self._health.count("logs", "export_failed")
        return result

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._delegate.shutdown()


class _FailureCountingMetricExporter:
    """MetricExporter that counts failed exports as S6/metrics."""

    def __init__(self, delegate: Any, health: HealthTracker) -> None:
        self._delegate = delegate
        self._health = health

    @property
    def _preferred_temporality(self) -> Any:
        # Read by PeriodicExportingMetricReader at construction; not an export.
        return self._delegate._preferred_temporality

    @property
    def _preferred_aggregation(self) -> Any:
        # Read by PeriodicExportingMetricReader at construction; not an export.
        return self._delegate._preferred_aggregation

    def export(
        self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any
    ) -> Any:
        from opentelemetry.sdk.metrics.export import MetricExportResult

        try:
            result = self._delegate.export(
                metrics_data, timeout_millis=timeout_millis, **kwargs
            )
        except Exception:  # noqa: BLE001 - exporter failures count as S6, never propagate
            self._health.count("metrics", "export_failed")
            return MetricExportResult.FAILURE
        if result is MetricExportResult.FAILURE:
            self._health.count("metrics", "export_failed")
        return result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis=timeout_millis))
        except Exception:  # noqa: BLE001 - flush failures are export-side; report false
            return False

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        with contextlib.suppress(Exception):
            self._delegate.shutdown(timeout_millis=timeout_millis, **kwargs)


def _setup_traces(
    *, resource: Any, endpoint: str, insecure: bool, health: HealthTracker
) -> SignalSetupStatus:
    from opentelemetry import trace as otel_trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=resource)
    exporter = _FailureCountingSpanExporter(
        OTLPSpanExporter(endpoint=endpoint, insecure=insecure), health
    )
    provider.add_span_processor(
        cast(
            Any,
            _QueueCountingSpanProcessor(
                BatchSpanProcessor(cast(Any, exporter)), health
            ),
        )
    )
    otel_trace.set_tracer_provider(provider)
    return "active"


def _setup_logs(
    *,
    service_name: str,
    resource: Any,
    endpoint: str,
    insecure: bool,
    health: HealthTracker,
) -> SignalSetupStatus:
    from loguru import logger
    from opentelemetry._logs.severity import SeverityNumber
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter,
    )
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    from finecode_extension_runner.logs import filter_logs

    provider = LoggerProvider(resource=resource)
    exporter = _FailureCountingLogExporter(
        OTLPLogExporter(endpoint=endpoint, insecure=insecure), health
    )
    provider.add_log_record_processor(
        cast(
            Any,
            _QueueCountingLogProcessor(
                BatchLogRecordProcessor(cast(Any, exporter)), health
            ),
        )
    )

    otel_logger = provider.get_logger(service_name)

    _severity_map = {
        "TRACE": SeverityNumber.TRACE,
        "DEBUG": SeverityNumber.DEBUG,
        "INFO": SeverityNumber.INFO,
        "SUCCESS": SeverityNumber.INFO2,
        "WARNING": SeverityNumber.WARN,
        "ERROR": SeverityNumber.ERROR,
        "CRITICAL": SeverityNumber.FATAL,
    }

    def _otel_sink(message: Any) -> None:
        from opentelemetry import context as otel_context

        rec = message.record
        # Skip OTel's own logs to avoid a feedback loop: OTel export failure →
        # InterceptHandler → Loguru → _otel_sink → export failure → …
        if rec["name"].startswith("opentelemetry"):
            return
        sev = _severity_map.get(rec["level"].name, SeverityNumber.UNSPECIFIED)
        otel_logger.emit(
            timestamp=int(rec["time"].timestamp() * 1e9),
            context=otel_context.get_current(),
            severity_number=sev,
            severity_text=rec["level"].name,
            body=rec["message"],
            attributes={
                "logger.name": rec["name"],
                "code.filepath": rec["file"].path if rec["file"] else None,
                "code.lineno": rec["line"],
                "code.function": rec["function"],
                **rec["extra"],
            },
        )

    _ = logger.add(_otel_sink, level="TRACE", filter=filter_logs)
    return "active"


def _setup_metrics(
    *, resource: Any, endpoint: str, insecure: bool, health: HealthTracker
) -> SignalSetupStatus:
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    reader = PeriodicExportingMetricReader(
        cast(
            Any,
            _FailureCountingMetricExporter(
                OTLPMetricExporter(endpoint=endpoint, insecure=insecure), health
            ),
        )
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    _ = otel_metrics.set_meter_provider(provider)
    return "active"


def _setup_once(
    signal: TelemetrySignal,
    *,
    service_name: str,
    resource: Any,
    endpoint: str,
    insecure: bool,
    health: HealthTracker,
) -> SignalSetupStatus:
    with _setup_lock:
        if signal in _attempted:
            return _statuses[signal]
        _attempted.add(signal)
    try:
        if signal == "logs":
            status = _setup_logs(
                service_name=service_name,
                resource=resource,
                endpoint=endpoint,
                insecure=insecure,
                health=health,
            )
        elif signal == "traces":
            status = _setup_traces(
                resource=resource, endpoint=endpoint, insecure=insecure, health=health
            )
        else:
            status = _setup_metrics(
                resource=resource, endpoint=endpoint, insecure=insecure, health=health
            )
        reason: SetupFailureKind | None = None
    except ImportError:
        status = "unavailable"
        reason = "import_error"
    except Exception:  # noqa: BLE001 - one signal's setup failure stays contained, never propagates
        status = "unavailable"
        reason = "provider_error"
    with _setup_lock:
        _statuses[signal] = status
        if reason is not None:
            _reasons[signal] = reason
    health.set_availability(signal, status, reason)
    return status


def setup_providers(
    *,
    service_name: str,
    resource_attrs: collections.abc.Mapping[str, str],
    endpoint: str,
    health: HealthTracker,
) -> ProviderSetupResult:
    """Set up the three OTel signals, each isolated from the others' failures.

    Never raises and never logs: an unparsable endpoint, a disabled SDK, a
    missing import or a provider error each leave the affected signals
    ``unavailable`` with a bounded reason. No signal is attempted twice per
    process and nothing is retried.
    """
    results: dict[TelemetrySignal, SignalSetupStatus] = {}
    reasons: dict[TelemetrySignal, SetupFailureKind] = {}
    if _sdk_disabled():
        for signal in _SIGNALS:
            results[signal] = "unavailable"
            reasons[signal] = "sdk_disabled"
            health.set_availability(signal, "unavailable", "sdk_disabled")
    else:
        try:
            _ = _parse_endpoint(endpoint)
        except ValueError:
            for signal in _SIGNALS:
                results[signal] = "unavailable"
                reasons[signal] = "invalid_setting"
                health.set_availability(signal, "unavailable", "invalid_setting")
        else:
            from opentelemetry.sdk.resources import Resource

            resource = Resource.create(
                {
                    **resource_attrs,
                    "finecode.process.incarnation": health.process_incarnation,
                }
            )
            insecure = not endpoint.startswith("https://")
            for signal in _SIGNALS:
                results[signal] = _setup_once(
                    signal,
                    service_name=service_name,
                    resource=resource,
                    endpoint=endpoint,
                    insecure=insecure,
                    health=health,
                )
                with _setup_lock:
                    if signal in _reasons:
                        reasons[signal] = _reasons[signal]
    return ProviderSetupResult(
        traces=results["traces"],
        metrics=results["metrics"],
        logs=results["logs"],
        reasons=reasons,
    )
