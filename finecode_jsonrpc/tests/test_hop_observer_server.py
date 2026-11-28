"""Hop-observer wiring on the server session and server transports.

A hop that crosses the wire must keep its identity, order its stages, and
survive observer faults: the RPC result a peer sees never depends on
telemetry, and a transport with no observer performs no hop work beyond
capturing the frame time.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing
from typing import cast

import pytest

from finecode_jsonrpc import JsonRpcServerSession, server_transport
from finecode_jsonrpc.server_transport import (
    ServerStdioTransport,
    TcpServerTransport,
)
from finecode_jsonrpc.tracing import (
    CallerOutcome,
    HandlerOutcome,
    HopCloseReason,
    HopIdentity,
    HopStage,
    HopToken,
    IHopObserver,
    StageStatus,
    StageTime,
    capture_stage_time,
)


class _RecordingObserver:
    """IHopObserver double recording every call; optionally raising once."""

    def __init__(self, raise_on: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.reports = 0
        self._raise_on = raise_on
        self._tokens = 0

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def _maybe_raise(self, name: str) -> None:
        if self._raise_on == name:
            raise RuntimeError(f"{name} failed")

    def _token(self) -> HopToken:
        self._tokens += 1
        return cast(HopToken, ("hop", self._tokens))

    def report_contained_failure(self) -> None:
        self.reports += 1

    def open_caller(
        self, identity: HopIdentity, peer: str, at: StageTime
    ) -> HopToken | None:
        self._record("open_caller", identity, peer, at)
        self._maybe_raise("open_caller")
        return self._token()

    def caller_traceparent(self, token: HopToken) -> str | None:
        self._record("caller_traceparent", token)
        self._maybe_raise("caller_traceparent")
        return "traceparent-for-test"

    def open_receiver(
        self,
        identity: HopIdentity,
        traceparent: str | None,
        frame_read: StageTime,
    ) -> HopToken | None:
        self._record("open_receiver", identity, traceparent, frame_read)
        self._maybe_raise("open_receiver")
        return self._token()

    def stage(
        self,
        token: HopToken,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus = "observed",
    ) -> None:
        self._record("stage", token, stage, at, status)
        self._maybe_raise("stage")

    def handler_scope(self, token: HopToken) -> contextlib.AbstractContextManager[None]:
        self._record("handler_scope", token)
        self._maybe_raise("handler_scope")
        return contextlib.nullcontext()

    def handler_outcome(self, token: HopToken, outcome: HandlerOutcome) -> None:
        self._record("handler_outcome", token, outcome)
        self._maybe_raise("handler_outcome")

    def close_caller(
        self, token: HopToken, outcome: CallerOutcome, at: StageTime
    ) -> None:
        self._record("close_caller", token, outcome, at)
        self._maybe_raise("close_caller")

    def close_receiver(
        self, token: HopToken, reason: HopCloseReason, at: StageTime
    ) -> None:
        self._record("close_receiver", token, reason, at)
        self._maybe_raise("close_receiver")

    def response_without_waiter(self, identity: HopIdentity, at: StageTime) -> None:
        self._record("response_without_waiter", identity, at)
        self._maybe_raise("response_without_waiter")

    def connection_closed(self, connection_id: str) -> None:
        self._record("connection_closed", connection_id)
        self._maybe_raise("connection_closed")


def _as_observer(fake: _RecordingObserver) -> IHopObserver:
    return cast(IHopObserver, fake)


class _FakeTransport:
    """Server transport double with the hop-aware surface."""

    def __init__(self) -> None:
        self.sent: list[tuple[dict[str, typing.Any], HopToken | None]] = []
        self.observer: IHopObserver | None = None
        self._timed: typing.Any = None
        self._plain: typing.Any = None
        self._readable_id = "test-client"

    def on_message(self, callback: typing.Any) -> None:
        self._plain = callback

    def on_message_timed(self, callback: typing.Any) -> None:
        self._timed = callback

    def attach_hop_observer(self, observer: IHopObserver | None) -> None:
        self.observer = observer

    def on_exit(self, _callback: typing.Any) -> None:
        return None

    def send(
        self, message: dict[str, typing.Any], *, hop: HopToken | None = None
    ) -> None:
        self.sent.append((message, hop))


def _make_session(
    observer: _RecordingObserver,
) -> tuple[JsonRpcServerSession, _FakeTransport]:
    session = JsonRpcServerSession(hop_observer=_as_observer(observer))
    transport = _FakeTransport()
    session.attach(transport)
    return session, transport


async def _deliver(transport: _FakeTransport, message: dict[str, typing.Any]) -> None:
    assert transport._timed is not None
    await transport._timed(message, capture_stage_time())
    for _ in range(20):
        if transport.sent:
            break
        await asyncio.sleep(0)


def _flow(observer: _RecordingObserver) -> list[object]:
    out: list[object] = []
    for name, args in observer.calls:
        if name == "open_receiver":
            out.append("open_receiver")
        elif name == "stage":
            out.append(args[1])
        elif name == "handler_scope":
            out.append("handler_scope")
        elif name == "handler_outcome":
            out.append(("handler_outcome", args[1]))
    return out


async def test_receiver_success_stage_order() -> None:
    """A handled request orders dispatch, handler scope and outcome stages."""
    observer = _RecordingObserver()
    session, transport = _make_session(observer)

    async def echo(params: dict[str, typing.Any] | None) -> typing.Any:
        return {"echo": params}

    session.on_request("test/echo", echo)
    await _deliver(transport, {"jsonrpc": "2.0", "id": 1, "method": "test/echo"})

    assert len(transport.sent) == 1
    message, hop = transport.sent[0]
    assert message["result"] == {"echo": None}
    assert hop is not None
    assert _flow(observer) == [
        "open_receiver",
        "dispatched",
        "handler_started",
        "handler_scope",
        "handler_ended",
        ("handler_outcome", "returned"),
    ]
    identity = observer.calls[0][1][0]
    assert isinstance(identity, HopIdentity)
    assert identity.role == "receiver"
    assert identity.request_id == 1
    assert isinstance(identity.request_id, int)


async def test_receiver_early_rejection_skips_handler_stages() -> None:
    """An unknown method is rejected with outcome and no handler scope."""
    observer = _RecordingObserver()
    _session, transport = _make_session(observer)

    await _deliver(transport, {"jsonrpc": "2.0", "id": 2, "method": "nope"})

    message, hop = transport.sent[0]
    assert message["error"]["code"] == -32601
    assert hop is not None
    assert ("handler_outcome", (hop, "not_executed")) in [
        (name, args) for name, args in observer.calls
    ]
    assert not [call for call in observer.calls if call[0] == "handler_scope"]
    staged = [args[1] for name, args in observer.calls if name == "stage"]
    assert staged == ["dispatched"]


async def test_caller_success_carries_traceparent() -> None:
    """An outbound request envelope carries the caller's traceparent."""
    observer = _RecordingObserver()
    session, transport = _make_session(observer)

    async def reply() -> None:
        await asyncio.sleep(0.01)
        await transport._timed(
            {"jsonrpc": "2.0", "id": 0, "result": 42}, capture_stage_time()
        )

    result = await asyncio.gather(session.send_request("m/a"), reply())
    assert result[0] == 42

    message, hop = transport.sent[0]
    assert message["_meta"] == {"traceparent": "traceparent-for-test"}
    assert hop is not None
    assert any(
        name == "close_caller" and args[1] == "result" for name, args in observer.calls
    )


