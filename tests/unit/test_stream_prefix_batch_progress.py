"""Batch/ progress-only prefix completion (issue #916, task 5).

Every requested progress prefix completes through subscription, slot
aggregation and the writer handoff before its terminal response is
exposed. A suppressed aggregator end still forwards its fence; duplicate
same-env handlers share one subscription; cancellation retires owned
hooks without stranding requests.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing
from unittest import mock

from finecode.wm_server import testing as wm_testing
from finecode.wm_server._api_handlers import _streaming as streaming
from finecode.wm_server.runner import _internal_client_types
from finecode.wm_server.runner.runner_manager import admit_runner_progress
from finecode.wm_server.services import partial_results_service
from finecode.wm_server.utils import stream_completion


async def test_distinct_runners_deduplicate_same_env() -> None:
    """Two handlers in one env share one runner subscription."""
    runner = object()
    runners = {"env_a": runner}
    handlers = [
        typing.cast(typing.Any, type("H", (), {"env": "env_a"})()),
        typing.cast(typing.Any, type("H", (), {"env": "env_a"})()),
    ]
    assert streaming._distinct_progress_runners(runners, handlers) == [runner]


async def test_subscription_pump_forwards_fence() -> None:
    """An intermediate pump forwards values and fences without acknowledging."""
    from finecode.wm_server.runner.stream_completion import RunnerStreamRegistry

    registry = RunnerStreamRegistry()
    with registry.subscribe_progress("tok-pump") as subscription:
        subscription.admit({"type": "report", "percentage": 50})
        fence = stream_completion.PrefixFence(
            completion=asyncio.get_running_loop().create_future()
        )
        subscription.stream.forward(fence)
        subscription.stream.end()
        output: stream_completion.CompletionStream[typing.Any] = (
            stream_completion.CompletionStream()
        )
        await streaming._pump_subscription_to_output(subscription, output)
        output.end()
        seen = [item async for item in output]
        assert seen == [{"type": "report", "percentage": 50}]
        assert fence.completion.done()
        assert fence.completion.result() is None


async def test_slot_pump_forwards_fence_without_aggregator_output() -> None:
    """A suppressed aggregator end still forwards its fence to the writer."""
    output: stream_completion.CompletionStream[typing.Any] = (
        stream_completion.CompletionStream()
    )
    aggregator = partial_results_service.ProgressAggregator(2, output)
    slot: stream_completion.CompletionStream[typing.Any] = (
        stream_completion.CompletionStream()
    )
    # A lone end for one of two slots emits no aggregated notification.
    aggregator.on_progress("proj/a", {"type": "end"})
    fence = stream_completion.PrefixFence(
        completion=asyncio.get_running_loop().create_future()
    )
    slot.forward(fence)
    slot.end()
    await streaming._pump_slot_to_combined(slot, "proj/a", aggregator, output)
    output.end()
    seen = [item async for item in output]
    assert seen == []
    assert fence.completion.done()
    assert fence.completion.result() is None


async def test_direct_progress_completes_prefix_before_response(
    tmp_path: pathlib.Path,
) -> None:
    """A direct progress run forwards its prefix before exposing the response."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source="test.actions.TestAction",
    )
    project.actions[0].canonical_source = "test.actions.TestAction"
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    async def _scripted_send(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout)
        assert on_response is not None
        admit_runner_progress(
            runner,
            _internal_client_types.ProgressParams(
                token="prog-1", value='{"type": "report", "percentage": 10}'
            ),
        )
        on_response.capture()
        await asyncio.sleep(0)
        return wm_testing.make_run_action_response(result_by_format={"ok": True})

    client.send_request = _scripted_send
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    writes: list[tuple[str, dict[str, typing.Any]]] = []

    class _Writer:
        async def drain(self) -> None:
            return None

    writer = typing.cast(asyncio.StreamWriter, _Writer())

    def _record(_w: typing.Any, method: str, payload: dict[str, typing.Any]) -> None:
        writes.append((method, payload))

    with mock.patch.object(
        streaming,
        "_notify_client",
        side_effect=_record,
    ):
        result = await streaming._handle_run_action_with_progress(
            {
                "actionSource": "test.actions.TestAction",
                "project": str(tmp_path),
                "progressToken": "prog-1",
                "options": {"trigger": "user", "devEnv": "cli"},
            },
            ws_context,
            writer,
        )
    assert result == {"resultByFormat": {"ok": True}, "returnCode": 0}
    assert [method for method, _ in writes] == ["actions/progress"]
    assert writes[0][1]["token"] == "prog-1"
    from finecode.wm_server.runner.stream_completion import StreamChannel

    assert runner.stream_registry.matching(StreamChannel.PROGRESS, "prog-1") == []
    assert runner.active_requests == 0
