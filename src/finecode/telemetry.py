import contextlib
import socket
import time
from pathlib import Path
from urllib.parse import urlparse
import importlib.metadata
import logging

# Metric instruments — populated by init_meter_provider(); None when OTel is disabled.
_action_duration_hist = None
_action_errors_counter = None
_er_startup_hist = None
_er_active_counter = None

# Endpoints already probed for the one-time reachability heads-up, so the three
# init_* functions log at most once per endpoint.
_probed_endpoints: set[str] = set()


def _validate_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse the OTLP endpoint into (host, port), raising on a malformed value.

    ``otlp_endpoint`` is explicit configuration, so a value we cannot parse is a
    developer error worth surfacing loudly rather than papering over with defaults.
    """
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(
            f"Invalid otlp_endpoint {endpoint!r}: expected host and port, "
            f"e.g. http://otel-lgtm:4317"
        )
    return parsed.hostname, parsed.port


def _silence_otel_export_logs() -> None:
    """Raise the OTel exporter logger threshold to ERROR.

    A collector that is absent at startup — or that goes down mid-session —
    otherwise produces a stream of gRPC export-retry warnings for the process
    lifetime. The exporters buffer and retry regardless, so suppressing the retry
    churn (while still surfacing genuine ERROR-level export failures) is safe.
    """

    logging.getLogger("opentelemetry.exporter").setLevel(logging.ERROR)


def _probe_endpoint_once(endpoint: str, host: str, port: int) -> None:
    """Log a one-time heads-up if the endpoint is not reachable at startup.

    This does NOT gate exporter setup: the OTLP batch processors buffer and retry,
    so a collector started after the WM (e.g. via ``scripts/observability.sh up`` or a
    ``COMPOSE_PROFILES=otel`` stack that comes up alongside the container) is picked
    up automatically. The probe only tells the developer whether signals are flowing
    yet — useful when verifying an observability setup.
    """
    if endpoint in _probed_endpoints:
        return
    _probed_endpoints.add(endpoint)

    try:
        with socket.create_connection((host, port), timeout=1.0):
            return
    except OSError:
        pass

    from loguru import logger

    logger.warning(
        f"OTLP endpoint {endpoint} is not reachable yet; exporters will connect once "
        f"it is up (e.g. scripts/observability.sh up). WAL events are recorded regardless."
    )


def init_otel_logging(
    service_name: str, workspace_path: Path | None = None, endpoint: str | None = None
) -> None:
    if not endpoint:
        return

    host, port = _validate_endpoint(endpoint)
    _silence_otel_export_logs()
    _probe_endpoint_once(endpoint, host, port)

    from finecode_extension_runner.logs import filter_logs
    from loguru import logger
    from opentelemetry._logs.severity import SeverityNumber
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter,
    )
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
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


def init_tracer_provider(
    service_name: str, workspace_path: Path | None = None, endpoint: str | None = None
) -> None:
    if not endpoint:
        return

    host, port = _validate_endpoint(endpoint)
    _silence_otel_export_logs()
    _probe_endpoint_once(endpoint, host, port)

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
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


def init_meter_provider(
    service_name: str, workspace_path: Path | None = None, endpoint: str | None = None
) -> None:
    global \
        _action_duration_hist, \
        _action_errors_counter, \
        _er_startup_hist, \
        _er_active_counter

    if not endpoint:
        return

    host, port = _validate_endpoint(endpoint)
    _silence_otel_export_logs()
    _probe_endpoint_once(endpoint, host, port)

    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        PeriodicExportingMetricReader,
    )
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
            _er_startup_hist.record(time.perf_counter() - start, {"env.name": env_name})


def er_active_inc(env_name: str) -> None:
    if _er_active_counter is not None:
        _er_active_counter.add(1, {"env.name": env_name})


def er_active_dec(env_name: str) -> None:
    if _er_active_counter is not None:
        _er_active_counter.add(-1, {"env.name": env_name})


@contextlib.contextmanager
def action_run_span(
    action_name: str,
    project_path: Path | str,
    wal_run_id: str,
    dev_env: str | None = None,
    orchestration_depth: int = 0,
):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.wm")
    attrs: dict[str, str | int] = {
        "action.name": action_name,
        "project.path": str(project_path),
        "wal.run_id": wal_run_id,
    }
    if dev_env is not None:
        attrs["run.dev_env"] = dev_env
    if orchestration_depth:
        attrs["run.orchestration_depth"] = orchestration_depth
    with tracer.start_as_current_span(
        f"action.run/{action_name}",
        attributes=attrs,
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


def get_current_traceparent() -> str | None:
    """Return the W3C traceparent header for the currently active span, or None if no active span."""
    from opentelemetry import propagate, trace

    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


@contextlib.contextmanager
def runner_start_span(env_name: str):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.wm")
    with tracer.start_as_current_span(
        "action.runner_start",
        attributes={"env.name": env_name},
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


@contextlib.contextmanager
def er_dispatch_span(env_name: str, runner_id: str, action_name: str):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.wm")
    with tracer.start_as_current_span(
        "action.er_dispatch",
        attributes={
            "env.name": env_name,
            "runner.id": runner_id,
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
    parent_ctx = (
        propagate.extract({"traceparent": traceparent}) if traceparent else None
    )
    with tracer.start_as_current_span(
        f"jsonrpc.server/{method}",
        context=parent_ctx,
        attributes={"rpc.system": "jsonrpc", "rpc.method": method},
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


class JsonRpcTracingHooks:
    """OTel implementation of ITracingHooks for the WM process.

    Provides single-hop envelope tracing for JSON-RPC messages.  Does not
    replace options.traceparent in action payloads — see ITracingHooks docstring
    and the comment on proxy_utils.traceparent capture for the rationale.
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
    from opentelemetry import trace

    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(name, attributes or {})


@contextlib.contextmanager
def lsp_request_span(method: str):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.lsp")
    with tracer.start_as_current_span(
        f"lsp.request/{method}",
        attributes={"lsp.method": method},
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


@contextlib.contextmanager
def mcp_tool_span(tool_name: str):
    from opentelemetry import trace

    tracer = trace.get_tracer("finecode.mcp")
    with tracer.start_as_current_span(
        f"mcp.tool/{tool_name}",
        attributes={"mcp.tool_name": tool_name},
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


@contextlib.contextmanager
def attach_incoming_traceparent(params: dict):
    """Restore a traceparent carried in params as the current OTel context.

    Pops ``_traceparent`` from params so it is not forwarded to action payload
    or other downstream consumers.  No-op when the key is absent or OTel is
    inactive.
    """
    incoming = params.pop("_traceparent", None)
    if not incoming:
        yield
        return
    from opentelemetry import context as otel_context
    from opentelemetry import propagate

    parent_ctx = propagate.extract({"traceparent": incoming})
    token = otel_context.attach(parent_ctx)
    try:
        yield
    finally:
        otel_context.detach(token)
