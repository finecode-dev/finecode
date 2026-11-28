"""Runner-owned stream admission and invocation boundaries.

Observable contract: the received notification prefix of each covered runner
invocation completes through listeners and local handoff before its received
success/error response is exposed. Unsubscribed callers keep the unchanged
request path, without completion bookkeeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import typing

import pytest

import finecode_jsonrpc as jsonrpc_client
from finecode.wm_server import domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import (
    _internal_client_types,
    runner_client,
    runner_manager,
)
from finecode.wm_server.runner import (
    stream_completion as runner_streams,
)
from finecode.wm_server.utils import stream_completion as streams


def _progress_params(token: int | str, value: object) -> typing.Any:
    return _internal_client_types.ProgressParams(token=token, value=json.dumps(value))


def _admit(
    runner: runner_client.ExtensionRunnerInfo, token: int | str, value: object
) -> None:
    runner_manager.admit_runner_progress(runner, _progress_params(token, value))


async def _consume_prefix(
    subscription: runner_streams.StreamSubscription[typing.Any],
    out: list[typing.Any],
) -> None:
    async for value in subscription.stream:
        out.append(value)


def _start_consumer(
    subscription: runner_streams.StreamSubscription[typing.Any],
    out: list[typing.Any],
) -> asyncio.Task[None]:
    return asyncio.ensure_future(_consume_prefix(subscription, out))


async def _cancel(task: asyncio.Task[typing.Any]) -> None:
    _ = task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


class _GatedErClient(wm_testing.FakeErClient):
    """Fake whose response waits for an explicit release, recording hooks."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.seen_on_response: list[typing.Any] = []

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (timeout, await_reply_after_cancel)
        assert self._configured, "configure the fake before exercising send_request"
        self.sent_requests.append((method, params))
        self.seen_on_response.append(on_response)
        await self.release.wait()
        if on_response is not None:
            try:
                on_response.capture()
            except Exception as error:  # noqa: BLE001
                on_response.error = error
        if self._exception is not None:
            raise self._exception
        return self._response


class _BlockingErClient(wm_testing.FakeErClient):
    """Fake whose send never resolves, mimicking a live transport wait."""

    def __init__(self) -> None:
        super().__init__()
        self._configured = True
        self.seen_on_response: list[typing.Any] = []

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (timeout, await_reply_after_cancel)
        self.sent_requests.append((method, params))
        self.seen_on_response.append(on_response)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError as exc:
            raise jsonrpc_client.RequestCancelledError(request_id="test") from exc
        raise AssertionError("unreachable")


async def test_registry_subscribe_select_and_release() -> None:
    """Subscriptions are selected by token and removed on context exit."""
    registry = runner_streams.RunnerStreamRegistry()
    assert registry.begin_invocation("p", "g").subscriptions == (), (
        "no subscriptions selects the unchanged path"
    )
    with (
        registry.subscribe_partial("p1") as partial,
        registry.subscribe_progress("g1") as progress,
    ):
        boundary = registry.begin_invocation("p1", "g1")
        assert tuple(boundary.subscriptions) == (partial, progress)
        # Unrelated tokens and crossed channels select nothing.
        assert registry.begin_invocation("other", None).subscriptions == ()
        assert registry.begin_invocation(None, None).subscriptions == ()
        cross = registry.begin_invocation("g1", "p1")
        assert cross.subscriptions == ()
    assert registry.begin_invocation("p1", "g1").subscriptions == ()


async def test_duplicate_same_token_subscriptions_each_own_a_prefix() -> None:
    """Duplicate same-env handlers create one owned subscription each."""
    registry = runner_streams.RunnerStreamRegistry()
    with (
        registry.subscribe_partial("p") as first,
        registry.subscribe_partial("p") as second,
    ):
        boundary = registry.begin_invocation("p", None)
        assert tuple(boundary.subscriptions) == (first, second)
        first.admit({"n": 1})
        assert first.admitted == 1
        assert second.admitted == 0


async def test_admission_routes_progress_and_partial(
    tmp_path: typing.Any,
) -> None:
    """Begin/report/end route to progress; anything else routes to partial."""
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    with (
        runner.stream_registry.subscribe_partial("p") as partial,
        runner.stream_registry.subscribe_progress("g") as progress,
        runner.partial_results.iterator() as legacy_partial,
        runner.progress_notifications.iterator() as legacy_progress,
    ):
        _admit(runner, "p", {"n": 1})
        _admit(runner, "g", {"type": "report", "p": 1})
        assert legacy_partial.values == [
            domain.PartialResult(token="p", value={"n": 1})
        ]
        assert legacy_progress.values == [
            domain.ProgressNotification(token="g", value={"type": "report", "p": 1})
        ]
        assert partial.admitted == 1
        assert progress.admitted == 1
        assert partial.stream.queue.qsize() == 1
        assert progress.stream.queue.qsize() == 1


