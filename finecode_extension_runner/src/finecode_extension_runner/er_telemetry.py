import contextlib
import time
from pathlib import Path

_handler_duration_hist = None
_handler_errors_counter = None


def apply_telemetry_config(
    config: dict,
    project_path: Path,
    env_name: str | None = None,
) -> None:
    endpoint: str | None = config.get("otlp_endpoint") or None
    if not endpoint:
        return
    service_name = f"finecode.er.{env_name}" if env_name else "finecode.er"
    init_otel_logging(service_name=service_name, project_path=project_path, endpoint=endpoint)
    init_tracer_provider(service_name=service_name, project_path=project_path, endpoint=endpoint)
    init_meter_provider(service_name=service_name, project_path=project_path, endpoint=endpoint)


def init_otel_logging(service_name: str, project_path: Path, endpoint: str) -> None:
    import importlib.metadata

    from loguru import logger
    from opentelemetry._logs.severity import SeverityNumber
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    from finecode_extension_runner.logs import filter_logs

    try:
        version = importlib.metadata.version("finecode_extension_runner")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.version": version,
        "project.path": str(project_path),
    }

    resource = Resource.create(resource_attrs)
    provider = LoggerProvider(resource=resource)
    insecure = not endpoint.startswith("https://")
    exporter = OTLPLogExporter(endpoint=endpoint, insecure=insecure)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

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

    def _otel_sink(message) -> None:
        rec = message.record
        sev = _severity_map.get(rec["level"].name, SeverityNumber.UNSPECIFIED)
        otel_logger.emit(
            timestamp=int(rec["time"].timestamp() * 1e9),
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

    logger.add(_otel_sink, level="TRACE", filter=filter_logs)


def init_tracer_provider(service_name: str, project_path: Path, endpoint: str) -> None:
    import importlib.metadata

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    try:
        version = importlib.metadata.version("finecode_extension_runner")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.version": version,
        "project.path": str(project_path),
    }

    resource = Resource.create(resource_attrs)
    provider = TracerProvider(resource=resource)
    insecure = not endpoint.startswith("https://")
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def init_meter_provider(service_name: str, project_path: Path, endpoint: str) -> None:
    global _handler_duration_hist, _handler_errors_counter

    import importlib.metadata

    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    try:
        version = importlib.metadata.version("finecode_extension_runner")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.version": version,
        "project.path": str(project_path),
    }

    resource = Resource.create(resource_attrs)
    insecure = not endpoint.startswith("https://")
    exporter = OTLPMetricExporter(endpoint=endpoint, insecure=insecure)
    reader = PeriodicExportingMetricReader(exporter)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)

    meter = provider.get_meter("finecode.er")
    _handler_duration_hist = meter.create_histogram(
        "finecode.handler.duration",
        unit="s",
        description="Duration of action handler execution",
    )
    _handler_errors_counter = meter.create_counter(
        "finecode.handler.errors",
        description="Number of action handler execution errors",
    )


@contextlib.contextmanager
def handler_span(handler_name: str, action_name: str, wal_run_id: str | None):
    if wal_run_id is None:
        yield None
        return

    import uuid

    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    tracer = trace.get_tracer("finecode.er")
    trace_id = int(uuid.UUID(wal_run_id))
    parent_ctx = trace.set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=trace_id,
                span_id=0x0000000000000001,
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )
    with tracer.start_as_current_span(
        "handler.run",
        context=parent_ctx,
        attributes={
            "handler.name": handler_name,
            "action.name": action_name,
            "wal.run_id": wal_run_id,
        },
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


@contextlib.contextmanager
def handler_metrics(handler_name: str, action_name: str):
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        if _handler_errors_counter is not None:
            _handler_errors_counter.add(
                1,
                {
                    "handler.name": handler_name,
                    "action.name": action_name,
                    "error.type": type(exc).__name__,
                },
            )
        raise
    finally:
        if _handler_duration_hist is not None:
            _handler_duration_hist.record(
                time.perf_counter() - start,
                {"handler.name": handler_name, "action.name": action_name},
            )