async def test_caller_timeout_then_late_response_is_reported() -> None:
    """A timed-out request closes, and its late answer is still identified."""
    observer = _RecordingObserver()
    session, transport = _make_session(observer)

    with pytest.raises(TimeoutError):
        await session.send_request("m/slow", timeout=0.01)
    assert any(
        name == "close_caller" and args[1] == "timeout" for name, args in observer.calls
    )

    observer.calls.clear()
    await transport._timed(
        {"jsonrpc": "2.0", "id": 0, "result": "late"}, capture_stage_time()
    )
    reported = [
        args[0] for name, args in observer.calls if name == "response_without_waiter"
    ]
    assert len(reported) == 1
    identity = reported[0]
    assert isinstance(identity, HopIdentity)
    assert (identity.role, identity.request_id, identity.method) == (
        "caller",
        0,
        "m/slow",
    )


async def test_exit_disconnects_open_callers() -> None:
    """Transport loss fails the waiter as disconnected and closes the hop."""
    observer = _RecordingObserver()
    session, _transport = _make_session(observer)

    async def hanging() -> typing.Any:
        try:
            await session.send_request("m/hang", timeout=30)
        except RuntimeError:
            return "disconnected"
        return "unexpected"

    task = asyncio.ensure_future(hanging())
    await asyncio.sleep(0.02)
    await session._handle_exit()
    assert await task == "disconnected"
    assert any(
        name == "close_caller" and args[1] == "disconnected"
        for name, args in observer.calls
    )
    assert any(name == "connection_closed" for name, _ in observer.calls)


