import contextlib
import importlib.metadata
import time
from pathlib import Path

from finecode_jsonrpc.tracing import IHopObserver

from finecode_extension_runner import logs as er_logs
from finecode_extension_runner.shared_telemetry import providers as shared_providers
from finecode_extension_runner.shared_telemetry.health import (
    HealthEmitter,
    HealthTracker,
    LogGroupLevel,
)

_handler_duration_hist = None
_handler_errors_counter = None
_telemetry_initialized = False
_health = HealthTracker(process_role="er")


def _required_group_levels() -> list[LogGroupLevel]:
    """Effective levels of the ER's required-record groups, read at writing."""
    return [
        {"group": group, "level": er_logs.effective_level_name(group)}
        for group in (
            "finecode_extension_runner.shared_telemetry.health",
            "finecode_extension_runner.shared_telemetry.rpc_hops",
            "finecode_extension_runner.er_lag_monitor",
        )
    ]


_health.set_log_level_reader(_required_group_levels)
_emitter: HealthEmitter | None = None


def _er_resource_attrs(service_name: str, project_path: Path) -> dict[str, str]:
    try:
        version = importlib.metadata.version("finecode_extension_runner")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {
        "service.name": service_name,
        "service.version": version,
        "project.path": str(project_path),
    }


def init_otel_logging(service_name: str, project_path: Path, endpoint: str) -> None:
    _ = shared_providers.setup_providers(
        service_name=service_name,
        resource_attrs=_er_resource_attrs(service_name, project_path),
        endpoint=endpoint,
        health=_health,
    )


def init_tracer_provider(service_name: str, project_path: Path, endpoint: str) -> None:
    _ = shared_providers.setup_providers(
        service_name=service_name,
        resource_attrs=_er_resource_attrs(service_name, project_path),
        endpoint=endpoint,
        health=_health,
    )


def init_meter_provider(service_name: str, project_path: Path, endpoint: str) -> None:
    global _handler_duration_hist, _handler_errors_counter

    from opentelemetry import metrics

    result = shared_providers.setup_providers(
        service_name=service_name,
        resource_attrs=_er_resource_attrs(service_name, project_path),
        endpoint=endpoint,
        health=_health,
    )
    if result.metrics != "active":
        return

    meter = metrics.get_meter("finecode.er")
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


_hop_observer: IHopObserver | None = None


def hop_observer() -> IHopObserver:
    """Process-wide hop observer for ER<->WM request timing.

    Always returns the singleton: while traces are inactive every open
    returns ``None`` and the transports perform no further hop work.
    ``tracing=`` stays for notification hooks, which have no hop.
    """
    global _hop_observer
    if _hop_observer is None:
        from finecode_extension_runner.shared_telemetry.rpc_hops import (
            OtelHopObserver,
        )

        _hop_observer = OtelHopObserver(role="er", health=_health)
    return _hop_observer


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
