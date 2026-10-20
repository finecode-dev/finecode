"""Requirement tests: a cancelled request handler must still answer the request.

REQUIREMENT: JSON-RPC 2.0 mandates exactly one response per request. Cancellation
is not an `Exception`, so a handler task that is cancelled — or whose own nested
outbound request is cancelled — would otherwise fall out of `run_feature_impl`
without writing anything to the wire, leaving the peer waiting for a response
that never comes. Peers that send requests without a timeout (the extension
runner does) wait forever.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import typing

import pytest

from finecode_jsonrpc import client as jc
from finecode_jsonrpc import error_codes

_METHOD = "test/method"


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
    impl: typing.Callable, **kwargs: typing.Any
) -> tuple[jc.JsonRpcClient, list[dict[str, typing.Any]]]:
    """A client with *impl* registered for `_METHOD`, and the list of messages
    it writes to the wire.
    """
    client = jc.JsonRpcClient(
        message_types={_METHOD: (_Request, _Params, _Response, _Result)},
        readable_id="test-client",
        **kwargs,
    )
    sent: list[dict[str, typing.Any]] = []
    client._send_data = lambda data: sent.append(json.loads(data))  # type: ignore[method-assign]
    client.feature(_METHOD, impl)
    return client, sent


async def _dispatch(client: jc.JsonRpcClient) -> asyncio.Task[typing.Any]:
    """Feed one request in and return the task handling it."""
    await client.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": _METHOD, "params": {}}
    )
    assert len(client._async_tasks) == 1
    return client._async_tasks[0]


async def test_cancelled_handler_task_still_sends_a_response() -> None:
    """The externally-cancelled case: nothing else will ever answer this
    request, so the cancellation branch is the peer's only way to learn the
    request is over."""
    started = asyncio.Event()

    async def impl(_params: _Params) -> _Result:
        started.set()
        await asyncio.Event().wait()  # never completes
        raise AssertionError("unreachable")

    client, sent = _make_client(impl)
    task = await _dispatch(client)
    await started.wait()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(sent) == 1
    assert sent[0]["id"] == 1
    assert sent[0]["error"]["code"] == error_codes.DEFAULT_REQUEST_CANCELLED


async def test_cancelled_handler_task_stays_cancelled() -> None:
    """Sending the response must not swallow a genuine cancellation: the task
    has to end up cancelled, or loop teardown and done-callbacks are told the
    handler completed normally."""
    started = asyncio.Event()

    async def impl(_params: _Params) -> _Result:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client, _sent = _make_client(impl)
    task = await _dispatch(client)
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_nested_request_cancellation_answers_without_cancelling_the_task() -> (
    None
):
    """`RequestCancelledError` subclasses `CancelledError` by design, so a
    handler whose own outbound request was cancelled lands in the same branch.
    The request must still be answered — but this task was never cancelled, so
    reporting it as cancelled would misattribute the failure.
    """

    async def impl(_params: _Params) -> _Result:
        raise jc.RequestCancelledError(request_id=99)

    client, sent = _make_client(impl)
    task = await _dispatch(client)
    await task

    assert len(sent) == 1
    assert sent[0]["error"]["code"] == error_codes.DEFAULT_REQUEST_CANCELLED
    assert not task.cancelled()


async def test_cancellation_code_is_configurable() -> None:
    """-32800 is LSP's number, not JSON-RPC's; a peer speaking a protocol that
    numbers cancellation differently must be able to say so."""

    async def impl(_params: _Params) -> _Result:
        raise jc.RequestCancelledError(request_id=99)

    client, sent = _make_client(impl, request_cancelled_code=-32099)
    task = await _dispatch(client)
    await task

    assert sent[0]["error"]["code"] == -32099


async def test_successful_handler_is_unaffected() -> None:
    """The cancellation branch must not intercept the ordinary success path."""

    async def impl(_params: _Params) -> _Result:
        return _Result()

    client, sent = _make_client(impl)
    task = await _dispatch(client)
    await task

    assert len(sent) == 1
    assert "error" not in sent[0]
    assert sent[0]["id"] == 1
