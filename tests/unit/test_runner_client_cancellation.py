from __future__ import annotations

import asyncio
import contextlib
import pathlib
import typing
from unittest import mock

import finecode_jsonrpc.client as jsonrpc_client
import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, runner_client

_REQUEST_ID = "req-7"


def _hanging_send_like_real_client() -> typing.Callable[..., typing.Any]:
    """A ``send_request`` that waits forever, and on cancellation raises
    ``RequestCancelledError`` carrying the request id, as ``JsonRpcClient`` does."""

    async def _send(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout, on_response)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            raise jsonrpc_client.RequestCancelledError(
                request_id=_REQUEST_ID
            ) from error
        raise AssertionError("unreachable")

    return _send


def _call(
    method: str, runner: runner_client.ExtensionRunnerInfo, options: dict
) -> typing.Awaitable[typing.Any]:
    if method == "run_action":
        return runner_client.run_action(
            runner=runner, action_name="test_action", params={}, options=options
        )
    return runner_client.run_handlers(
        runner=runner, action_name="test_action", handler_names=["h1"], options=options
    )


@pytest.mark.parametrize("method", ["run_action", "run_handlers"])
@pytest.mark.parametrize("stream", ["none", "partial", "progress"])
async def test_cancelled_request_sends_cancel_to_er(
    tmp_path: pathlib.Path, method: str, stream: str
) -> None:
    """Cancelling the WM task awaiting an ER request tells the ER, also when the
    request carries stream subscriptions (issue #998)."""
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    options: dict[str, typing.Any] = {}
    with contextlib.ExitStack() as stack:
        if stream == "partial":
            options["partialResultToken"] = "tok"
            stack.enter_context(runner.stream_registry.subscribe_partial("tok"))
        elif stream == "progress":
            options["progressToken"] = "tok"
            stack.enter_context(runner.stream_registry.subscribe_progress("tok"))
        with mock.patch.object(client, "send_request", _hanging_send_like_real_client()):
            task = asyncio.ensure_future(_call(method, runner, options))
            await asyncio.sleep(0.01)
            _ = task.cancel()
            with pytest.raises(jsonrpc_client.RequestCancelledError) as exc_info:
                await task

    assert exc_info.value.request_id == _REQUEST_ID
    assert (
        _internal_client_types.CANCEL_REQUEST,
        _internal_client_types.CancelParams(id=_REQUEST_ID),
    ) in client.sent_requests
    assert runner.active_requests == 0


async def test_run_action_raises_action_run_cancelled_on_er_cancellation(
    tmp_path: pathlib.Path,
) -> None:
    """When the ER signals a genuine JSON-RPC cancellation (code ==
    REQUEST_CANCELLED) for ``actions/run``, ``runner_client.run_action`` must
    raise ``ActionRunCancelled`` — a distinct, benign outcome from an ordinary
    ``ActionRunFailed`` — so callers can propagate cancellation instead of
    logging it as a failure.
    """
    client = wm_testing.FakeErClient()
    client.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(runner_client.ActionRunCancelled) as exc_info:
        await runner_client.run_action(
            runner=runner, action_name="test_action", params={}
        )

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_run_action_unrelated_error_code_propagates_unwrapped(
    tmp_path: pathlib.Path,
) -> None:
    """An ``actions/run`` error with an unrelated JSON-RPC code must not be
    mistranslated into a cancellation — the original transport exception
    propagates unchanged.
    """
    client = wm_testing.FakeErClient()
    original = wm_testing.make_error_on_request(-32603, "boom")
    client.configure_error(original)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(type(original)) as exc_info:
        await runner_client.run_action(
            runner=runner, action_name="test_action", params={}
        )

    assert exc_info.value is original


async def test_run_handlers_raises_action_run_cancelled_on_er_cancellation(
    tmp_path: pathlib.Path,
) -> None:
    """Same cancellation propagation contract as ``run_action``, but for the
    ``actions/runHandlers`` multi-env segment call.
    """
    client = wm_testing.FakeErClient()
    client.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(runner_client.ActionRunCancelled) as exc_info:
        await runner_client.run_handlers(
            runner=runner,
            action_name="test_action",
            handler_names=["h1"],
        )

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_run_handlers_unrelated_error_code_propagates_unwrapped(
    tmp_path: pathlib.Path,
) -> None:
    """Regression guard mirroring ``run_action``'s: an unrelated error code on
    ``actions/runHandlers`` must not be reclassified as a cancellation.
    """
    client = wm_testing.FakeErClient()
    original = wm_testing.make_error_on_request(-32603, "boom")
    client.configure_error(original)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(type(original)) as exc_info:
        await runner_client.run_handlers(
            runner=runner,
            action_name="test_action",
            handler_names=["h1"],
        )

    assert exc_info.value is original
