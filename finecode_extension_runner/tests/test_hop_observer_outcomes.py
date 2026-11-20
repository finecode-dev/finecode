"""Failure isolation and outcome attribution for OTel hop spans.

Each RPC outcome must close its span exactly once with the right outcome
attributes, and a fault inside telemetry must never change the RPC result:
the span is evidence, not a participant. Late-stage records are covered by
the late-log tests; here a response with no waiter is simply dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import threading
import typing

import pytest
from finecode_jsonrpc import JsonRpcServerSession
from finecode_jsonrpc import client as jc
from finecode_jsonrpc.jsonrpc_client import JsonRpcError
from finecode_jsonrpc.server_transport import ServerStdioTransport
from finecode_jsonrpc.tracing import (
    HopIdentity,
    HopToken,
    capture_stage_time,
)
from loguru import logger
from opentelemetry import context as context_module
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import Span, Tracer, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from finecode_extension_runner.shared_telemetry.health import HealthTracker
from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver


@dataclasses.dataclass
class _REnv:
    id: int | str
    jsonrpc: str
    result: typing.Any = None
    params: typing.Any = None


@dataclasses.dataclass
class _StrictEnv:
    id: int | str
    method: str
    params: dict[str, str]
    jsonrpc: str


def _frame(message: dict[str, typing.Any]) -> bytes:
    body = json.dumps(message).encode()
    return (
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n"
    ).encode() + body


def _body(data: bytes) -> dict[str, typing.Any]:
    _header, _, payload = data.decode().partition("\r\n\r\n")
    return typing.cast(dict[str, typing.Any], json.loads(payload))


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    """One global OTel provider for the module; cleared between tests."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace_api.set_tracer_provider(provider)
    return exporter


@pytest.fixture()
def observer(exporter: typing.Any) -> OtelHopObserver:
    exporter.clear()
    health = HealthTracker(process_role="er")
    health.set_availability("traces", "active")
    return OtelHopObserver(role="er", health=health)


@pytest.fixture()
def health_observer(
    exporter: InMemorySpanExporter,
) -> tuple[OtelHopObserver, HealthTracker]:
    exporter.clear()
    health = HealthTracker(process_role="er")
    health.set_availability("traces", "active")
    return OtelHopObserver(role="er", health=health), health


def _spans_by_name(exporter: typing.Any) -> dict[str, list[typing.Any]]:
    grouped: dict[str, list[typing.Any]] = {}
    for span in exporter.get_finished_spans():
        grouped.setdefault(span.name, []).append(span)
    return grouped


def _only(span_list: list[typing.Any]) -> typing.Any:
    assert len(span_list) == 1, [span.name for span in span_list]
    return span_list[0]


class _FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


