import contextlib
import time
from pathlib import Path

# Metric instruments — populated by init_meter_provider(); None when OTel is disabled.
_action_duration_hist = None
_action_errors_counter = None
_er_startup_hist = None
_er_active_counter = None


def init_otel_logging(service_name: str, workspace_path: Path | None = None, endpoint: str | None = None) -> None:
    if not endpoint:
        return

    import importlib.metadata

    from loguru import logger
    from opentelemetry._logs.severity import SeverityNumber
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    from finecode_extension_runner.logs import filter_logs

    try:
        version = importlib.metadata.version("finecode")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.version": version,
    }
    if workspace_path is not None:
        resource_attrs["workspace.path"] = str(workspace_path)

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


def init_tracer_provider(service_name: str, workspace_path: Path | None = None, endpoint: str | None = None) -> None:
    if not endpoint:
        return

    import importlib.metadata

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    try:
        version = importlib.metadata.version("finecode")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.version": version,
    }
    if workspace_path is not None:
        resource_attrs["workspace.path"] = str(workspace_path)

    resource = Resource.create(resource_attrs)
    provider = TracerProvider(resource=resource)
    insecure = not endpoint.startswith("https://")
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def init_meter_provider(service_name: str, workspace_path: Path | None = None, endpoint: str | None = None) -> None:
    global _action_duration_hist, _action_errors_counter, _er_startup_hist, _er_active_counter

    if not endpoint:
        return

    import importlib.metadata

    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    try:
        version = importlib.metadata.version("finecode")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.version": version,
    }
    if workspace_path is not None:
        resource_attrs["workspace.path"] = str(workspace_path)

    resource = Resource.create(resource_attrs)
    insecure = not endpoint.startswith("https://")
    exporter = OTLPMetricExporter(endpoint=endpoint, insecure=insecure)
    reader = PeriodicExportingMetricReader(exporter)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)

    meter = provider.get_meter("finecode.wm")
    _action_duration_hist = meter.create_histogram(
        "finecode.action.duration",
        unit="s",
        description="Duration of action execution",
    )
    _action_errors_counter = meter.create_counter(
        "finecode.action.errors",
        description="Number of action execution errors",
    )
    _er_startup_hist = meter.create_histogram(
        "finecode.er.startup_duration",
        unit="s",
        description="Duration of extension runner startup",
    )
    _er_active_counter = meter.create_up_down_counter(
        "finecode.er.active",
        description="Number of active extension runners",
    )


@contextlib.contextmanager
def action_metrics(action_name: str, project_name: str):
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        if _action_errors_counter is not None:
            _action_errors_counter.add(
                1, {"action.name": action_name, "error.type": type(exc).__name__}
            )
        raise
    finally:
        if _action_duration_hist is not None:
            _action_duration_hist.record(
                time.perf_counter() - start,
                {"action.name": action_name, "project.name": project_name},
            )


@contextlib.contextmanager
def er_startup_metrics(env_name: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        if _er_startup_hist is not None:
            _er_startup_hist.record(
                time.perf_counter() - start, {"env.name": env_name}
            )


def er_active_inc(env_name: str) -> None:
    if _er_active_counter is not None:
        _er_active_counter.add(1, {"env.name": env_name})


def er_active_dec(env_name: str) -> None:
    if _er_active_counter is not None:
        _er_active_counter.add(-1, {"env.name": env_name})


@contextlib.contextmanager
def action_run_span(action_name: str, project_path: Path | str, wal_run_id: str):
    import uuid

    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    tracer = trace.get_tracer("finecode.wm")
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
        "action.run",
        context=parent_ctx,
        attributes={
            "action.name": action_name,
            "project.path": str(project_path),
            "wal.run_id": wal_run_id,
        },
    ) as span:
        yield span


@contextlib.contextmanager
def runner_start_span(env_name: str):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.wm")
    with tracer.start_as_current_span(
        "action.runner_start",
        attributes={"env.name": env_name},
    ) as span:
        yield span


@contextlib.contextmanager
def er_dispatch_span(env_name: str, runner_id: str):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.wm")
    with tracer.start_as_current_span(
        "action.er_dispatch",
        attributes={"env.name": env_name, "runner.id": runner_id},
    ) as span:
        yield span
