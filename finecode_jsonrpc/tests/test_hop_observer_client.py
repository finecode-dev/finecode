"""Hop-observer wiring on the JSON-RPC client.

The client's caller hops must close exactly once with the outcome the waiter
observed, receiver hops must bracket the handler, and a broken observer must
never change what the caller receives.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import typing
from typing import cast

import pytest

from finecode_jsonrpc import client as jc
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


def _make_client(observer: _RecordingObserver | None = None) -> jc.JsonRpcClient:
    message_types: dict[str, typing.Any] = {
        "r/m": (dict, dict, _REnv, None),
        "in/m": (_REnv, dict, str, None),
        "in/strict": (_StrictEnv, dict, str, None),
        # Replies structured into _StrictEnv always fail: the envelope has
        # neither a method nor params member.
        "bad/r": (dict, dict, _StrictEnv, None),
    }
    return jc.JsonRpcClient(
        message_types=message_types,
        readable_id="test-server",
        hop_observer=_as_observer(observer) if observer is not None else None,
    )


def _body(frame: jc.OutgoingFrame) -> dict[str, typing.Any]:
    raw = frame.data.decode()
    _header, _, payload = raw.partition("\r\n\r\n")
    return typing.cast(dict[str, typing.Any], json.loads(payload))


def _sent_frames(client: jc.JsonRpcClient) -> list[jc.OutgoingFrame]:
    frames: list[jc.OutgoingFrame] = []
    while True:
        try:
            frames.append(client.out_message_queue.sync_q.get_nowait())
        except Exception:  # noqa: BLE001
            return frames


def _close_outcomes(observer: _RecordingObserver) -> list[CallerOutcome]:
    return [
        typing.cast(CallerOutcome, args[1])
        for name, args in observer.calls
        if name == "close_caller"
    ]


async def test_async_success_stage_order() -> None:
    """A resolved async request orders caller stages around the reply."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    task = asyncio.ensure_future(client.send_request("r/m"))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))

    frames = _sent_frames(client)
    assert len(frames) == 1
    assert _body(frames[0])["_meta"] == {"traceparent": "traceparent-for-test"}

    await client.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "result": "ok"}, capture_stage_time()
    )
    response = await task
    assert response.result == "ok"

    names = [name for name, _ in observer.calls]
    assert names[0] == "open_caller"
    assert [args[1] for name, args in observer.calls if name == "stage"] == [
        "submitted",
        "enqueued",
        "frame_read",
        "response_resolved",
        "caller_completed",
    ]
    assert _close_outcomes(observer) == ["result"]


async def test_async_error_outcome() -> None:
    """A remote error closes the caller hop as a remote error."""
    observer = _RecordingObserver()
    client = _make_client(observer)

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
    assert _close_outcomes(observer) == ["remote_error"]


async def test_async_invalid_response_outcome() -> None:
    """An undecodable reply closes the caller hop as an invalid response."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    task = asyncio.ensure_future(client.send_request("bad/r"))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "result": {"not": "a string"}},
        capture_stage_time(),
    )
    with pytest.raises(jc.InvalidResponse):
        await task
    assert _close_outcomes(observer) == ["invalid_response"]


async def test_async_timeout_then_late_response_is_matched() -> None:
    """A timed-out request keeps its identity so the late reply is matched."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    with pytest.raises(jc.ResponseTimeout):
        await client.send_request("r/m", timeout=0.01)
    assert _close_outcomes(observer) == ["timeout"]

    observer.calls.clear()
    await client.handle_message(
        {"jsonrpc": "2.0", "id": "missing", "result": "late"},
        capture_stage_time(),
    )
    reported = [
        args[0] for name, args in observer.calls if name == "response_without_waiter"
    ]
    assert len(reported) == 1
    identity = reported[0]
    assert isinstance(identity, HopIdentity)
    assert (identity.role, identity.method) == ("caller", "")

    stale_id = next(iter(client._async_request_hops))
    observer.calls.clear()
    await client.handle_message(
        {"jsonrpc": "2.0", "id": stale_id, "result": "late"},
        capture_stage_time(),
    )
    reported = [
        args[0] for name, args in observer.calls if name == "response_without_waiter"
    ]
    assert len(reported) == 1
    identity = reported[0]
    assert isinstance(identity, HopIdentity)
    assert (identity.role, identity.request_id, identity.method) == (
        "caller",
        stale_id,
        "r/m",
    )


async def test_async_cancel_outcome() -> None:
    """Cancelling the waiter closes the caller hop as cancelled."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    task = asyncio.ensure_future(client.send_request("r/m"))
    await asyncio.sleep(0)
    _ = task.cancel()
    with pytest.raises(jc.RequestCancelledError):
        await task
    assert _close_outcomes(observer) == ["cancelled"]
    # The hop entry is retained so a late reply can still be matched.
    assert len(client._async_request_hops) == 1


async def test_disconnect_outcome() -> None:
    """A dead channel closes the caller hop as disconnected."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    task = asyncio.ensure_future(client.send_request("r/m"))
    await asyncio.sleep(0)
    client._fail_pending_requests("boom")
    with pytest.raises(jc.ServerStoppedError):
        await task
    assert _close_outcomes(observer) == ["disconnected"]