async def test_admission_runs_synchronously_without_scheduling(
    tmp_path: typing.Any,
) -> None:
    """Admission publishes before returning; nothing is deferred to a task."""
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    with runner.partial_results.iterator() as legacy:
        _admit(runner, "p", {"n": 1})
        # Visible without yielding to the loop: no task was scheduled.
        assert legacy.values == [domain.PartialResult(token="p", value={"n": 1})]


async def test_admission_malformed_json_logged_drop(
    tmp_path: typing.Any,
) -> None:
    """Undecodable envelopes are dropped; no subscription or legacy sees them."""
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    with (
        runner.stream_registry.subscribe_partial("p") as partial,
        runner.partial_results.iterator() as legacy,
    ):
        runner_manager.admit_runner_progress(
            runner, _internal_client_types.ProgressParams(token="p", value="{bad")
        )
        assert legacy.values == []
        assert partial.admitted == 0
        assert partial.stream.queue.qsize() == 0


async def test_admission_error_latched_per_subscription(
    tmp_path: typing.Any,
) -> None:
    """One failing subscriber is retained and signaled; the rest continue."""
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    with (
        runner.stream_registry.subscribe_partial("p") as first,
        runner.stream_registry.subscribe_partial("p") as second,
    ):
        failure = RuntimeError("downstream down")

        def _boom(value: typing.Any) -> None:
            _ = value
            raise failure

        first.stream.append = _boom
        _admit(runner, "p", {"n": 1})
        assert first.admission_errors == [failure]
        assert first.failure.done() and first.failure.result() is failure
        assert second.admitted == 1
        assert second.admission_errors == []
        assert not second.failure.done()


async def test_boundary_freezes_prefix_suffix_cannot_extend(
    tmp_path: typing.Any,
) -> None:
    """Capture freezes counts; later items cannot extend the frozen prefix."""
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    with runner.stream_registry.subscribe_partial("p") as subscription:
        _admit(runner, "p", {"n": 1})
        _admit(runner, "p", {"n": 2})
        boundary = runner.stream_registry.begin_invocation("p", None)
        assert not boundary.received
        boundary.completion.capture()
        assert boundary.received
        assert [prefix.end_position for prefix in boundary.prefixes] == [2]
        _admit(runner, "p", {"n": 3})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            assert await asyncio.wait_for(boundary.wait_prefixes(), timeout=5) == ()
            for _ in range(100):
                if len(out) >= 2 and len(subscription.stream.fences) == 1:
                    fence = subscription.stream.fences[0]
                    if fence.completion.done():
                        break
                await asyncio.sleep(0)
            assert out[:2] == [{"n": 1}, {"n": 2}]
            assert subscription.stream.fences[0].completion.result() is None
        finally:
            await _cancel(consumer)


async def test_boundary_hook_failure_aborts_without_wait(
    tmp_path: typing.Any,
) -> None:
    """A hook fault surfaces instead of waiting for a fence never inserted."""
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    with runner.stream_registry.subscribe_partial("p") as subscription:
        _admit(runner, "p", {"n": 1})
        boundary = runner.stream_registry.begin_invocation("p", None)
        hook_failure = RuntimeError("capture down")

        def _boom(fence: streams.PrefixFence) -> None:
            _ = fence
            raise hook_failure

        subscription.stream.forward = _boom
        # The transport records capture faults on the completion; emulate it.
        try:
            boundary.completion.capture()
        except Exception as error:  # noqa: BLE001
            boundary.completion.error = error
        assert isinstance(boundary.completion.error, RuntimeError)
        errors = await asyncio.wait_for(boundary.wait_prefixes(), timeout=5)
        assert errors == (boundary.completion.error,)


async def test_run_action_subscribed_success_drains_before_response(
    tmp_path: typing.Any,
) -> None:
    """The prefix completes through the listener before the response maps."""
    client = _GatedErClient()
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {"ok": True}}
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        _admit(runner, "p1", {"n": 2})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            task = asyncio.ensure_future(
                runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert runner.active_requests == 1
            client.release.set()
            response = await asyncio.wait_for(task, timeout=5)
            assert response.return_code == 0
            assert response.result_by_format == {"json": {"ok": True}}
            assert out == [{"n": 1}, {"n": 2}]
            assert subscription.stream.fences[0].completion.result() is None
            assert client.seen_on_response != [None]
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_action_subscribed_typed_error_drains_first(
    tmp_path: typing.Any,
) -> None:
    """Error-prefix items arrive before the mapped runner error."""
    client = wm_testing.FakeErClient()
    client.configure_response(wm_testing.make_run_action_response(error="runner boom"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunFailed, match="runner boom"):
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)


async def test_run_action_subscribed_stopped_status(tmp_path: typing.Any) -> None:
    """A received stopped status still maps after its prefix drains."""
    client = wm_testing.FakeErClient()
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {}}, status="stopped"
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunStopped):
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)


