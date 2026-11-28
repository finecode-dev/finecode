"""Minimal ER-side peer for RPC delay-attribution probes.

Runs a real ``JsonRpcServerSession`` over stdio with a real ``OtelHopObserver``
whose spans are appended as JSON lines to ``--span-file``. Serves two methods:

- ``probe/echo``: replies ``{"echo": params}`` after ``--handler-ms``.
- ``probe/callback``: sends ``probe/event`` back to the caller and replies
  with whatever the caller answered.

Delay injection (all milliseconds, applied with blocking sleeps):

- ``--write-ms``: sleep in the transport writer before each write.
- ``--dispatch-ms``: sleep in the transport dispatch after each frame read.
- ``--handler-ms``: sleep at the start of every probe handler.
- ``--finish-ms``: sleep in the caller-completion path before closing.

When ``--otlp-endpoint`` is set, the peer additionally runs the real ER
telemetry setup against it (providers, lag monitor) so its spans and lag
records land in the collector; ``--block-ms`` stalls the loop once after
setup so the lag monitor warns. This is test harness, not product code: the
sleeps only shift timing so the test can check that each delay lands in the
right stage gap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
import typing

from finecode_extension_runner.shared_telemetry.health import HealthTracker
from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
from finecode_jsonrpc.server_transport import ServerStdioTransport
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import StatusCode

from finecode_jsonrpc import JsonRpcServerSession

args_parser = argparse.ArgumentParser()
_ = args_parser.add_argument("--span-file", required=True)
_ = args_parser.add_argument("--write-ms", type=float, default=0.0)
_ = args_parser.add_argument("--dispatch-ms", type=float, default=0.0)
_ = args_parser.add_argument("--handler-ms", type=float, default=0.0)
_ = args_parser.add_argument("--finish-ms", type=float, default=0.0)
_ = args_parser.add_argument("--otlp-endpoint", type=str, default="")
_ = args_parser.add_argument("--block-ms", type=float, default=0.0)
args = args_parser.parse_args()

_WRITE_S = args.write_ms / 1000.0
_DISPATCH_S = args.dispatch_ms / 1000.0
_HANDLER_S = args.handler_ms / 1000.0
_FINISH_S = args.finish_ms / 1000.0
_OTLP_ENDPOINT = args.otlp_endpoint
_BLOCK_S = args.block_ms / 1000.0


class _FileSpanExporter(SpanExporter):
    """Append finished spans to a JSONL file (one object per line)."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._path = path

    def export(self, spans: typing.Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock, open(self._path, "a", encoding="utf-8") as handle:
            for span in spans:
                context = span.get_span_context()
                assert context is not None
                parent = span.parent
                _ = handle.write(
                    json.dumps(
                        {
                            "name": span.name,
                            "trace_id": format(context.trace_id, "032x"),
                            "span_id": format(context.span_id, "016x"),
                            "parent_span_id": (
                                format(parent.span_id, "016x")
                                if parent is not None
                                else None
                            ),
                            "start_time": span.start_time,
                            "end_time": span.end_time,
                            "status": str(span.status.status_code)
                            if span.status is not None
                            else str(StatusCode.UNSET),
                            "attributes": dict(span.attributes or {}),
                            "events": [
                                {
                                    "name": event.name,
                                    "timestamp": event.timestamp,
                                    "attributes": dict(event.attributes or {}),
                                }
                                for event in span.events or []
                            ],
                        }
                    )
                    + "\n"
                )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def _install_exporter(path: str, otlp_endpoint: str = "") -> None:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": "finecode.er.probe"})
    )
    provider.add_span_processor(SimpleSpanProcessor(_FileSpanExporter(path)))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=otlp_endpoint,
                    insecure=not otlp_endpoint.startswith("https://"),
                )
            )
        )
    trace_api.set_tracer_provider(provider)


_install_exporter(args.span_file, args.otlp_endpoint)

_peer_health = HealthTracker(process_role="er")
_peer_health.set_availability("traces", "active")
observer = OtelHopObserver(role="er", health=_peer_health)


def _maybe_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


_CALLBACK_SESSION: dict[str, typing.Any] = {"session": None}


async def _echo(params: typing.Any) -> typing.Any:
    _maybe_sleep(_HANDLER_S)
    return {"echo": params}


async def _callback(params: typing.Any) -> typing.Any:
    _maybe_sleep(_HANDLER_S)
    session = _CALLBACK_SESSION["session"]
    assert session is not None
    seen = await session.send_request("probe/event", params)
    return {"seen": seen}


async def _amain() -> None:
    if _OTLP_ENDPOINT:
        from finecode_extension_runner.shared_telemetry import (
            providers as shared_providers,
        )

        from finecode_extension_runner import er_lag_monitor

        shared_providers.setup_providers(
            service_name="finecode.er.probe",
            resource_attrs={"service.name": "finecode.er.probe"},
            endpoint=_OTLP_ENDPOINT,
            health=_peer_health,
        )
        monitor = er_lag_monitor.ErLagMonitor(health=_peer_health)
        monitor.arm()
        monitor.start()
        if _BLOCK_S > 0:
            time.sleep(_BLOCK_S)  # noqa: ASYNC251 - stalling the loop is the behaviour under test
    transport = ServerStdioTransport()
    session = JsonRpcServerSession(tracing=None, hop_observer=observer)
    session.attach(transport)
    _CALLBACK_SESSION["session"] = session
    session.on_request("probe/echo", _echo)
    session.on_request("probe/callback", _callback)

    if _WRITE_S > 0:
        original_write_messages = transport._write_messages

        async def slow_write_messages(write_transport: typing.Any) -> None:
            original_write = write_transport.write

            def slow_write(data: typing.Any) -> typing.Any:
                _maybe_sleep(_WRITE_S)
                return original_write(data)

            write_transport.write = slow_write
            await original_write_messages(write_transport)

        transport._write_messages = slow_write_messages

    if _DISPATCH_S > 0:
        original_dispatch = transport._dispatch_message

        async def slow_dispatch(message: typing.Any, frame_read: typing.Any) -> None:
            _maybe_sleep(_DISPATCH_S)
            await original_dispatch(message, frame_read)

        transport._dispatch_message = slow_dispatch

    if _FINISH_S > 0:
        original_finish = session._finish_caller

        def slow_finish(hop: typing.Any, outcome: typing.Any) -> None:
            _maybe_sleep(_FINISH_S)
            original_finish(hop, outcome)

        session._finish_caller = slow_finish

    await transport.start()
    _done, pending = await asyncio.wait(
        list(transport._tasks), return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        _ = task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


asyncio.run(_amain())