async def test_send_failed_outcome() -> None:
    """An unserializable request never reaches the wire and fails to send."""
    observer = _RecordingObserver()
    client = jc.JsonRpcClient(
        message_types={"bad/m": (None, object, str, None)},
        readable_id="test-server",
        hop_observer=_as_observer(observer),
    )
    with pytest.raises(jc.InvalidResponse):
        await client.send_request("bad/m", object())
    assert _close_outcomes(observer) == ["send_failed"]
    assert _sent_frames(client) == []


async def test_sync_future_closes_on_completion_not_at_return() -> None:
    """A sync caller hop ends in the done-callback, never at send return."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    future = client.send_request_sync("r/m")
    (frame,) = _sent_frames(client)
    assert _body(frame)["_meta"] == {"traceparent": "traceparent-for-test"}
    assert _close_outcomes(observer) == []

    future.set_result("ok")
    assert _close_outcomes(observer) == ["result"]
    staged = [args[1] for name, args in observer.calls if name == "stage"]
    assert staged == ["submitted", "enqueued", "caller_completed"]


async def test_sync_future_cancel_reports_cancelled() -> None:
    """Cancelling a sync future reports cancellation through the callback."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    future = client.send_request_sync("r/m")
    assert _close_outcomes(observer) == []
    _ = future.cancel()
    assert _close_outcomes(observer) == ["cancelled"]


async def test_sync_future_disconnect_reports_disconnected() -> None:
    """A dead channel reports the sync waiter as disconnected."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    future = client.send_request_sync("r/m")
    client._fail_pending_requests("boom")
    with pytest.raises(jc.ServerStoppedError):
        future.result()
    assert _close_outcomes(observer) == ["disconnected"]


async def test_incoming_request_observed() -> None:
    """An inbound request brackets its handler and answers with the hop."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    async def impl(_params: typing.Any) -> typing.Any:
        return "done"

    client.feature("in/m", impl)
    await client.handle_message(
        {"jsonrpc": "2.0", "id": 7, "method": "in/m", "params": {}},
        capture_stage_time(),
    )
    assert len(client._async_tasks) == 1
    await client._async_tasks[0]

    frames = _sent_frames(client)
    assert len(frames) == 1
    assert frames[0].hop is not None
    assert _body(frames[0]) == {"jsonrpc": "2.0", "id": 7, "result": "done"}

    names = [
        (name, args[1] if name in ("stage", "handler_outcome") else None)
        for name, args in observer.calls
    ]
    kinds = [name for name, _ in observer.calls]
    assert kinds[0] == "open_receiver"
    assert ("handler_outcome", "returned") in names


async def test_incoming_request_early_rejections() -> None:
    """Unregistered methods and bad params are rejected without a handler."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    await client.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "nope"}, capture_stage_time()
    )
    await client.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "in/strict",
            "params": "not-a-dict",
        },
        capture_stage_time(),
    )
    # Neither rejection schedules a handler task.
    assert client._async_tasks == []

    frames = _sent_frames(client)
    assert len(frames) == 2
    assert all(frame.hop is not None for frame in frames)
    outcomes = [args[1] for name, args in observer.calls if name == "handler_outcome"]
    assert outcomes == ["not_executed", "not_executed"]
    assert [_body(frame)["error"]["code"] for frame in frames] == [
        -32601,
        -32602,
    ]


async def test_handler_cancellation_reports_cancelled() -> None:
    """A cancelled handler still answers, and reports cancellation."""
    observer = _RecordingObserver()
    client = _make_client(observer)

    async def impl(_params: typing.Any) -> typing.Any:
        raise jc.RequestCancelledError(request_id=99)

    client.feature("in/m", impl)
    await client.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "in/m", "params": {}},
        capture_stage_time(),
    )
    await client._async_tasks[0]

    frames = _sent_frames(client)
    assert len(frames) == 1
    assert frames[0].hop is not None
    outcomes = [args[1] for name, args in observer.calls if name == "handler_outcome"]
    assert outcomes == ["cancelled"]


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
    client = _make_client(observer)

    async def impl(_params: typing.Any) -> typing.Any:
        return "ok"

    client.feature("in/m", impl)
    task = asyncio.ensure_future(client.send_request("r/m"))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "result": "ok"}, capture_stage_time()
    )
    assert (await task).result == "ok"

    await client.handle_message(
        {"jsonrpc": "2.0", "id": 9, "method": "in/m", "params": {}},
        capture_stage_time(),
    )
    await asyncio.gather(*client._async_tasks)
    frames = _sent_frames(client)
    assert len(frames) == 2
    assert (observer.reports >= 1) == expect_report


async def test_no_observer_performs_no_hop_clock_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an observer the client records no stage clock reads at all."""
    reads: list[None] = []
    original = jc.capture_stage_time

    def counting() -> StageTime:
        reads.append(None)
        return original()

    monkeypatch.setattr(jc, "capture_stage_time", counting)
    client = _make_client(None)

    task = asyncio.ensure_future(client.send_request("r/m"))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message({"jsonrpc": "2.0", "id": msg_id, "result": "ok"})
    response = await task
    assert response.result == "ok"
    future = client.send_request_sync("r/m")
    assert not future.done()
    assert reads == []