async def _run_session_exchange(
    observer: OtelHopObserver,
    handler: typing.Callable[[typing.Any], typing.Any] | None,
    request: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    """Drive one request through a real session and real stdio transport."""
    transport = ServerStdioTransport()
    session = JsonRpcServerSession(tracing=None, hop_observer=observer)
    session.attach(transport)
    if handler is not None:
        session.on_request(request["method"], handler)

    writer = _FakeWriter()
    writer_task = asyncio.ensure_future(
        transport._write_messages(typing.cast(typing.Any, writer))
    )
    reader = asyncio.StreamReader()
    read_task = asyncio.ensure_future(transport._read_messages(reader))
    try:
        reader.feed_data(_frame(request))
        for _ in range(1000):
            if writer.written and not observer._open:
                break
            await asyncio.sleep(0)
        assert writer.written, "no reply was written"
        assert not observer._open, "hop was not closed by the writer"
        return _body(writer.written[0])
    finally:
        reader.feed_eof()
        await asyncio.gather(read_task, return_exceptions=True)
        _ = writer_task.cancel()
        await asyncio.gather(writer_task, return_exceptions=True)
        await transport.stop()


async def test_receiver_success_records_handler_and_close(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """A handled request ends its span once with outcome and close reason."""

    async def echo(params: typing.Any) -> typing.Any:
        return {"echo": params}

    reply = await _run_session_exchange(
        observer, echo, {"jsonrpc": "2.0", "id": 1, "method": "t/echo"}
    )
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {"echo": None}}
    assert observer._open == {}
    assert observer._expiry_timer is None
    span = _only(_spans_by_name(exporter)["jsonrpc.server/t/echo"])
    assert span.attributes["finecode.rpc.handler_outcome"] == "returned"
    assert span.attributes["finecode.rpc.close_reason"] == "completed"
    assert span.attributes["finecode.rpc.request_id"] == "1"
    assert span.attributes["finecode.rpc.request_id_type"] == "int"
    assert [event.name for event in span.events] == [
        "rpc.stage.frame_read",
        "rpc.stage.dispatched",
        "rpc.stage.handler_started",
        "rpc.stage.handler_ended",
        "rpc.stage.submitted",
        "rpc.stage.enqueued",
        "rpc.stage.dequeued",
        "rpc.stage.write_returned",
        "rpc.stage.drain_started",
        "rpc.stage.drain_returned",
    ]


async def test_receiver_error_and_write_failure_keep_both_facts(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """A raising handler records raised; a failed write records write_failed."""

    async def boom(_params: typing.Any) -> typing.Any:
        raise RuntimeError("handler blew up")

    reply = await _run_session_exchange(
        observer, boom, {"jsonrpc": "2.0", "id": 2, "method": "t/boom"}
    )
    assert reply["error"]["code"] == -32603
    span = _only(_spans_by_name(exporter)["jsonrpc.server/t/boom"])
    assert span.attributes["finecode.rpc.handler_outcome"] == "raised"
    assert span.attributes["finecode.rpc.close_reason"] == "completed"

    async def ok(_params: typing.Any) -> typing.Any:
        return "fine"

    transport = ServerStdioTransport()
    session = JsonRpcServerSession(tracing=None, hop_observer=observer)
    session.attach(transport)
    session.on_request("t/ok", ok)

    class _FailingWriter(_FakeWriter):
        def write(self, data: bytes) -> None:
            raise RuntimeError(f"write of {len(data)} bytes blew up")

    writer_task = asyncio.ensure_future(
        transport._write_messages(typing.cast(typing.Any, _FailingWriter()))
    )
    reader = asyncio.StreamReader()
    read_task = asyncio.ensure_future(transport._read_messages(reader))
    try:
        reader.feed_data(_frame({"jsonrpc": "2.0", "id": 3, "method": "t/ok"}))
        for _ in range(1000):
            done = [
                span
                for span in exporter.get_finished_spans()
                if span.name == "jsonrpc.server/t/ok"
            ]
            if done:
                break
            await asyncio.sleep(0)
    finally:
        reader.feed_eof()
        await asyncio.gather(read_task, return_exceptions=True)
        await asyncio.gather(writer_task, return_exceptions=True)
        await transport.stop()
    assert observer._open == {}
    span = _only(_spans_by_name(exporter)["jsonrpc.server/t/ok"])
    assert span.attributes["finecode.rpc.handler_outcome"] == "returned"
    assert span.attributes["finecode.rpc.close_reason"] == "write_failed"


async def test_receiver_early_rejection_marks_not_executed(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """An unknown method is rejected without handler stages or scope."""
    reply = await _run_session_exchange(
        observer, None, {"jsonrpc": "2.0", "id": 4, "method": "t/nope"}
    )
    assert reply["error"]["code"] == -32601
    span = _only(_spans_by_name(exporter)["jsonrpc.server/t/nope"])
    assert span.attributes["finecode.rpc.handler_outcome"] == "not_executed"
    assert span.attributes["finecode.rpc.close_reason"] == "completed"
    assert [event.name for event in span.events] == [
        "rpc.stage.frame_read",
        "rpc.stage.dispatched",
        "rpc.stage.submitted",
        "rpc.stage.enqueued",
        "rpc.stage.dequeued",
        "rpc.stage.write_returned",
        "rpc.stage.drain_started",
        "rpc.stage.drain_returned",
    ]


class _SessionFakeTransport:
    """Server-transport double capturing envelopes and hops."""

    def __init__(self, observer: OtelHopObserver | None = None) -> None:
        self.sent: list[tuple[dict[str, typing.Any], HopToken | None]] = []
        self._readable_id = "wm"
        self._plain: typing.Any = None
        self._timed: typing.Any = None
        self._observer = observer

    def on_message(self, callback: typing.Any) -> None:
        self._plain = callback

    def on_message_timed(self, callback: typing.Any) -> None:
        self._timed = callback

    def attach_hop_observer(self, observer: typing.Any) -> None:
        self._observer = observer

    def on_exit(self, _callback: typing.Any) -> None:
        return None

    def send(
        self, message: dict[str, typing.Any], *, hop: HopToken | None = None
    ) -> None:
        self.sent.append((message, hop))
        # Mirror the real writer: the receiver hop closes once its frame is
        # written. Caller hops are unaffected (the observer ignores the
        # role-mismatched close).
        if hop is not None and self._observer is not None:
            self._observer.close_receiver(hop, "completed", capture_stage_time())


async def test_session_caller_outcomes(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """ER-initiated requests close with the outcome the waiter observed."""
    session = JsonRpcServerSession(tracing=None, hop_observer=observer)
    transport = _SessionFakeTransport()
    session.attach(transport)

    async def reply_with(message: dict[str, typing.Any]) -> None:
        await asyncio.sleep(0.01)
        await transport._timed(message, capture_stage_time())

    seen: list[str] = []

    async def run_case(
        method: str,
        trigger: typing.Callable[[int], typing.Any],
        expect: type[BaseException] | None,
        outcome: str,
        timeout: float | None = None,
    ) -> None:
        exporter.clear()
        task = asyncio.ensure_future(session.send_request(method, timeout=timeout))
        await asyncio.sleep(0)
        msg_id = len(seen)
        await trigger(msg_id)
        if expect is None:
            await task
        else:
            with pytest.raises(expect):
                await task
        seen.append(outcome)
        assert observer._open == {}
        span = _only(_spans_by_name(exporter)[f"jsonrpc.client/{method}"])
        assert span.attributes["finecode.rpc.caller_outcome"] == outcome

    await run_case(
        "m/ok",
        lambda i: reply_with({"jsonrpc": "2.0", "id": i, "result": i}),
        None,
        "result",
    )
    await run_case(
        "m/err",
        lambda i: reply_with(
            {"jsonrpc": "2.0", "id": i, "error": {"code": -1, "message": "bad"}}
        ),
        JsonRpcError,
        "remote_error",
    )
    await run_case(
        "m/slow", lambda _i: asyncio.sleep(0.05), TimeoutError, "timeout", 0.01
    )

    exporter.clear()
    cancellable = asyncio.ensure_future(session.send_request("m/cancel"))
    await asyncio.sleep(0.01)
    _ = cancellable.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancellable
    seen.append("cancelled")
    assert observer._open == {}
    span = _only(_spans_by_name(exporter)["jsonrpc.client/m/cancel"])
    assert span.attributes["finecode.rpc.caller_outcome"] == "cancelled"

    exporter.clear()
    hanging = asyncio.ensure_future(session.send_request("m/hang", timeout=30))
    await asyncio.sleep(0.01)
    await session._handle_exit()
    with pytest.raises(RuntimeError):
        await hanging
    seen.append("disconnected")
    assert observer._open == {}
    span = _only(_spans_by_name(exporter)["jsonrpc.client/m/hang"])
    assert span.attributes["finecode.rpc.caller_outcome"] == "disconnected"

    assert seen == ["result", "remote_error", "timeout", "cancelled", "disconnected"]


def _make_client(observer: OtelHopObserver) -> jc.JsonRpcClient:
    return jc.JsonRpcClient(
        message_types={
            "r/m": (dict, dict, _REnv, None),
            "bad/r": (dict, dict, _StrictEnv, None),
            "s/m": (dict, dict, str, None),
        },
        readable_id="test-server",
        hop_observer=observer,
    )


async def _drive_sender(
    client: jc.JsonRpcClient, writer: _FakeWriter
) -> asyncio.Task[None]:
    sender = asyncio.ensure_future(
        client._send_outgoing_loop(typing.cast(typing.Any, writer))
    )
    await asyncio.sleep(0)
    return sender


class _DrivenClient:
    """A client with its sender loop running; closed with ``aclose``."""

    def __init__(self, observer: OtelHopObserver) -> None:
        self.client = _make_client(observer)
        self.sender: asyncio.Task[None] | None = None

    async def __aenter__(self) -> jc.JsonRpcClient:
        self.sender = await _drive_sender(self.client, _FakeWriter())
        return self.client

    async def __aexit__(self, *args: object) -> None:
        assert self.sender is not None
        _ = self.sender.cancel()
        await asyncio.gather(self.sender, return_exceptions=True)


async def test_client_caller_success(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """A resolved WM request closes its span with the result outcome."""
    async with _DrivenClient(observer) as client:
        task = asyncio.ensure_future(client.send_request("r/m"))
        await asyncio.sleep(0)
        msg_id = next(iter(client._async_request_futures))
        await client.handle_message(
            {"jsonrpc": "2.0", "id": msg_id, "result": "ok"},
            capture_stage_time(),
        )
        response = await task
        assert response.result == "ok"
        span = _only(_spans_by_name(exporter)["jsonrpc.client/r/m"])
        assert span.attributes["finecode.rpc.caller_outcome"] == "result"
    assert observer._open == {}


async def test_client_caller_remote_error(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """A remote error closes the WM caller span as a remote error."""
    async with _DrivenClient(observer) as client:
        task = asyncio.ensure_future(client.send_request("r/m"))
        await asyncio.sleep(0)
        msg_id = next(iter(client._async_request_futures))
        await client.handle_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -1, "message": "bad"},
            },
            capture_stage_time(),
        )
        with pytest.raises(jc.ErrorOnRequest):
            await task
        span = _only(_spans_by_name(exporter)["jsonrpc.client/r/m"])
        assert span.attributes["finecode.rpc.caller_outcome"] == "remote_error"
    assert observer._open == {}


async def test_client_caller_invalid_response(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """An undecodable reply closes the WM caller span as invalid."""
    async with _DrivenClient(observer) as client:
        task = asyncio.ensure_future(client.send_request("bad/r"))
        await asyncio.sleep(0)
        msg_id = next(iter(client._async_request_futures))
        await client.handle_message(
            {"jsonrpc": "2.0", "id": msg_id, "result": {"a": 1}},
            capture_stage_time(),
        )
        with pytest.raises(jc.InvalidResponse):
            await task
        span = _only(_spans_by_name(exporter)["jsonrpc.client/bad/r"])
        assert span.attributes["finecode.rpc.caller_outcome"] == "invalid_response"
    assert observer._open == {}


async def test_client_caller_timeout(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """A timed-out WM request closes its span with the timeout outcome."""
    async with _DrivenClient(observer) as client:
        with pytest.raises(jc.ResponseTimeout):
            await client.send_request("r/m", timeout=0.01)
        span = _only(_spans_by_name(exporter)["jsonrpc.client/r/m"])
        assert span.attributes["finecode.rpc.caller_outcome"] == "timeout"
    assert observer._open == {}


async def test_client_caller_cancel(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """Cancelling the waiter closes the WM caller span as cancelled."""
    async with _DrivenClient(observer) as client:
        task = asyncio.ensure_future(client.send_request("r/m"))
        await asyncio.sleep(0)
        _ = task.cancel()
        with pytest.raises(jc.RequestCancelledError):
            await task
        span = _only(_spans_by_name(exporter)["jsonrpc.client/r/m"])
        assert span.attributes["finecode.rpc.caller_outcome"] == "cancelled"
    assert observer._open == {}


async def test_client_caller_disconnect(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """A dead channel closes the WM caller span as disconnected."""
    async with _DrivenClient(observer) as client:
        task = asyncio.ensure_future(client.send_request("r/m"))
        await asyncio.sleep(0)
        client._fail_pending_requests("boom")
        with pytest.raises(jc.ServerStoppedError):
            await task
        span = _only(_spans_by_name(exporter)["jsonrpc.client/r/m"])
        assert span.attributes["finecode.rpc.caller_outcome"] == "disconnected"
    assert observer._open == {}


async def test_client_sync_future_resolves_on_io_reader(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """The direct reader path stages frame and resolution for sync waiters."""
    client = _make_client(observer)
    future = client.send_request_sync("s/m")
    msg_id = next(iter(client._sync_request_futures))

    message_queue: typing.Any = client.in_message_queue.sync_q
    hop_state = jc._ReaderHopState(
        observer=observer, sync_hops=client._sync_request_hops
    )
    reader = asyncio.StreamReader()
    reader.feed_data(_frame({"jsonrpc": "2.0", "id": msg_id, "result": "ok"}))
    reader.feed_eof()
    await jc.read_messages_from_reader(
        reader,
        message_queue,
        client._sync_request_futures,
        client._expected_result_type_by_msg_id,
        threading.Event(),
        server_id="test-server",
        hop_state=hop_state,
    )
    assert future.result() == "ok"
    span = _only(_spans_by_name(exporter)["jsonrpc.client/s/m"])
    assert span.attributes["finecode.rpc.caller_outcome"] == "result"
    assert [event.name for event in span.events] == [
        "rpc.stage.submitted",
        "rpc.stage.enqueued",
        "rpc.stage.frame_read",
        "rpc.stage.response_resolved",
        "rpc.stage.caller_completed",
    ]
    assert observer._open == {}


async def test_injected_span_creation_failure_keeps_rpc_going(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """A raise while creating the span is contained once; RPC is unaffected."""
    observer, health = health_observer
    original_start_span = Tracer.start_span

    def raising_start_span(
        self: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        raise RuntimeError("tracer blew up")

    Tracer.start_span = raising_start_span
    try:
        token = observer.open_caller(
            HopIdentity(connection_id="c", role="caller", request_id=1, method="m"),
            peer="p",
            at=capture_stage_time(),
        )
        assert token is None
    finally:
        Tracer.start_span = original_start_span
    assert health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1
    assert observer._open == {}


async def test_injected_event_end_and_attach_failures_are_contained(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """Raises in event/end/attach are each contained once as S10."""
    observer, health = health_observer
    before = health.snapshot()["signals"]["traces"]["counts"]["contained_failure"]

    token = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=1, method="m"),
        peer="p",
        at=capture_stage_time(),
    )
    assert token is not None

    original_add_event = Span.add_event

    def raising_add_event(
        self: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        raise RuntimeError("event blew up")

    Span.add_event = raising_add_event
    try:
        observer.stage(token, "submitted", capture_stage_time())
    finally:
        Span.add_event = original_add_event

    original_end = Span.end

    def raising_end(self: typing.Any, *a: typing.Any, **k: typing.Any) -> None:
        raise RuntimeError("end blew up")

    Span.end = raising_end
    try:
        observer.close_caller(token, "result", capture_stage_time())
    finally:
        Span.end = original_end
    assert observer._open == {}

    original_attach = context_module.attach

    def raising_attach(*a: typing.Any, **k: typing.Any) -> typing.Any:
        raise RuntimeError("attach blew up")

    token2 = observer.open_receiver(
        HopIdentity(connection_id="c", role="receiver", request_id=2, method="m"),
        None,
        capture_stage_time(),
    )
    assert token2 is not None
    context_module.attach = raising_attach
    try:
        with observer.handler_scope(token2):
            pass
    finally:
        context_module.attach = original_attach
    observer.close_receiver(token2, "completed", capture_stage_time())

    after = health.snapshot()["signals"]["traces"]["counts"]["contained_failure"]
    assert after - before == 3
    assert observer._open == {}


async def test_stale_close_cannot_close_replacement(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """A close for an ended hop never touches the hop reusing its id."""
    first = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=9, method="m"),
        peer="p",
        at=capture_stage_time(),
    )
    assert first is not None
    observer.close_caller(first, "result", capture_stage_time())
    second = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=9, method="m"),
        peer="p",
        at=capture_stage_time(),
    )
    assert second is not None and second is not first
    observer.close_caller(first, "result", capture_stage_time())
    assert len(observer._open) == 1
    observer.close_caller(second, "timeout", capture_stage_time())
    assert observer._open == {}
    spans = _spans_by_name(exporter)["jsonrpc.client/m"]
    assert len(spans) == 2
    assert spans[0].attributes["finecode.rpc.caller_outcome"] == "result"
    assert spans[1].attributes["finecode.rpc.caller_outcome"] == "timeout"
    assert (
        spans[0].attributes["finecode.rpc.generation"]
        != spans[1].attributes["finecode.rpc.generation"]
    )


async def test_connection_close_ends_remaining_as_teardown(
    observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """Transport loss ends stranded hops with the teardown reason."""
    token = observer.open_receiver(
        HopIdentity(connection_id="lost", role="receiver", request_id=1, method="m"),
        None,
        capture_stage_time(),
    )
    assert token is not None
    other = observer.open_receiver(
        HopIdentity(connection_id="kept", role="receiver", request_id=1, method="m"),
        None,
        capture_stage_time(),
    )
    assert other is not None
    observer.connection_closed("lost")
    assert len(observer._open) == 1
    observer.close_receiver(other, "completed", capture_stage_time())
    assert observer._open == {}
    spans = _spans_by_name(exporter)["jsonrpc.server/m"]
    assert len(spans) == 2
    reasons = sorted(span.attributes["finecode.rpc.close_reason"] for span in spans)
    assert reasons == ["completed", "teardown"]


@contextlib.contextmanager
def _capture_late_logs() -> typing.Generator[list[typing.Any], None, None]:
    """Capture late log records plus the ambient trace seen at emission."""
    captured: list[typing.Any] = []

    def sink(message: typing.Any) -> None:
        captured.append(
            (
                dict(message.record["extra"]),
                trace_api.get_current_span().get_span_context(),
            )
        )

    sink_id = logger.add(
        sink,
        level="INFO",
        filter=lambda record: record["extra"].get("late_kind") is not None,
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _late_counts(health: HealthTracker) -> dict[str, int]:
    counts = health.snapshot()["signals"]["traces"]["counts"]
    return {
        "suppressed": counts["suppressed"],
        "contained_failure": counts["contained_failure"],
    }


async def test_late_response_after_timeout_is_matched(
    health_observer: tuple[OtelHopObserver, HealthTracker],
    exporter: typing.Any,
) -> None:
    """A late answer logs one matched record; the caller span is untouched."""
    observer, _health = health_observer
    with _capture_late_logs() as captured:
        token = observer.open_caller(
            HopIdentity(connection_id="c", role="caller", request_id=1, method="m/a"),
            peer="p",
            at=capture_stage_time(),
        )
        assert token is not None
        observer.close_caller(token, "timeout", capture_stage_time())
        span_before = _only(_spans_by_name(exporter)["jsonrpc.client/m/a"])
        events_before = len(span_before.events)
        end_before = span_before.end_time
        observer.response_without_waiter(
            HopIdentity(connection_id="c", role="caller", request_id=1, method="m/a"),
            capture_stage_time(),
        )
    assert len(captured) == 1
    extras, ambient = captured[0]
    assert extras["late_kind"] == "late_response"
    assert extras["rpc_correlation"] == "matched"
    assert extras["rpc_method"] == "m/a"
    assert extras["rpc_connection_id"] == "c"
    assert extras["rpc_request_id"] == "1"
    assert extras["rpc_request_id_type"] == "int"
    assert extras["rpc_generation"] == 1
    assert extras["rpc_prior_outcome"] == "timeout"
    assert extras["trace_id"] == f"{span_before.context.trace_id:032x}"
    assert extras["span_id"] == f"{span_before.context.span_id:016x}"
    # The saved trace context is restored only for the emission.
    assert ambient.trace_id == span_before.context.trace_id
    # The caller span itself is not modified by the late record.
    assert len(span_before.events) == events_before
    assert span_before.end_time == end_before
    assert observer._open == {}


async def test_late_entry_expiry_gives_unmatched(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """After the 60 s entry expiry the late answer is uncorrelated."""
    observer, _health = health_observer
    token = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=2, method="m/b"),
        peer="p",
        at=capture_stage_time(),
    )
    assert token is not None
    observer.close_caller(token, "timeout", capture_stage_time())
    assert len(observer._late) == 1
    entry = next(iter(observer._late.values()))
    entry.expires_at = -1.0
    with _capture_late_logs() as captured:
        observer.response_without_waiter(
            HopIdentity(connection_id="c", role="caller", request_id=2, method="m/b"),
            capture_stage_time(),
        )
    assert len(captured) == 1
    extras, _ambient = captured[0]
    assert extras["late_kind"] == "unmatched_response"
    assert extras["rpc_correlation"] == "unavailable"
    assert "trace_id" not in extras
    assert "span_id" not in extras
    assert "rpc_generation" not in extras
    assert extras["rpc_prior_outcome"] == "unavailable"


async def test_late_emission_cap_eight_per_entry(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """One entry emits at most eight late records, then suppresses."""
    observer, health = health_observer
    token = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=3, method="m/c"),
        peer="p",
        at=capture_stage_time(),
    )
    assert token is not None
    observer.close_caller(token, "timeout", capture_stage_time())
    before = _late_counts(health)["suppressed"]
    with _capture_late_logs() as captured:
        for _ in range(10):
            observer.response_without_waiter(
                HopIdentity(
                    connection_id="c", role="caller", request_id=3, method="m/c"
                ),
                capture_stage_time(),
            )
    assert len(captured) == 8
    assert all(extras["late_kind"] == "late_response" for extras, _ in captured)
    assert _late_counts(health)["suppressed"] - before == 2


async def test_late_bucket_burst_and_refill(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """The bucket admits a burst of 20, then refills one per second."""
    observer, health = health_observer
    before = _late_counts(health)["suppressed"]
    with _capture_late_logs() as captured:
        for i in range(21):
            observer.response_without_waiter(
                HopIdentity(
                    connection_id="c",
                    role="caller",
                    request_id=f"fresh-{i}",
                    method="m",
                ),
                capture_stage_time(),
            )
    assert len(captured) == 20
    assert all(extras["late_kind"] == "unmatched_response" for extras, _ in captured)
    assert _late_counts(health)["suppressed"] - before == 1
    await asyncio.sleep(1.2)
    with _capture_late_logs() as captured:
        observer.response_without_waiter(
            HopIdentity(
                connection_id="c", role="caller", request_id="fresh-21", method="m"
            ),
            capture_stage_time(),
        )
    assert len(captured) == 1
    assert _late_counts(health)["suppressed"] - before == 1


async def test_late_retention_cap_1024(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """Beyond 1,024 retained entries closes retain nothing and count S7."""
    observer, health = health_observer
    for i in range(1024):
        token = observer.open_caller(
            HopIdentity(connection_id="c", role="caller", request_id=i, method="m"),
            peer="p",
            at=capture_stage_time(),
        )
        assert token is not None
        observer.close_caller(token, "timeout", capture_stage_time())
    assert len(observer._late) == 1024
    before = _late_counts(health)["suppressed"]
    token = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id="over", method="m"),
        peer="p",
        at=capture_stage_time(),
    )
    assert token is not None
    observer.close_caller(token, "timeout", capture_stage_time())
    assert len(observer._late) == 1024
    assert _late_counts(health)["suppressed"] - before == 1
    with _capture_late_logs() as captured:
        observer.response_without_waiter(
            HopIdentity(
                connection_id="c", role="caller", request_id="over", method="m"
            ),
            capture_stage_time(),
        )
    assert len(captured) == 1
    extras, _ambient = captured[0]
    assert extras["late_kind"] == "unmatched_response"


async def test_overlong_field_suppresses_correlation(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """A >2 KiB identity field is kept whole but never correlated."""
    observer, health = health_observer
    long_method = "m/" + "x" * 3000
    token = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=4, method=long_method),
        peer="p",
        at=capture_stage_time(),
    )
    assert token is not None
    observer.close_caller(token, "timeout", capture_stage_time())
    before = _late_counts(health)["suppressed"]
    with _capture_late_logs() as captured:
        observer.response_without_waiter(
            HopIdentity(
                connection_id="c", role="caller", request_id=4, method=long_method
            ),
            capture_stage_time(),
        )
    assert len(captured) == 1
    extras, _ambient = captured[0]
    assert extras["late_kind"] == "unmatched_response"
    assert extras["rpc_correlation"] == "unavailable"
    assert extras["rpc_method"] == long_method
    assert "trace_id" not in extras
    assert _late_counts(health)["suppressed"] - before == 1


async def test_late_contention_sets_sticky_flag(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """A busy admission lock suppresses the record and sticks the flag."""
    observer, health = health_observer
    before = _late_counts(health)["suppressed"]
    assert observer._late_lock.acquire(blocking=False)
    try:
        with _capture_late_logs() as captured:
            observer.response_without_waiter(
                HopIdentity(connection_id="c", role="caller", request_id=5, method="m"),
                capture_stage_time(),
            )
    finally:
        observer._late_lock.release()
    assert captured == []
    assert _late_counts(health)["suppressed"] - before == 1
    assert health.snapshot()["late_admission_flags"]["contention"] is True
    with _capture_late_logs() as captured:
        observer.response_without_waiter(
            HopIdentity(connection_id="c", role="caller", request_id=6, method="m"),
            capture_stage_time(),
        )
    assert len(captured) == 1
    assert health.snapshot()["late_admission_flags"]["contention"] is True


async def test_late_clock_failure_sets_sticky_flag(
    health_observer: tuple[OtelHopObserver, HealthTracker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock failure sets the flag, counts S7 and writes nothing."""
    observer, health = health_observer

    class _BoomClock:
        @staticmethod
        def monotonic() -> float:
            raise RuntimeError("clock blew up")

        @staticmethod
        def monotonic_ns() -> int:
            return 0

    monkeypatch.setattr(
        "finecode_extension_runner.shared_telemetry.rpc_hops.time", _BoomClock
    )
    before = _late_counts(health)
    with _capture_late_logs() as captured:
        observer.response_without_waiter(
            HopIdentity(connection_id="c", role="caller", request_id=7, method="m"),
            capture_stage_time(),
        )
    assert captured == []
    assert _late_counts(health)["suppressed"] - before["suppressed"] == 1
    assert health.snapshot()["late_admission_flags"]["clock_failure"] is True


async def test_unmatched_ignores_ambient_span(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """An unrelated active span never lends its trace id to a record."""
    observer, _health = health_observer
    token = observer.open_receiver(
        HopIdentity(connection_id="c", role="receiver", request_id=8, method="m"),
        None,
        capture_stage_time(),
    )
    assert token is not None
    with _capture_late_logs() as captured, observer.handler_scope(token):
        observer.response_without_waiter(
            HopIdentity(connection_id="c", role="caller", request_id=9, method="m"),
            capture_stage_time(),
        )
    observer.close_receiver(token, "completed", capture_stage_time())
    assert len(captured) == 1
    extras, _ambient = captured[0]
    assert extras["late_kind"] == "unmatched_response"
    assert "trace_id" not in extras
    assert "rpc_generation" not in extras


async def test_colliding_ids_across_connections(
    health_observer: tuple[OtelHopObserver, HealthTracker],
    exporter: typing.Any,
) -> None:
    """Equal wire ids on two ER connections stay distinct and parented."""
    observer, _health = health_observer
    wm_health = HealthTracker(process_role="wm")
    wm_health.set_availability("traces", "active")
    wm_observer = OtelHopObserver(role="wm", health=wm_health)

    session_a = JsonRpcServerSession(tracing=None, hop_observer=observer)
    transport_a = _SessionFakeTransport()
    session_a.attach(transport_a)
    session_b = JsonRpcServerSession(tracing=None, hop_observer=observer)
    transport_b = _SessionFakeTransport()
    session_b.attach(transport_b)
    assert session_a._connection_id != session_b._connection_id

    async def reply_to(
        transport: _SessionFakeTransport, msg_id: int, result: typing.Any
    ) -> None:
        await asyncio.sleep(0.01)
        await transport._timed(
            {"jsonrpc": "2.0", "id": msg_id, "result": result},
            capture_stage_time(),
        )

    async def er_call(session: JsonRpcServerSession, method: str) -> typing.Any:
        return await session.send_request(method)

    results = await asyncio.gather(
        er_call(session_a, "m/a"),
        er_call(session_b, "m/b"),
        reply_to(transport_a, 0, "a-ok"),
        reply_to(transport_b, 0, "b-ok"),
    )
    assert results[0] == "a-ok"
    assert results[1] == "b-ok"
    assert observer._open == {}

    callers = _spans_by_name(exporter)
    spans_a = callers["jsonrpc.client/m/a"]
    spans_b = callers["jsonrpc.client/m/b"]
    assert len(spans_a) == 1 and len(spans_b) == 1
    (span_a,) = spans_a
    (span_b,) = spans_b
    assert span_a.attributes["finecode.rpc.request_id"] == "0"
    assert span_b.attributes["finecode.rpc.request_id"] == "0"
    assert (
        span_a.attributes["finecode.rpc.connection_id"]
        != span_b.attributes["finecode.rpc.connection_id"]
    )
    assert (
        span_a.attributes["finecode.rpc.generation"]
        != span_b.attributes["finecode.rpc.generation"]
    )
    assert span_a.attributes["finecode.rpc.caller_outcome"] == "result"

    wm_token = wm_observer.open_caller(
        HopIdentity(connection_id="wm", role="caller", request_id=5, method="m/w"),
        peer="er",
        at=capture_stage_time(),
    )
    assert wm_token is not None
    traceparent = wm_observer.caller_traceparent(wm_token)
    assert traceparent is not None
    carrier = {"traceparent": traceparent}
    session_c = JsonRpcServerSession(tracing=None, hop_observer=observer)
    transport_c = _SessionFakeTransport()
    session_c.attach(transport_c)
    await transport_c._timed(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "m/w",
            "_meta": {"traceparent": carrier["traceparent"]},
        },
        capture_stage_time(),
    )
    await asyncio.sleep(0.05)
    await session_c._handle_exit()
    wm_observer.close_caller(wm_token, "result", capture_stage_time())
    receiver = _only(_spans_by_name(exporter)["jsonrpc.server/m/w"])
    assert receiver.parent is not None
    caller_spans = [
        span
        for spans in _spans_by_name(exporter).values()
        for span in spans
        if span.context.span_id == receiver.parent.span_id
    ]
    assert len(caller_spans) == 1
    assert receiver.attributes["finecode.rpc.causal_link"] == "propagated"


async def test_nested_callback_parents_to_enclosing_request(
    health_observer: tuple[OtelHopObserver, HealthTracker],
    exporter: typing.Any,
) -> None:
    """An ER→WM request inside a WM→ER handler nests under that request."""
    observer, _health = health_observer
    receiver_token = observer.open_receiver(
        HopIdentity(connection_id="c", role="receiver", request_id=1, method="m/o"),
        None,
        capture_stage_time(),
    )
    assert receiver_token is not None
    with observer.handler_scope(receiver_token):
        nested = observer.open_caller(
            HopIdentity(connection_id="c", role="caller", request_id=2, method="m/n"),
            peer="wm",
            at=capture_stage_time(),
        )
        assert nested is not None
    observer.close_caller(nested, "result", capture_stage_time())
    observer.close_receiver(receiver_token, "completed", capture_stage_time())
    nested_span = _only(_spans_by_name(exporter)["jsonrpc.client/m/n"])
    receiver_span = _only(_spans_by_name(exporter)["jsonrpc.server/m/o"])
    assert nested_span.parent is not None
    assert nested_span.parent.span_id == receiver_span.context.span_id
    assert nested_span.context.trace_id == receiver_span.context.trace_id
    assert observer._open == {}


async def test_nested_callback_over_session_wire(
    health_observer: tuple[OtelHopObserver, HealthTracker],
    exporter: typing.Any,
) -> None:
    """A handler-issued ER→WM request carries the enclosing traceparent."""
    observer, _health = health_observer
    session = JsonRpcServerSession(tracing=None, hop_observer=observer)
    transport = _SessionFakeTransport()
    session.attach(transport)

    async def handler(_params: typing.Any) -> typing.Any:
        nested = await session.send_request("nested/method")
        return {"nested": nested}

    session.on_request("outer/method", handler)

    async def drive() -> None:
        await transport._timed(
            {"jsonrpc": "2.0", "id": 10, "method": "outer/method"},
            capture_stage_time(),
        )

    async def answer_nested() -> None:
        for _ in range(1000):
            nested_sends = [
                message
                for message, _hop in transport.sent
                if message.get("method") == "nested/method"
            ]
            if nested_sends:
                break
            await asyncio.sleep(0)
        assert nested_sends, "handler never sent the nested request"
        nested_id = nested_sends[0]["id"]
        await transport._timed(
            {"jsonrpc": "2.0", "id": nested_id, "result": "nested-ok"},
            capture_stage_time(),
        )

    await asyncio.gather(drive(), answer_nested())
    for _ in range(1000):
        replies = [
            message for message, _hop in transport.sent if message.get("id") == 10
        ]
        if replies and not observer._open:
            break
        await asyncio.sleep(0)
    assert replies, "outer request was never answered"
    assert replies[0]["result"] == {"nested": "nested-ok"}
    nested_envelope = next(
        message
        for message, _hop in transport.sent
        if message.get("method") == "nested/method"
    )
    receiver_span = _only(_spans_by_name(exporter)["jsonrpc.server/outer/method"])
    nested_span = _only(_spans_by_name(exporter)["jsonrpc.client/nested/method"])
    assert nested_span.parent is not None
    assert nested_span.parent.span_id == receiver_span.context.span_id
    assert nested_envelope["_meta"]["traceparent"].split("-")[1] == (
        f"{receiver_span.context.trace_id:032x}"
    )
    await session._handle_exit()
    assert observer._open == {}


async def test_reconnect_mints_fresh_connection_id(
    health_observer: tuple[OtelHopObserver, HealthTracker],
) -> None:
    """A new attach starts a new connection incarnation for later hops."""
    observer, _health = health_observer
    session = JsonRpcServerSession(tracing=None, hop_observer=observer)
    first_transport = _SessionFakeTransport()
    session.attach(first_transport)
    first_id = session._connection_id
    assert first_id
    second_transport = _SessionFakeTransport()
    session.attach(second_transport)
    assert session._connection_id
    assert session._connection_id != first_id
    token = observer.open_caller(
        HopIdentity(
            connection_id=session._connection_id,
            role="caller",
            request_id=0,
            method="m",
        ),
        peer="p",
        at=capture_stage_time(),
    )
    assert token is not None
    observer.close_caller(token, "result", capture_stage_time())
    await session._handle_exit()


async def test_hop_path_creates_no_metric_instruments(
    health_observer: tuple[OtelHopObserver, HealthTracker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hop timing must never put request ids into metric dimensions.

    The hop path records spans only; no metric instrument may be created
    while opening, staging or closing hops (a regression here would leak
    high-cardinality ids into metric attributes).
    """
    observer, _health = health_observer

    def forbidden_get_meter(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        raise AssertionError("hop path must not create metric instruments")

    monkeypatch.setattr("opentelemetry.metrics.get_meter", forbidden_get_meter)
    token = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=1, method="m/a"),
        peer="p",
        at=capture_stage_time(),
    )
    assert token is not None
    observer.stage(token, "submitted", capture_stage_time())
    observer.close_caller(token, "result", capture_stage_time())
    receiver = observer.open_receiver(
        HopIdentity(connection_id="c", role="receiver", request_id=1, method="m/b"),
        None,
        capture_stage_time(),
    )
    assert receiver is not None
    observer.stage(receiver, "dispatched", capture_stage_time())
    observer.handler_outcome(receiver, "returned")
    observer.close_receiver(receiver, "completed", capture_stage_time())
    observer.response_without_waiter(
        HopIdentity(connection_id="c", role="caller", request_id=9, method="m"),
        capture_stage_time(),
    )
    assert observer._open == {}


@pytest.mark.parametrize("availability", ["disabled", "unavailable"])
def test_inactive_traces_open_nothing_and_count_no_loss(
    exporter: InMemorySpanExporter, availability: str
) -> None:
    """Traces not ``active``: both opens return None, no span, no S3.

    A recording provider is installed, so a span appearing here could only
    come from the observer ignoring the traces setup outcome.
    """
    exporter.clear()
    health = HealthTracker(process_role="er")
    health.set_availability(
        "traces",
        typing.cast(typing.Any, availability),
        "provider_error" if availability == "unavailable" else None,
    )
    observer = OtelHopObserver(role="er", health=health)

    caller = observer.open_caller(
        HopIdentity(connection_id="c", role="caller", request_id=1, method="m"),
        peer="p",
        at=capture_stage_time(),
    )
    receiver = observer.open_receiver(
        HopIdentity(connection_id="c", role="receiver", request_id=2, method="m"),
        None,
        capture_stage_time(),
    )

    assert caller is None
    assert receiver is None
    assert exporter.get_finished_spans() == ()
    counts = health.snapshot()["signals"]["traces"]["counts"]
    assert counts["sampled_out"] == 0
    assert all(value == 0 for value in counts.values())
