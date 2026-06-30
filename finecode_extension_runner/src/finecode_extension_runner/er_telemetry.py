import contextlib
import time
from pathlib import Path

_handler_duration_hist = None
_handler_errors_counter = None
_telemetry_initialized = False


def apply_telemetry_config(
    config: dict,
    project_path: Path,
    env_name: str | None = None,
) -> None:
    # OTel SDK forbids replacing a TracerProvider/MeterProvider once set, so
    # telemetry can only be configured once per process lifetime.
    global _telemetry_initialized
    if _telemetry_initialized:
        return
    endpoint: str | None = config.get("otlp_endpoint") or None
    if not endpoint:
        return
    service_name = f"finecode.er.{env_name}" if env_name else "finecode.er"
    init_otel_logging(service_name=service_name, project_path=project_path, endpoint=endpoint)
    init_tracer_provider(service_name=service_name, project_path=project_path, endpoint=endpoint)
    init_meter_provider(service_name=service_name, project_path=project_path, endpoint=endpoint)
    _telemetry_initialized = True


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


def get_current_traceparent() -> str | None:
    """Return the W3C traceparent header for the currently active span, or None if no active span."""
    if not _telemetry_initialized:
        return None
    from opentelemetry import propagate, trace

    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


@contextlib.contextmanager
def handler_span(
    handler_name: str,
    action_name: str,
    traceparent: str | None,
    trigger: str = "unknown",
    dev_env: str = "unknown",
):
    # traceparent is passed explicitly from the action payload (options.traceparent),
    # not taken from the ambient OTel context.  This is intentional: JsonRpcServerSession
    # is a long-running, concurrent session whose event loop has no per-request span
    # context, so the ambient context at call time is not the correct parent.
    # options.traceparent carries the action_run_span identity explicitly across
    # WM → ER → WM → ER hops, which ambient context propagation cannot do.
    if traceparent is None:
        yield None
        return

    from opentelemetry import propagate, trace

    tracer = trace.get_tracer("finecode.er")
    parent_ctx = propagate.extract({"traceparent": traceparent})
    with tracer.start_as_current_span(
        f"handler.run/{handler_name}",
        context=parent_ctx,
        attributes={
            "handler.name": handler_name,
            "action.name": action_name,
            "run.trigger": trigger,
            "run.dev_env": dev_env,
        },
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


@contextlib.contextmanager
def handler_initialize_span(handler_name: str, action_name: str):
    """Span for handler cold-start initialization — must be entered within a handler_span context.

    Emits no span when telemetry is off or when there is no active parent span
    (i.e. handler_span was a no-op because traceparent was absent).
    """
    if not _telemetry_initialized:
        yield None
        return
    from opentelemetry import trace
    if not trace.get_current_span().get_span_context().is_valid:
        yield None
        return
    tracer = trace.get_tracer("finecode.er")
    with tracer.start_as_current_span(
        f"handler.initialize/{handler_name}",
        attributes={
            "handler.name": handler_name,
            "action.name": action_name,
        },
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


@contextlib.contextmanager
def _jsonrpc_client_span(method: str, peer_id: str):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.jsonrpc")
    with tracer.start_as_current_span(
        f"jsonrpc.client/{method}",
        attributes={"rpc.system": "jsonrpc", "rpc.method": method, "peer.id": peer_id},
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


@contextlib.contextmanager
def _jsonrpc_server_span(method: str, traceparent: str | None):
    from opentelemetry import propagate, trace

    tracer = trace.get_tracer("finecode.jsonrpc")
    parent_ctx = propagate.extract({"traceparent": traceparent}) if traceparent else None
    with tracer.start_as_current_span(
        f"jsonrpc.server/{method}",
        context=parent_ctx,
        attributes={"rpc.system": "jsonrpc", "rpc.method": method},
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


class JsonRpcTracingHooks:
    """OTel implementation of ITracingHooks for ER processes.

    Provides single-hop envelope tracing for JSON-RPC messages.  Does not
    replace options.traceparent in action payloads — see ITracingHooks docstring
    and the comment on handler_span for the rationale.
    """

    def get_traceparent(self) -> str | None:
        return get_current_traceparent()

    def client_span(self, method: str, peer_id: str):
        return _jsonrpc_client_span(method, peer_id)

    def server_span(self, method: str, traceparent: str | None):
        return _jsonrpc_server_span(method, traceparent)

    def notification_sent(self, method: str) -> None:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span.is_recording():
            span.add_event("jsonrpc.notification.sent", {"rpc.method": method})

    def notification_received(self, method: str, traceparent: str | None) -> None:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span.is_recording():
            span.add_event("jsonrpc.notification.received", {"rpc.method": method})


def add_span_event(name: str, attributes: dict | None = None) -> None:
    if not _telemetry_initialized:
        return
    from opentelemetry import trace
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(name, attributes or {})


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