@pytest.mark.parametrize(
    ("raise_on", "expect_report"),
    [
        ("open_caller", True),
        ("caller_traceparent", True),
        ("open_receiver", True),
        ("stage", True),
        ("handler_scope", True),
        ("handler_outcome", True),
        ("close_caller", True),
        # Unexercised in a success flow (writer close, late reply, exit),
        # so nothing is contained — outcomes still hold.
        ("close_receiver", False),
        ("response_without_waiter", False),
        ("connection_closed", False),
    ],
)
async def test_raising_observer_leaves_outcomes_unchanged(
    raise_on: str, expect_report: bool
) -> None:
    """An observer fault at any hook never changes the RPC result on either side."""
    observer = _RecordingObserver(raise_on=raise_on)
    session, transport = _make_session(observer)

    async def echo(_params: dict[str, typing.Any] | None) -> typing.Any:
        return "ok"

    session.on_request("test/echo", echo)
    await _deliver(transport, {"jsonrpc": "2.0", "id": 1, "method": "test/echo"})
    message, _hop = transport.sent[0]
    assert message["result"] == "ok"

    async def reply() -> None:
        await asyncio.sleep(0.01)
        await transport._timed(
            {"jsonrpc": "2.0", "id": 0, "result": 42}, capture_stage_time()
        )

    transport.sent.clear()
    result = await asyncio.gather(session.send_request("m/a"), reply())
    assert result[0] == 42
    assert (observer.reports >= 1) == expect_report


async def test_stdio_writer_records_drain_unsupported_and_close() -> None:
    """A stdio reply marks drain stages unsupported and completes the hop."""
    observer = _RecordingObserver()
    transport = ServerStdioTransport()
    transport.attach_hop_observer(_as_observer(observer))

    written: list[bytes] = []

    class _Writer:
        def write(self, _data: bytes) -> None:
            written.append(_data)

    async def run_writer() -> None:
        await transport._write_messages(typing.cast(typing.Any, _Writer()))

    task = asyncio.ensure_future(run_writer())
    token = cast(HopToken, object())
    transport.send({"jsonrpc": "2.0", "id": 1, "result": 1}, hop=token)
    for _ in range(100):
        if any(name == "close_receiver" for name, _ in observer.calls):
            break
        await asyncio.sleep(0)
    _ = task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert written != []
    kinds = [(args[1], args[3]) for name, args in observer.calls if name == "stage"]
    assert ("write_returned", "observed") in kinds
    assert ("drain_started", "unsupported") in kinds
    assert ("drain_returned", "unsupported") in kinds
    assert any(
        name == "close_receiver" and args[1] == "completed"
        for name, args in observer.calls
    )


