"""A delegated child's reply that beats the cancel still yields its verdict.

When the reply has already completed the send by the time the caller's cancel
lands, the caller must report it as a reply after cancel, so the delegated
call's ``subtreeStopped`` verdict counts the child instead of leaving it
pending (which reads unconfirmed).
"""

from __future__ import annotations

import asyncio
import pathlib
import typing
from unittest import mock

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, runner_client
from finecode.wm_server.runner.delegated_child_stops import DelegatedChildStops
from finecode_jsonrpc import client as jsonrpc_client


def _reply(*, subtree_stopped: bool) -> _internal_client_types.ErRunActionResponse:
    return _internal_client_types.ErRunActionResponse(
        id="req-1",
        jsonrpc="2.0",
        result=_internal_client_types.ErRunActionResult(
            return_code=0,
            result_by_format={},
            status="streamed",
            subtree_stopped=subtree_stopped,
        ),
    )


async def test_reply_before_cancel_still_records_child_verdict(
    tmp_path: pathlib.Path,
) -> None:
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    stops = DelegatedChildStops()
    loop = asyncio.get_running_loop()
    caller: asyncio.Task[typing.Any] | None = None

    async def _reply_then_cancel_caller(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: jsonrpc_client.ResponseCompletion | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout, await_reply_after_cancel)
        assert on_response is not None
        assert caller is not None
        on_response.capture()
        # Queued ahead of the caller's wake-up, so the cancel reaches the
        # caller only after this send has already returned the reply.
        _ = loop.call_soon(caller.cancel)
        return _reply(subtree_stopped=True)

    with (
        runner.stream_registry.subscribe_partial("tok") as subscription,
        mock.patch.object(client, "send_request", _reply_then_cancel_caller),
    ):

        async def _sink() -> None:
            async for _ in subscription.stream:
                pass

        sink = asyncio.ensure_future(_sink())
        caller = asyncio.ensure_future(
            runner_client.run_action(
                runner,
                "test_action",
                {},
                {"partialResultToken": "tok"},
                child_stops=stops,
            )
        )
        # Awaited directly: a task ending in a CancelledError subclass reads
        # as cancelled, and gather() would replace the subclass with a bare
        # CancelledError.
        outcome: BaseException | None = None
        try:
            await caller
        except asyncio.CancelledError as exc:
            outcome = exc
        subscription.stream.end()
        await asyncio.wait_for(sink, timeout=2.0)

    assert isinstance(outcome, jsonrpc_client.RequestCancelledError), repr(outcome)
    assert outcome.reply is not None
    assert outcome.reply.kind is jsonrpc_client.ReplyAfterCancelKind.RESULT
    assert stops.subtree_stopped()
    assert runner.active_requests == 0
