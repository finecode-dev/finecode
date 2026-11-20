"""AC2 combined run: probe-(d) delay attribution with an OTLP sink, asserting
the ER lag warning and the span gaps together.

The peer runs the real ER-side stack (hop observer, lag monitor) against the
sink while a 300 ms caller-completion delay lands in exactly one stage gap;
a 1.6 s loop stall up front makes the peer's monitor warn and recover. Every
record is asserted from the sink, not from the local exporters.
"""

from __future__ import annotations

import asyncio
import time
import typing
from pathlib import Path

import pytest
from finecode_extension_runner.shared_telemetry.health import HealthTracker
from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver

from tests.e2e.otlp_sink import OtlpSink
from tests.integration.test_rpc_delay_attribution import (
    _connected_peer,
    _er_to_wm_exchange,
    _prepare,
)

_LAG_MS = 290.0
_SMALL_MS = 100.0


@pytest.fixture()
def sink() -> typing.Iterator[OtlpSink]:
    receiver = OtlpSink()
    try:
        yield receiver
    finally:
        receiver.stop()


def _event_nanos(span: object) -> list[tuple[str, int]]:
    ordered = sorted(span.events, key=lambda e: e.time_unix_nano)  # type: ignore[union-attr]
    return [(e.name, e.time_unix_nano) for e in ordered]


def _assert_single_large_gap(
    events: list[tuple[str, int]], expected: tuple[str, str]
) -> None:
    monos = [mono for _, mono in events]
    assert monos == sorted(monos)
    gaps = [
        ((events[i][0], events[i + 1][0]), (monos[i + 1] - monos[i]) / 1e6)
        for i in range(len(events) - 1)
    ]
    large = [(pair, ms) for pair, ms in gaps if ms >= _LAG_MS]
    assert len(large) == 1, gaps
    assert large[0][0] == expected, gaps
    assert all(ms < _SMALL_MS for pair, ms in gaps if pair != expected), gaps


async def test_probe_d_with_er_lag_warning_over_otlp(
    tmp_path: Path, sink: OtlpSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 300 ms caller-completion delay lands in one stage gap while the
    stalled peer loop warns and recovers -- all visible in the sink."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "finecode.test.wm"})
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"http://{sink.address}", insecure=True)
        )
    )

    def _test_tracer(name: str, *args: object, **kwargs: object) -> object:
        return provider.get_tracer(name)

    monkeypatch.setattr(otel_trace, "get_tracer", _test_tracer)
    wm_health = HealthTracker(process_role="wm")
    wm_health.set_availability("traces", "active")
    observer = OtelHopObserver(role="wm", health=wm_health)

    async with _connected_peer(
        tmp_path,
        observer,
        [
            "--finish-ms",
            "300",
            "--otlp-endpoint",
            f"http://{sink.address}",
            "--block-ms",
            "1600",
        ],
    ) as peer:
        await _prepare(peer, observer, exporter)
        result = await asyncio.wait_for(
            _er_to_wm_exchange(peer, observer), timeout=60.0
        )
        assert result == {"seen": {"event": None}}

        def _complete() -> bool:
            snapshot = sink.snapshot()
            bodies = [
                str(r.body)
                for batch in snapshot["logs"]
                for resource_logs in batch.resource_logs
                for scope_logs in resource_logs.scope_logs
                for r in scope_logs.log_records
            ]
            names = [
                span.name
                for batch in snapshot["traces"]
                for resource_spans in batch.resource_spans
                for scope_spans in resource_spans.scope_spans
                for span in scope_spans.spans
            ]
            return (
                any("lag exceeded threshold" in b for b in bodies)
                and any("lag recovered" in b for b in bodies)
                and "jsonrpc.client/probe/event" in names
                and "jsonrpc.server/probe/event" in names
            )

        started = time.monotonic()
        while time.monotonic() - started < 120.0:
            if _complete():
                break
            await asyncio.sleep(0.5)
        assert _complete()

    snapshot = sink.snapshot()
    by_name: dict[str, list] = {}
    service_of: dict[int, str] = {}
    for batch in snapshot["traces"]:
        for resource_spans in batch.resource_spans:
            service = next(
                (
                    item.value.string_value
                    for item in resource_spans.resource.attributes
                    if item.key == "service.name"
                ),
                None,
            )
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    by_name.setdefault(span.name, []).append(span)
                    service_of[id(span)] = service or ""
    caller = by_name["jsonrpc.client/probe/event"]
    assert len(caller) == 1
    _assert_single_large_gap(
        _event_nanos(caller[0]),
        ("rpc.stage.response_resolved", "rpc.stage.caller_completed"),
    )
    assert service_of[id(caller[0])] == "finecode.er.probe"
    receiver = by_name["jsonrpc.server/probe/event"]
    assert len(receiver) == 1
    assert receiver[0].parent_span_id == caller[0].span_id