async def test_stdio_write_failure_closes_write_failed() -> None:
    """A failed stdio write marks the stage and closes the hop as failed."""
    observer = _RecordingObserver()
    transport = ServerStdioTransport()
    transport.attach_hop_observer(_as_observer(observer))

    class _FailingWriter:
        def write(self, _data: bytes) -> None:
            raise RuntimeError("nope")

    token = cast(HopToken, object())
    transport.send({"jsonrpc": "2.0", "id": 1, "result": 1}, hop=token)
    await transport._write_messages(typing.cast(typing.Any, _FailingWriter()))
    # Writer logs the error and stops; the hop is already closed.
    assert any(
        name == "stage" and args[1] == "write_returned" and args[3] == "failed"
        for name, args in observer.calls
    )
    assert any(
        name == "close_receiver" and args[1] == "write_failed"
        for name, args in observer.calls
    )


async def test_tcp_writer_records_drain_stages() -> None:
    """A TCP reply brackets the drain and completes the hop afterwards."""
    observer = _RecordingObserver()
    reader = asyncio.StreamReader()
    writer_calls: list[str] = []

    class _Writer:
        def write(self, _data: bytes) -> None:
            writer_calls.append("write")

        async def drain(self) -> None:
            writer_calls.append("drain")

        def close(self) -> None:
            writer_calls.append("close")

    transport = TcpServerTransport(
        typing.cast(typing.Any, reader), typing.cast(typing.Any, _Writer())
    )
    transport.attach_hop_observer(_as_observer(observer))

    async def run_writer() -> None:
        await transport._write_messages(typing.cast(typing.Any, _Writer()))

    task = asyncio.ensure_future(run_writer())
    token = cast(HopToken, object())
    transport._loop = asyncio.get_running_loop()
    transport.send({"jsonrpc": "2.0", "id": 1, "result": 1}, hop=token)
    for _ in range(100):
        if any(name == "close_receiver" for name, _ in observer.calls):
            break
        await asyncio.sleep(0)
    _ = task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    names = [args[1] for name, args in observer.calls if name == "stage"]
    assert names == [
        "submitted",
        "enqueued",
        "dequeued",
        "write_returned",
        "drain_started",
        "drain_returned",
    ]
    assert writer_calls[:2] == ["write", "drain"]
    assert any(
        name == "close_receiver" and args[1] == "completed"
        for name, args in observer.calls
    )


async def test_enqueued_recorded_at_insertion_not_submission() -> None:
    """With a running loop, ``enqueued`` fires in the queued callback."""
    observer = _RecordingObserver()
    transport = ServerStdioTransport()
    transport.attach_hop_observer(_as_observer(observer))
    transport._loop = asyncio.get_running_loop()

    token = cast(HopToken, object())
    transport.send({"jsonrpc": "2.0", "method": "m"}, hop=token)
    staged = [args[1] for name, args in observer.calls if name == "stage"]
    assert "submitted" in staged
    assert "enqueued" not in staged
    for _ in range(20):
        staged = [args[1] for name, args in observer.calls if name == "stage"]
        if "enqueued" in staged:
            break
        await asyncio.sleep(0)
    assert "enqueued" in staged


async def test_frame_time_reaches_timed_handler() -> None:
    """The read path hands the frame time to the timed handler, before parsing."""
    transport = ServerStdioTransport(framing="newline")
    seen: list[tuple[dict[str, typing.Any], StageTime]] = []

    async def on_timed(message: dict[str, typing.Any], frame_read: StageTime) -> None:
        seen.append((message, frame_read))

    transport.on_message_timed(on_timed)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc": "2.0", "method": "m"}\n')
    reader.feed_eof()
    await transport._read_messages(reader)
    assert len(seen) == 1
    message, frame_read = seen[0]
    assert message == {"jsonrpc": "2.0", "method": "m"}
    assert isinstance(frame_read, StageTime)


def test_no_observer_performs_no_hop_clock_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an observer, sending performs no stage clock reads at all."""
    reads: list[None] = []
    original = server_transport.capture_stage_time

    def counting() -> StageTime:
        reads.append(None)
        return original()

    monkeypatch.setattr(server_transport, "capture_stage_time", counting)
    transport = ServerStdioTransport()
    transport.send({"jsonrpc": "2.0", "method": "m"})
    assert reads == []
    item = transport._out_queue.get_nowait()
    assert item is not None
    assert item.hop is None