async def test_run_action_raw_rpc_error_drains_prefix_first(
    tmp_path: typing.Any,
) -> None:
    """A received transport error is exposed only after its prefix drains."""
    client = wm_testing.FakeErClient()
    original = wm_testing.make_error_on_request(-32603, "transport boom")
    client.configure_error(original)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(jsonrpc_client.ErrorOnRequest) as exc_info:
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert exc_info.value is original
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)


async def test_run_action_unsubscribed_uses_unchanged_path(
    tmp_path: typing.Any,
) -> None:
    """Without subscriptions there is no completion bookkeeping on the send."""
    client = _GatedErClient()
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {"ok": True}}
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    task = asyncio.ensure_future(
        runner_client.run_action(
            runner,
            action_name="a",
            params={},
            options={"partialResultToken": "p1"},
            child_stops=None,
        )
    )
    await asyncio.sleep(0)
    client.release.set()
    response = await asyncio.wait_for(task, timeout=5)
    assert response.return_code == 0
    assert client.seen_on_response == [None]


async def test_run_action_pre_response_fault_aborts_request(
    tmp_path: typing.Any,
) -> None:
    """A subscription fault while pending aborts the wait with cancel signal."""
    client = _BlockingErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        failure = RuntimeError("publication down")

        def _boom(value: typing.Any) -> None:
            _ = value
            raise failure

        subscription.stream.append = _boom
        task = asyncio.ensure_future(
            runner_client.run_action(
                runner,
                action_name="a",
                params={},
                options={"partialResultToken": "p1"},
                child_stops=None,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        _admit(runner, "p1", {"n": 1})
        with pytest.raises(jsonrpc_client.RequestCancelledError):
            await asyncio.wait_for(task, timeout=5)
        cancel_methods = [method for method, _ in client.sent_requests]
        assert _internal_client_types.CANCEL_REQUEST in cancel_methods
        assert subscription.admission_errors == [failure]
    assert runner.active_requests == 0


async def test_run_action_post_response_fault_surfaces_as_run_failed(
    tmp_path: typing.Any,
) -> None:
    """A fault witnessed after receipt fails the run instead of hiding it."""

    class _FaultAfterCaptureClient(wm_testing.FakeErClient):
        async def send_request(
            self,
            method: str,
            params: typing.Any = None,
            timeout: float | None = None,
            on_response: typing.Any | None = None,
            await_reply_after_cancel: typing.Any | None = None,
        ) -> typing.Any:
            _ = (timeout, await_reply_after_cancel)
            self.sent_requests.append((method, params))
            assert on_response is not None
            on_response.capture()
            late = RuntimeError("late publication down")
            subscription.admission_errors.append(late)
            subscription.failure.set_result(late)
            return self._response

    client = _FaultAfterCaptureClient()
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {"ok": True}}
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunFailed, match="Stream prefix"):
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)


async def test_run_action_hook_fault_cannot_strand_wait(
    tmp_path: typing.Any,
) -> None:
    """A capture fault surfaces through the drain instead of hanging it."""
    client = wm_testing.FakeErClient()
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {"ok": True}}
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        hook_failure = RuntimeError("no fence")

        def _boom(fence: streams.PrefixFence) -> None:
            _ = fence
            raise hook_failure

        subscription.stream.forward = _boom
        with pytest.raises(runner_client.ActionRunFailed, match="Stream prefix"):
            await asyncio.wait_for(
                runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                ),
                timeout=5,
            )


async def test_run_handlers_subscribed_success(tmp_path: typing.Any) -> None:
    """runHandlers drains its prefix before mapping the segment response."""
    client = wm_testing.FakeErClient()
    client.configure_response(
        _internal_client_types.ErRunHandlersResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErRunHandlersResult(
                return_code=0,
                result_by_format={"json": {}},
                status="success",
                result={},
                context=None,
                error=None,
            ),
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            response = await runner_client.run_handlers(
                runner,
                action_name="a",
                handler_names=["h1"],
                options={"partialResultToken": "p1"},
                child_stops=None,
            )
            assert response.return_code == 0
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


class _AbortAfterCaptureClient(wm_testing.FakeErClient):
    """Captures the response, then fails the subscribed stream before replying."""

    def __init__(self, abort: typing.Callable[[], None]) -> None:
        super().__init__()
        self._abort = abort

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
        await_reply_after_cancel: typing.Any | None = None,
    ) -> typing.Any:
        _ = (timeout, await_reply_after_cancel)
        assert self._configured
        self.sent_requests.append((method, params))
        assert on_response is not None
        on_response.capture()
        self._abort()
        if self._exception is not None:
            raise self._exception
        return self._response


