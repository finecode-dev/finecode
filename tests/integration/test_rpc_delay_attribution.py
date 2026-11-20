"""Delay attribution: each injected 300 ms delay lands in one stage gap.

Eight probes cover both directions and all four injection points from PA2:
sender write, receiver dispatch, handler, and caller response processing.
The peer is a real ER-side subprocess running the production transport,
session and hop observer over OS pipes; it writes spans to a file while the
test process keeps its own spans in memory. Gaps are compared on each
process's own monotonic clock only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
import typing
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from finecode_extension_runner.shared_telemetry.health import HealthTracker
from finecode_extension_runner.shared_telemetry.rpc_hops import OtelHopObserver
from finecode_jsonrpc.server_transport import ServerStdioTransport
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from finecode_jsonrpc import JsonRpcServerSession

_PEER_SCRIPT = Path(__file__).with_name("rpc_probe_peer.py")

_PROBE_MS = 300.0
_LARGE_GAP_MS = 290.0
_SMALL_GAP_MS = 100.0


def _span_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.jsonl"
    _ = path.write_text("", encoding="utf-8")
    return path


def _read_peer_spans(path: Path) -> list[dict[str, typing.Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event_monos(span: dict[str, typing.Any]) -> list[tuple[str, int]]:
    """Stage events carrying a monotonic timestamp (unsupported drains do not)."""
    ordered = sorted(span["events"], key=lambda event: event["timestamp"])
    return [
        (event["name"], event["attributes"]["finecode.mono_ns"])
        for event in ordered
        if "finecode.mono_ns" in event["attributes"]
    ]


def _assert_single_large_gap(
    events: list[tuple[str, int]], expected: tuple[str, str]
) -> None:
    assert events, "span has no stage events"
    monos = [mono for _, mono in events]
    assert monos == sorted(monos), "stage events out of order"
    gaps = [
        ((events[i][0], events[i + 1][0]), (monos[i + 1] - monos[i]) / 1e6)
        for i in range(len(events) - 1)
    ]
    large = [(pair, ms) for pair, ms in gaps if ms >= _LARGE_GAP_MS]
    assert len(large) == 1, gaps
    assert large[0][0] == expected, gaps
    small = [ms for pair, ms in gaps if pair != expected]
    assert small, "span needs at least one small gap besides the injection"
    assert all(ms < _SMALL_GAP_MS for ms in small), gaps


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
    health = HealthTracker(process_role="wm")
    health.set_availability("traces", "active")
    return OtelHopObserver(role="wm", health=health)


class _Peer:
    """One WM-side session wired to one ER-side peer subprocess."""

    def __init__(self, tmp_path: Path, peer_args: list[str]) -> None:
        self.span_file = _span_file(tmp_path, f"peer-{time.monotonic_ns()}")
        self.err_path = tmp_path / f"peer-{time.monotonic_ns()}.stderr.log"
        self._err_handle = self.err_path.open("wb")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(_PEER_SCRIPT),
                "--span-file",
                str(self.span_file),
                *peer_args,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._err_handle,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.transport = ServerStdioTransport()
        self.session: JsonRpcServerSession | None = None

    async def start(self, observer: OtelHopObserver) -> JsonRpcServerSession:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        session = JsonRpcServerSession(tracing=None, hop_observer=observer)
        session.attach(self.transport)
        await self.transport.start(
            stdin_buf=typing.cast(typing.BinaryIO, self.proc.stdout),
            stdout_buf=typing.cast(typing.BinaryIO, self.proc.stdin),
        )
        self.session = session
        return session

    async def stop(self) -> None:
        await self.transport.stop()
        self.proc.terminate()
        try:
            _ = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _ = self.proc.wait(timeout=10)
        self._err_handle.close()
        assert self.proc.returncode is not None

    def peer_spans(self) -> list[dict[str, typing.Any]]:
        return _read_peer_spans(self.span_file)

    async def wait_for_peer_span(self, name: str) -> dict[str, typing.Any]:
        for _ in range(1000):
            for span in self.peer_spans():
                if span["name"] == name and span["end_time"]:
                    return span
            await asyncio.sleep(0)
        raise AssertionError(f"peer span {name} never finished: {self.err_tail()}")

    def err_tail(self) -> str:
        try:
            return self.err_path.read_text(encoding="utf-8")[-2000:]
        except OSError:
            return "<no stderr captured>"


def _own_span(exporter: typing.Any, name: str) -> typing.Any:
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, [s.name for s in exporter.get_finished_spans()]
    return matches[0]


def _own_events(span: typing.Any) -> list[tuple[str, int]]:
    return [
        (event.name, event.attributes["finecode.mono_ns"])
        for event in span.events
        if "finecode.mono_ns" in event.attributes
    ]


@contextlib.asynccontextmanager
async def _connected_peer(
    tmp_path: Path, observer: OtelHopObserver, peer_args: list[str]
) -> AsyncGenerator[_Peer, None]:
    peer = _Peer(tmp_path, peer_args)
    try:
        await peer.start(observer)
        yield peer
    finally:
        await peer.stop()


async def _prepare(
    peer: _Peer, observer: OtelHopObserver, exporter: typing.Any
) -> None:
    """One warmup round-trip so peer startup never lands in a measured gap."""
    assert peer.session is not None
    result = await asyncio.wait_for(peer.session.send_request("probe/echo"), 15)
    assert result == {"echo": None}
    exporter.clear()
    _ = peer.span_file.write_text("", encoding="utf-8")
    assert not observer._open


async def _wm_to_er_exchange(
    peer: _Peer, observer: OtelHopObserver, payload: typing.Any = None
) -> dict[str, typing.Any]:
    assert peer.session is not None
    result = await peer.session.send_request("probe/echo", payload)
    for _ in range(1000):
        if not observer._open:
            break
        await asyncio.sleep(0)
    assert not observer._open
    return typing.cast(dict[str, typing.Any], result)


async def _er_to_wm_exchange(
    peer: _Peer,
    observer: OtelHopObserver,
    payload: typing.Any = None,
    handler: typing.Callable[[typing.Any], typing.Any] | None = None,
) -> dict[str, typing.Any]:
    assert peer.session is not None

    async def on_event(params: typing.Any) -> typing.Any:
        return {"event": params}

    peer.session.on_request("probe/event", handler or on_event)
    result = await peer.session.send_request("probe/callback", payload)
    for _ in range(1000):
        if not observer._open:
            break
        await asyncio.sleep(0)
    assert not observer._open
    return typing.cast(dict[str, typing.Any], result)


def _patch_writer_delay(transport: ServerStdioTransport, delay_s: float) -> None:
    """Sleep before every sender write on this transport."""
    write_transport: typing.Any = transport._write_transport
    assert write_transport is not None
    original_write = write_transport.write

    def slow_write(data: typing.Any) -> typing.Any:
        if delay_s > 0:
            time.sleep(delay_s)
        return original_write(data)

    write_transport.write = slow_write


def _patch_dispatch_delay(transport: ServerStdioTransport, delay_s: float) -> None:
    """Sleep in the dispatch step right after each frame read."""
    original_dispatch = transport._dispatch_message

    async def slow_dispatch(message: typing.Any, frame_read: typing.Any) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        await original_dispatch(message, frame_read)

    transport._dispatch_message = slow_dispatch


def _patch_finish_delay(session: JsonRpcServerSession, delay_s: float) -> None:
    """Sleep in the caller-completion path before the hop closes."""
    original_finish = session._finish_caller

    def slow_finish(hop: typing.Any, outcome: typing.Any) -> None:
        if delay_s > 0:
            time.sleep(delay_s)
        original_finish(hop, outcome)

    session._finish_caller = slow_finish


def _assert_small_gaps_only(events: list[tuple[str, int]]) -> None:
    monos = [mono for _, mono in events]
    gaps = [(monos[i + 1] - monos[i]) / 1e6 for i in range(len(monos) - 1)]
    assert gaps, "span has no stage events"
    assert all(ms < _SMALL_GAP_MS for ms in gaps), gaps


async def test_wm_to_er_sender_write_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """A 300 ms sender write shows up only in the caller drain gap."""
    async with _connected_peer(tmp_path, observer, []) as peer:
        await _prepare(peer, observer, exporter)
        assert peer.session is not None
        _patch_writer_delay(peer.transport, 0.3)
        result = await asyncio.wait_for(_wm_to_er_exchange(peer, observer), timeout=30)
        assert result == {"echo": None}
        receiver = await peer.wait_for_peer_span("jsonrpc.server/probe/echo")
    caller = _own_span(exporter, "jsonrpc.client/probe/echo")
    _assert_single_large_gap(
        _own_events(caller),
        ("rpc.stage.dequeued", "rpc.stage.write_returned"),
    )
    _assert_small_gaps_only(_event_monos(receiver))


async def test_wm_to_er_receiver_dispatch_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """A 300 ms receiver dispatch shows up only in the frame-read gap."""
    async with _connected_peer(tmp_path, observer, ["--dispatch-ms", "300"]) as peer:
        await _prepare(peer, observer, exporter)
        result = await asyncio.wait_for(_wm_to_er_exchange(peer, observer), timeout=30)
        assert result == {"echo": None}
        receiver = await peer.wait_for_peer_span("jsonrpc.server/probe/echo")
    caller = _own_span(exporter, "jsonrpc.client/probe/echo")
    _assert_single_large_gap(
        _event_monos(receiver), ("rpc.stage.frame_read", "rpc.stage.dispatched")
    )
    _assert_single_large_gap(
        _own_events(caller),
        ("rpc.stage.write_returned", "rpc.stage.frame_read"),
    )


async def test_wm_to_er_handler_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """A 300 ms handler shows up only in the handler execution gap."""
    async with _connected_peer(tmp_path, observer, ["--handler-ms", "300"]) as peer:
        await _prepare(peer, observer, exporter)
        result = await asyncio.wait_for(_wm_to_er_exchange(peer, observer), timeout=30)
        assert result == {"echo": None}
        receiver = await peer.wait_for_peer_span("jsonrpc.server/probe/echo")
    caller = _own_span(exporter, "jsonrpc.client/probe/echo")
    _assert_single_large_gap(
        _event_monos(receiver),
        ("rpc.stage.handler_started", "rpc.stage.handler_ended"),
    )
    _assert_single_large_gap(
        _own_events(caller),
        ("rpc.stage.write_returned", "rpc.stage.frame_read"),
    )


async def test_wm_to_er_caller_response_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """Slow caller response processing shows up only past resolution."""
    async with _connected_peer(tmp_path, observer, []) as peer:
        await _prepare(peer, observer, exporter)
        assert peer.session is not None
        _patch_finish_delay(peer.session, 0.3)
        result = await asyncio.wait_for(_wm_to_er_exchange(peer, observer), timeout=30)
        assert result == {"echo": None}
        receiver = await peer.wait_for_peer_span("jsonrpc.server/probe/echo")
    caller = _own_span(exporter, "jsonrpc.client/probe/echo")
    _assert_single_large_gap(
        _own_events(caller),
        ("rpc.stage.response_resolved", "rpc.stage.caller_completed"),
    )
    _assert_small_gaps_only(_event_monos(receiver))


async def test_er_to_wm_sender_write_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """A 300 ms sender write shows up only in the caller drain gap."""
    async with _connected_peer(tmp_path, observer, ["--write-ms", "300"]) as peer:
        await _prepare(peer, observer, exporter)
        result = await asyncio.wait_for(_er_to_wm_exchange(peer, observer), timeout=30)
        assert result == {"seen": {"event": None}}
        caller = await peer.wait_for_peer_span("jsonrpc.client/probe/event")
    receiver = _own_span(exporter, "jsonrpc.server/probe/event")
    _assert_single_large_gap(
        _event_monos(caller),
        ("rpc.stage.dequeued", "rpc.stage.write_returned"),
    )
    _assert_small_gaps_only(_own_events(receiver))


async def test_er_to_wm_receiver_dispatch_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """A 300 ms receiver dispatch shows up only in the frame-read gap."""
    async with _connected_peer(tmp_path, observer, []) as peer:
        await _prepare(peer, observer, exporter)
        assert peer.session is not None
        _patch_dispatch_delay(peer.transport, 0.3)
        result = await asyncio.wait_for(_er_to_wm_exchange(peer, observer), timeout=30)
        assert result == {"seen": {"event": None}}
        caller = await peer.wait_for_peer_span("jsonrpc.client/probe/event")
    receiver = _own_span(exporter, "jsonrpc.server/probe/event")
    _assert_single_large_gap(
        _own_events(receiver),
        ("rpc.stage.frame_read", "rpc.stage.dispatched"),
    )
    _assert_single_large_gap(
        _event_monos(caller),
        ("rpc.stage.write_returned", "rpc.stage.frame_read"),
    )


async def test_er_to_wm_handler_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """A 300 ms handler shows up only in the handler execution gap."""

    async def slow_event(params: typing.Any) -> typing.Any:
        await asyncio.sleep(0.3)
        return {"event": params}

    async with _connected_peer(tmp_path, observer, []) as peer:
        await _prepare(peer, observer, exporter)
        result = await asyncio.wait_for(
            _er_to_wm_exchange(peer, observer, handler=slow_event), timeout=30
        )
        assert result == {"seen": {"event": None}}
        caller = await peer.wait_for_peer_span("jsonrpc.client/probe/event")
    receiver = _own_span(exporter, "jsonrpc.server/probe/event")
    _assert_single_large_gap(
        _own_events(receiver),
        ("rpc.stage.handler_started", "rpc.stage.handler_ended"),
    )
    _assert_single_large_gap(
        _event_monos(caller),
        ("rpc.stage.write_returned", "rpc.stage.frame_read"),
    )


async def test_er_to_wm_caller_response_delay(
    observer: OtelHopObserver, exporter: typing.Any, tmp_path: Path
) -> None:
    """Slow caller response processing shows up only past resolution."""
    async with _connected_peer(tmp_path, observer, ["--finish-ms", "300"]) as peer:
        await _prepare(peer, observer, exporter)
        result = await asyncio.wait_for(_er_to_wm_exchange(peer, observer), timeout=30)
        assert result == {"seen": {"event": None}}
        caller = await peer.wait_for_peer_span("jsonrpc.client/probe/event")
    receiver = _own_span(exporter, "jsonrpc.server/probe/event")
    _assert_single_large_gap(
        _event_monos(caller),
        ("rpc.stage.response_resolved", "rpc.stage.caller_completed"),
    )
    _assert_small_gaps_only(_own_events(receiver))
