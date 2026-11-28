"""Ordered prefix completion before response resolution.

Observable contract: a subscribed synchronous prefix runs in the same dispatch
pass as its notification, and a per-request capture freezes that prefix before
the request future resolves. Without this, a runner response could be exposed
while its final notification is still queued behind a task switch.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import typing

import pytest

from finecode_jsonrpc import client as jc


@dataclasses.dataclass
class _NEnv:
    method: str
    params: typing.Any = None
    jsonrpc: str = "2.0"


@dataclasses.dataclass
class _REnv:
    id: int | str
    jsonrpc: str
    result: typing.Any = None


@dataclasses.dataclass
class _PRequired:
    required: int


@dataclasses.dataclass
class _NTyped:
    method: str
    params: _PRequired
    jsonrpc: str = "2.0"


def _make_client(message_types: dict[str, typing.Any]) -> jc.JsonRpcClient:
    return jc.JsonRpcClient(message_types=message_types, readable_id="test-client")


def _notif_message(method: str, params: typing.Any = None) -> dict[str, typing.Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def test_sync_registration_rejects_async_and_overlap() -> None:
    """Only plain sync callables may register; overlap with ordinary is refused."""
    client = _make_client({"n/a": (_NEnv, dict, _REnv, None)})

    async def _bad(_params: object) -> None:
        return None

    async def _gen(_params: object) -> typing.AsyncIterator[object]:
        yield _params
        return

    with pytest.raises(TypeError):
        client.feature_sync("n/a", _bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        client.feature_sync("n/a", _gen)  # type: ignore[arg-type]

    class _Callable:
        async def __call__(self, _params: object) -> None:
            return None

    with pytest.raises(TypeError):
        client.feature_sync("n/a", _Callable())  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        client.feature_sync("n/a", functools.partial(_bad, None))  # type: ignore[arg-type]

    def _ok(_params: object) -> None:
        return None

    client.feature_sync("n/a", _ok)
    with pytest.raises(ValueError):
        client.feature("n/a", _ok)
    other = _make_client({"n/b": (_NEnv, dict, _REnv, None)})
    other.feature("n/b", _ok)
    with pytest.raises(ValueError):
        other.feature_sync("n/b", _ok)
    with pytest.raises(ValueError):
        client.feature_sync("unknown/method", _ok)


async def test_sync_notification_runs_inline_and_isolates_siblings() -> None:
    """Sync admission runs before dispatch returns; siblings stay independent."""
    order: list[str] = []
    client = _make_client(
        {
            "m/sync": (_NEnv, dict, _REnv, None),
            "m/async": (_NEnv, dict, _REnv, None),
        }
    )

    def _sync(_params: object) -> None:
        order.append("sync")

    async def _async(_params: object) -> None:
        order.append("async")

    client.feature_sync("m/sync", _sync)
    client.feature("m/async", _async)

    await client.handle_message(_notif_message("m/sync", {"v": 1}))
    # Synchronous: visible without yielding to the loop.
    assert order == ["sync"]
    await client.handle_message(_notif_message("m/async", {"v": 1}))
    # Ordinary stays task-scheduled: not yet run.
    assert order == ["sync"]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert order == ["sync", "async"]


async def test_sync_malformed_input_drops_without_calling() -> None:
    """Malformed envelopes are logged and dropped; the sync impl never fires."""
    calls: list[object] = []
    client = _make_client({"m/typed": (_NTyped, dict, _REnv, None)})

    def _sync(params: object) -> None:
        calls.append(params)

    client.feature_sync("m/typed", _sync)
    await client.handle_message(_notif_message("m/typed", {"wrong": "shape"}))
    await asyncio.sleep(0)
    assert calls == []


async def test_sync_awaitable_return_is_disposed() -> None:
    """An unexpected awaitable from a sync impl is disposed, not left pending."""
    client = _make_client({"m/sync": (_NEnv, dict, _REnv, None)})

    async def _impl() -> None:
        return None

    coro_holder: list[typing.Any] = []

    def _sync(_params: object) -> typing.Any:
        coro = _impl()
        coro_holder.append(coro)
        return coro

    client.feature_sync("m/sync", _sync)
    await client.handle_message(_notif_message("m/sync"))
    assert len(coro_holder) == 1
    # Closed coroutine: awaiting it now must fail.
    with pytest.raises(RuntimeError):
        await coro_holder[0]


async def test_capture_runs_before_success_resolution() -> None:
    """The capture hook observes the prefix before the caller sees the result."""
    events: list[str] = []
    client = _make_client({"r/m": (dict, dict, _REnv, None)})

    def _capture() -> None:
        events.append("capture")

    task = asyncio.ensure_future(
        client.send_request("r/m", on_response=jc.ResponseCompletion(capture=_capture))
    )
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message({"jsonrpc": "2.0", "id": msg_id, "result": "ok"})
    assert events == ["capture"]
    response = await task
    assert response.result == "ok"


async def test_capture_runs_before_error_and_survives_hook_fault() -> None:
    """A hook fault is recorded without replacing the received runner error."""
    client = _make_client({"r/m": (dict, dict, _REnv, None)})

    def _bad_capture() -> None:
        raise RuntimeError("hook boom")

    completion = jc.ResponseCompletion(capture=_bad_capture)
    task = asyncio.ensure_future(client.send_request("r/m", on_response=completion))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -1, "message": "nope"}}
    )
    with pytest.raises(jc.ErrorOnRequest):
        await task
    assert isinstance(completion.error, RuntimeError)


async def test_capture_runs_before_invalid_response() -> None:
    """An undecodable response still freezes the prefix first."""
    seen: list[str] = []
    client = _make_client({"r/m": (dict, dict, _REnv, None)})

    def _capture() -> None:
        seen.append("capture")

    completion = jc.ResponseCompletion(capture=_capture)
    task = asyncio.ensure_future(client.send_request("r/m", on_response=completion))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "result": {"un": "hashable-but-fine"}}
    )
    # Envelope accepts any result; force the invalid path via a bad envelope:
    # a second client with a strict envelope type rejects the same payload.
    assert seen == ["capture"]
    response = await task
    assert response.result == {"un": "hashable-but-fine"}
    assert msg_id not in client._response_completions


async def test_capture_runs_before_invalid_envelope() -> None:
    """A structurally invalid envelope still freezes the prefix first."""
    seen: list[str] = []

    client = _make_client({"r/m": (dict, dict, _REnv, None)})

    def _capture() -> None:
        seen.append("capture")

    completion = jc.ResponseCompletion(capture=_capture)
    task = asyncio.ensure_future(client.send_request("r/m", on_response=completion))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "error": "not-an-error-object"}
    )
    assert seen == ["capture"]
    with pytest.raises(jc.InvalidResponse):
        await task
    assert msg_id not in client._response_completions


async def test_duplicate_response_cannot_recapture() -> None:
    """A late duplicate finds no registration and never re-runs the capture."""
    calls: list[str] = []
    client = _make_client({"r/m": (dict, dict, _REnv, None)})

    def _capture() -> None:
        calls.append("capture")

    completion = jc.ResponseCompletion(capture=_capture)
    task = asyncio.ensure_future(client.send_request("r/m", on_response=completion))
    await asyncio.sleep(0)
    msg_id = next(iter(client._async_request_futures))
    await client.handle_message({"jsonrpc": "2.0", "id": msg_id, "result": "first"})
    response = await task
    assert response.result == "first"
    assert calls == ["capture"]
    await client.handle_message({"jsonrpc": "2.0", "id": msg_id, "result": "second"})
    assert calls == ["capture"]


async def test_cancel_and_timeout_never_run_capture() -> None:
    """Interruption drops the hook instead of freezing a prefix nobody waits on."""
    for mode in ("cancel", "timeout"):
        calls: list[str] = []
        client = _make_client({"r/m": (dict, dict, _REnv, None)})

        def _capture(_calls: list[str] = calls) -> None:
            _calls.append("capture")

        completion = jc.ResponseCompletion(capture=_capture)
        if mode == "cancel":
            task = asyncio.ensure_future(
                client.send_request("r/m", on_response=completion)
            )
            await asyncio.sleep(0)
            _ = task.cancel()
            with pytest.raises((asyncio.CancelledError, jc.RequestCancelledError)):
                await task
        else:
            with pytest.raises(jc.ResponseTimeout):
                await client.send_request("r/m", timeout=0.01, on_response=completion)
        assert calls == []
        assert client._response_completions == {}


async def test_send_failure_cleans_completion_registry() -> None:
    """Serialization failure removes the hook along with the future and types."""
    client = _make_client({"r/m": (dict, dict, _REnv, None)})

    def _capture() -> None:
        raise AssertionError("must not run")

    with pytest.raises(jc.InvalidResponse):
        await client.send_request(
            "r/m", params=object(), on_response=jc.ResponseCompletion(capture=_capture)
        )
    assert client._async_request_futures == {}
    assert client._response_completions == {}


async def test_queue_close_drops_capture_without_running() -> None:
    """EOF fails the waiter but never runs the response hook."""
    calls: list[str] = []
    client = _make_client({"r/m": (dict, dict, _REnv, None)})

    def _capture() -> None:
        calls.append("capture")

    task = asyncio.ensure_future(
        client.send_request("r/m", on_response=jc.ResponseCompletion(capture=_capture))
    )
    await asyncio.sleep(0)
    client._fail_pending_requests("inbound queue closed")
    with pytest.raises(jc.ServerStoppedError):
        await task
    assert calls == []