def _handlers_response(
    *,
    error: str | None = None,
    status: str | None = "success",
    return_code: int = 0,
) -> _internal_client_types.ErRunHandlersResponse:
    return _internal_client_types.ErRunHandlersResponse(
        id=1,
        jsonrpc="2.0",
        result=_internal_client_types.ErRunHandlersResult(
            return_code=return_code,
            result_by_format={"json": {}},
            status=status,
            result={},
            context=None,
            error=error,
        ),
    )


async def _run_handlers_call(
    runner: runner_client.ExtensionRunnerInfo,
) -> typing.Any:
    return await runner_client.run_handlers(
        runner,
        action_name="a",
        handler_names=["h1"],
        options={"partialResultToken": "p1"},
        child_stops=None,
    )


async def test_run_action_typed_error_with_failed_fence_keeps_runner_error(
    tmp_path: typing.Any,
) -> None:
    """A drain fault cannot mask a received typed runner failure."""
    fault = RuntimeError("writer down")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_response(wm_testing.make_run_action_response(error="runner boom"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunFailed, match="runner boom"):
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert out == [{"n": 1}]
            assert subscription.stream.fences[0].completion.result() is fault
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_action_stopped_with_failed_fence_keeps_stopped_mapping(
    tmp_path: typing.Any,
) -> None:
    """A drain fault cannot mask a received stopped status."""
    fault = RuntimeError("writer down")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {}}, status="stopped"
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunStopped):
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert out == [{"n": 1}]
            assert subscription.stream.fences[0].completion.result() is fault
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_action_raw_rpc_error_with_failed_fence_keeps_identity(
    tmp_path: typing.Any,
) -> None:
    """A drain fault cannot replace the received transport error."""
    fault = RuntimeError("writer down")
    original = wm_testing.make_error_on_request(-32603, "transport boom")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_error(original)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(jsonrpc_client.ErrorOnRequest) as exc_info:
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert exc_info.value is original
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_action_success_with_failed_fence_propagates_drain(
    tmp_path: typing.Any,
) -> None:
    """With runner success, a drain fault fails the covered run."""
    fault = RuntimeError("writer down")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {"ok": True}}
        )
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunFailed, match="Stream prefix"):
                await runner_client.run_action(
                    runner,
                    action_name="a",
                    params={},
                    options={"partialResultToken": "p1"},
                    child_stops=None,
                )
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_handlers_typed_error_with_failed_fence_keeps_runner_error(
    tmp_path: typing.Any,
) -> None:
    """A drain fault cannot mask a received typed handler failure."""
    fault = RuntimeError("writer down")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_response(_handlers_response(error="handler boom"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunFailed, match="handler boom"):
                await _run_handlers_call(runner)
            assert out == [{"n": 1}]
            assert subscription.stream.fences[0].completion.result() is fault
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_handlers_stopped_with_failed_fence_keeps_stopped_mapping(
    tmp_path: typing.Any,
) -> None:
    """A drain fault cannot mask a received stopped handler status."""
    fault = RuntimeError("writer down")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_response(_handlers_response(status="stopped"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunStopped):
                await _run_handlers_call(runner)
            assert out == [{"n": 1}]
            assert subscription.stream.fences[0].completion.result() is fault
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_handlers_raw_rpc_error_with_failed_fence_keeps_identity(
    tmp_path: typing.Any,
) -> None:
    """A drain fault cannot replace the received handler transport error."""
    fault = RuntimeError("writer down")
    original = wm_testing.make_error_on_request(-32603, "transport boom")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_error(original)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(jsonrpc_client.ErrorOnRequest) as exc_info:
                await _run_handlers_call(runner)
            assert exc_info.value is original
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0


async def test_run_handlers_success_with_failed_fence_propagates_drain(
    tmp_path: typing.Any,
) -> None:
    """With handler success, a drain fault fails the covered segment."""
    fault = RuntimeError("writer down")
    client = _AbortAfterCaptureClient(
        lambda: runner.stream_registry.matching(
            runner_streams.StreamChannel.PARTIAL, "p1"
        )[0].stream.abort(fault)
    )
    client.configure_response(_handlers_response())
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    with runner.stream_registry.subscribe_partial("p1") as subscription:
        _admit(runner, "p1", {"n": 1})
        out: list[typing.Any] = []
        consumer = _start_consumer(subscription, out)
        try:
            with pytest.raises(runner_client.ActionRunFailed, match="Stream prefix"):
                await _run_handlers_call(runner)
            assert out == [{"n": 1}]
        finally:
            await _cancel(consumer)
    assert runner.active_requests == 0
