"""A peer's ``$/cancelRequest`` ends only the requests that opted into it.

REQUIREMENT: a request method registered with ``on_peer_cancel`` is stopped when
the peer cancels it, and is answered exactly once with the cancelled error code,
whenever the cancel arrives. Every other cancel — for a method with no hook, an
unknown id or a finished request — is ignored and the request keeps being
served. A leaked or unanswered request would leave the peer waiting forever.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import typing

import pytest
from loguru import logger

from finecode_jsonrpc import client as jc
from finecode_jsonrpc import error_codes

_METHOD = "test/method"
_PLAIN_METHOD = "test/plain"


@dataclasses.dataclass
class _Params:
    pass


@dataclasses.dataclass
class _Request:
    id: int | str
    method: str
    jsonrpc: str
    params: _Params


@dataclasses.dataclass
class _Response:
    id: int | str
    jsonrpc: str


@dataclasses.dataclass
class _Result:
    pass


def _make_client(
    impl: typing.Callable[..., typing.Awaitable[typing.Any]],
    on_peer_cancel: typing.Callable[[asyncio.Task[typing.Any]], None] | None,
) -> tuple[jc.JsonRpcClient, list[dict[str, typing.Any]]]:
    client = jc.JsonRpcClient(
        message_types={
            _METHOD: (_Request, _Params, _Response, _Result),
            _PLAIN_METHOD: (_Request, _Params, _Response, _Result),
        },
        readable_id="test-client",
    )
    sent: list[dict[str, typing.Any]] = []
    client._send_data = lambda data: sent.append(json.loads(data))  # type: ignore[method-assign]
    client.feature(_METHOD, impl, on_peer_cancel=on_peer_cancel)
    client.feature(_PLAIN_METHOD, impl)
    return client, sent


async def _request(
    client: jc.JsonRpcClient, request_id: int, method: str = _METHOD
) -> None:
    await client.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}}
    )


async def _cancel(client: jc.JsonRpcClient, request_id: int) -> None:
    await client.handle_message(
        {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": request_id}}
    )


@pytest.fixture
def problems() -> typing.Iterator[list[str]]:
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    yield seen
    logger.remove(handler_id)


async def _blocking_impl(_params: _Params) -> _Result:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def _finish(client: jc.JsonRpcClient) -> None:
    await asyncio.gather(*client._async_tasks, return_exceptions=True)


async def test_hook_runs_before_the_task_is_cancelled(problems: list[str]) -> None:
    events: list[str] = []
    started = asyncio.Event()

    async def impl(_params: _Params) -> _Result:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        raise AssertionError("unreachable")

    client, sent = _make_client(impl, lambda task: events.append("hook"))
    await _request(client, 1)
    await started.wait()

    await _cancel(client, 1)
    await _finish(client)

    assert events == ["hook", "cancelled"]
    assert [m["error"]["code"] for m in sent] == [error_codes.DEFAULT_REQUEST_CANCELLED]
    assert client._inbound_requests == {}
    assert client._async_tasks == []
    assert problems == []


async def test_cancel_before_the_handler_started_answers_once_and_never_runs_it(
    problems: list[str],
) -> None:
    ran = False

    async def impl(_params: _Params) -> _Result:
        nonlocal ran
        ran = True
        return _Result()

    hooked: list[asyncio.Task[typing.Any]] = []
    client, sent = _make_client(impl, hooked.append)
    await _request(client, 1)
    await _cancel(client, 1)
    await _finish(client)

    assert not ran
    assert len(hooked) == 1
    assert [m["id"] for m in sent] == [1]
    assert sent[0]["error"]["code"] == error_codes.DEFAULT_REQUEST_CANCELLED
    assert client._async_tasks == []
    assert client._inbound_requests == {}
    assert problems == []


async def test_cancel_after_the_request_finished_is_ignored(
    problems: list[str],
) -> None:
    async def impl(_params: _Params) -> _Result:
        return _Result()

    hooked: list[asyncio.Task[typing.Any]] = []
    client, sent = _make_client(impl, hooked.append)
    await _request(client, 1)
    await _finish(client)

    await _cancel(client, 1)

    assert hooked == []
    assert len(sent) == 1
    assert "result" in sent[0]
    assert problems == []


async def test_cancel_for_an_unknown_id_is_ignored(problems: list[str]) -> None:
    hooked: list[asyncio.Task[typing.Any]] = []
    client, sent = _make_client(_blocking_impl, hooked.append)

    await _cancel(client, 42)

    assert hooked == []
    assert sent == []
    assert problems == []


async def test_a_method_without_a_hook_keeps_being_served(problems: list[str]) -> None:
    release = asyncio.Event()

    async def impl(_params: _Params) -> _Result:
        await release.wait()
        return _Result()

    client, sent = _make_client(impl, lambda task: None)
    await _request(client, 1, _PLAIN_METHOD)
    await asyncio.sleep(0)

    await _cancel(client, 1)
    release.set()
    await _finish(client)

    assert len(sent) == 1
    assert "result" in sent[0]
    assert problems == []


async def test_a_failing_hook_still_cancels_the_request(problems: list[str]) -> None:
    started = asyncio.Event()

    async def impl(_params: _Params) -> _Result:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def hook(_task: asyncio.Task[typing.Any]) -> None:
        raise RuntimeError("hook failed")

    client, sent = _make_client(impl, hook)
    await _request(client, 1)
    await started.wait()

    await _cancel(client, 1)
    await _finish(client)

    assert [m["error"]["code"] for m in sent] == [error_codes.DEFAULT_REQUEST_CANCELLED]
    assert any("hook failed" in p for p in problems)
    assert not any("ERROR" in p for p in problems)


async def test_a_following_request_is_created_after_the_hook_ran() -> None:
    order: list[str] = []

    async def impl(_params: _Params) -> _Result:
        order.append("started")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client, _sent = _make_client(impl, lambda task: order.append("hook"))
    await _request(client, 1)
    await asyncio.sleep(0)
    await _cancel(client, 1)
    await _request(client, 2)
    await asyncio.sleep(0)

    assert order[:2] == ["started", "hook"]
    assert order[2:] == ["started"]
    client._async_tasks[-1].cancel()
    await _finish(client)


async def test_a_user_registered_cancel_handler_keeps_precedence() -> None:
    received: list[dict[str, typing.Any]] = []
    hooked: list[asyncio.Task[typing.Any]] = []
    started = asyncio.Event()

    async def impl(_params: _Params) -> _Result:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client, sent = _make_client(impl, hooked.append)
    client.message_types["$/cancelRequest"] = (_CancelNotification, None, None, None)

    async def on_cancel(params: dict[str, typing.Any]) -> None:
        received.append(params)

    client.feature("$/cancelRequest", on_cancel)
    await _request(client, 1)
    await started.wait()

    await _cancel(client, 1)
    await asyncio.sleep(0)

    assert hooked == []
    assert sent == []
    assert len(received) == 1
    client._async_tasks[0].cancel()
    await _finish(client)


@dataclasses.dataclass
class _CancelNotification:
    jsonrpc: str
    method: str
    params: dict[str, typing.Any]


def _make_flagged_client(
    impl: typing.Callable[..., typing.Awaitable[typing.Any]],
    on_peer_cancel: typing.Callable[[asyncio.Task[typing.Any]], None] | None,
    *,
    flagged_method: str = _METHOD,
    not_started_data: dict[str, object] | None = None,
) -> tuple[jc.JsonRpcClient, list[dict[str, typing.Any]]]:
    client = jc.JsonRpcClient(
        message_types={
            _METHOD: (_Request, _Params, _Response, _Result),
            _PLAIN_METHOD: (_Request, _Params, _Response, _Result),
        },
        readable_id="test-client",
    )
    sent: list[dict[str, typing.Any]] = []
    client._send_data = lambda data: sent.append(json.loads(data))  # type: ignore[method-assign]
    client.feature(
        _METHOD,
        impl,
        on_peer_cancel=on_peer_cancel,
        not_started_cancel_data=not_started_data,
        cancel_on_connection_loss=(_METHOD == flagged_method),
    )
    client.feature(
        _PLAIN_METHOD,
        impl,
        cancel_on_connection_loss=(_PLAIN_METHOD == flagged_method),
    )
    return client, sent


async def test_not_started_cancel_answers_with_method_data(
    problems: list[str],
) -> None:
    ran = False

    async def impl(_params: _Params) -> _Result:
        nonlocal ran
        ran = True
        return _Result()

    hooked: list[asyncio.Task[typing.Any]] = []
    client, sent = _make_flagged_client(
        impl, hooked.append, not_started_data={"subtreeStopped": True}
    )
    await _request(client, 1)
    await _cancel(client, 1)
    await _finish(client)

    assert not ran
    assert sent[0]["error"]["data"] == {"subtreeStopped": True}
    assert problems == []


async def test_connection_loss_cancels_only_flagged_requests_once(
    problems: list[str],
) -> None:
    started_flagged = asyncio.Event()
    started_plain = asyncio.Event()
    release_plain = asyncio.Event()

    async def flagged_impl(_params: _Params) -> _Result:
        started_flagged.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def plain_impl(_params: _Params) -> _Result:
        started_plain.set()
        await release_plain.wait()
        return _Result()

    hooked: list[asyncio.Task[typing.Any]] = []
    client = jc.JsonRpcClient(
        message_types={
            _METHOD: (_Request, _Params, _Response, _Result),
            _PLAIN_METHOD: (_Request, _Params, _Response, _Result),
        },
        readable_id="test-client",
    )
    sent: list[dict[str, typing.Any]] = []
    client._send_data = lambda data: sent.append(json.loads(data))  # type: ignore[method-assign]
    client.feature(
        _METHOD, flagged_impl, on_peer_cancel=hooked.append,
        cancel_on_connection_loss=True,
    )
    client.feature(_PLAIN_METHOD, plain_impl)
    await _request(client, 1)
    await _request(client, 2, _PLAIN_METHOD)
    await started_flagged.wait()
    await started_plain.wait()

    client._fail_pending_requests("transport gone")
    # A second channel failure must not cancel again.
    client._fail_pending_requests("transport gone")
    release_plain.set()
    await _finish(client)

    assert len(hooked) == 1
    cancelled = [m for m in sent if m.get("id") == 1]
    assert cancelled and cancelled[0]["error"]["code"] == (
        error_codes.DEFAULT_REQUEST_CANCELLED
    )
    served = [m for m in sent if m.get("id") == 2]
    assert served and "result" in served[0]
    assert problems == []


async def test_late_reply_to_cancelled_request_is_debug_only() -> None:
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        client, _sent = _make_client(_blocking_impl, lambda task: None)
        task = asyncio.ensure_future(client.send_request(_METHOD, _Params()))
        await asyncio.sleep(0)
        request_id = next(iter(client._async_request_futures))
        task.cancel()
        with pytest.raises(jc.RequestCancelledError):
            await task
        await client.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "late"},
            }
        )
        assert client._cancelled_request_ids == set()
    finally:
        logger.remove(handler_id)
    assert not any("ERROR" in s for s in seen)
