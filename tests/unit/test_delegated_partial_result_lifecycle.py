"""Delegated partial-result lifecycle over the runner stream registry."""

from __future__ import annotations

import asyncio
import json
import pathlib
import typing
import uuid
from unittest import mock

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import (
    _internal_client_types,
    runner_client,
    runner_manager,
)
from finecode.wm_server.runner.stream_completion import StreamChannel
from finecode.wm_server.services.run_service import proxy_utils
from finecode.wm_server.services.run_service.exceptions import (
    ActionCancelledError,
    ActionRunFailed,
)

_CANONICAL_SOURCE = "test.actions.TestAction"


def _partial_params(
    token: int | str, value: dict[str, typing.Any]
) -> _internal_client_types.ProgressParams:
    return _internal_client_types.ProgressParams(token=token, value=json.dumps(value))


class _ScriptedClient(wm_testing.FakeErClient):
    """Admits planned notifications, then captures and returns the response."""

    def __init__(self, admissions: list[_internal_client_types.ProgressParams]) -> None:
        super().__init__()
        self.runner: typing.Any = None
        self.admissions = admissions

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
    ) -> typing.Any:
        assert self.runner is not None
        for admission in self.admissions:
            runner_manager.admit_runner_progress(self.runner, admission)
        return await super().send_request(
            method, params, timeout=timeout, on_response=on_response
        )


def _make_project(
    tmp_path: pathlib.Path, client: wm_testing.FakeErClient
) -> tuple[typing.Any, typing.Any, typing.Any]:
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    if isinstance(client, _ScriptedClient):
        client.runner = runner
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return project, runner, ws_context


async def _run_and_collect(
    ws_context: typing.Any,
    tmp_path: pathlib.Path,
    token: int | str,
    collected: list[typing.Any],
) -> None:
    async with proxy_utils.run_with_partial_results(
        action_name="test_action",
        params={},
        partial_result_token=token,
        project_dir_path=tmp_path,
        run_trigger=runner_client.RunActionTrigger.USER,
        dev_env=runner_client.DevEnv.CI,
        ws_context=ws_context,
        origin=None,
    ) as ctx:
        async for item in ctx:
            collected.append(item)


async def test_subscription_exists_before_dispatch(tmp_path: pathlib.Path) -> None:
    """A partial admitted on entry is observed exactly once."""
    client = _ScriptedClient([_partial_params("tok-A2", {"marker": "first"})])
    client.configure_response(
        wm_testing.make_run_action_response(result_by_format={}, status="streamed")
    )
    _project, runner, ws_context = _make_project(tmp_path, client)
    matched: list[int] = []
    real_admit = runner_manager.admit_runner_progress

    def _recording_admit(
        target: typing.Any, params: _internal_client_types.ProgressParams
    ) -> None:
        matched.append(
            len(target.stream_registry.matching(StreamChannel.PARTIAL, params.token))
        )
        real_admit(target, params)

    collected: list[typing.Any] = []
    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            return_value=runner,
        ),
        mock.patch.object(runner_manager, "admit_runner_progress", _recording_admit),
    ):
        await _run_and_collect(ws_context, tmp_path, "tok-A2", collected)

    assert matched == [1]
    assert collected == [{"marker": "first"}]
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-A2") == []


async def test_delayed_relay_still_captures_first_partial(
    tmp_path: pathlib.Path,
) -> None:
    """A slow relay still forwards the first admitted send."""
    client = _ScriptedClient([_partial_params("tok-delayed", {"marker": "first"})])
    client.configure_response(
        wm_testing.make_run_action_response(result_by_format={}, status="streamed")
    )
    _project, runner, ws_context = _make_project(tmp_path, client)
    real_relay = proxy_utils._relay_subscription

    async def _delayed_relay(subscription: typing.Any, output: typing.Any) -> None:
        await asyncio.sleep(0.05)
        await real_relay(subscription, output)

    collected: list[typing.Any] = []
    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            return_value=runner,
        ),
        mock.patch.object(proxy_utils, "_relay_subscription", _delayed_relay),
    ):
        await _run_and_collect(ws_context, tmp_path, "tok-delayed", collected)

    assert collected == [{"marker": "first"}]
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-delayed") == []


async def test_success_restores_registry_counts(tmp_path: pathlib.Path) -> None:
    """A successful child run releases its registry subscription."""
    client = _ScriptedClient([_partial_params("tok-ok", {"ok": True})])
    client.configure_response(
        wm_testing.make_run_action_response(result_by_format={}, status="streamed")
    )
    _project, runner, ws_context = _make_project(tmp_path, client)
    baseline = len(runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-ok"))

    collected: list[typing.Any] = []
    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        return_value=runner,
    ):
        await _run_and_collect(ws_context, tmp_path, "tok-ok", collected)

    assert collected == [{"ok": True}]
    assert (
        len(runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-ok"))
        == baseline
    )


async def test_child_error_restores_registry_counts(tmp_path: pathlib.Path) -> None:
    """A failing child run still releases its registry subscription."""
    client = _ScriptedClient([_partial_params("tok-err", {"marker": "late"})])
    client.configure_response(wm_testing.make_run_action_response(error="child failed"))
    _project, runner, ws_context = _make_project(tmp_path, client)

    collected: list[typing.Any] = []
    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        return_value=runner,
    ):
        try:
            await _run_and_collect(ws_context, tmp_path, "tok-err", collected)
        except ActionRunFailed as exc:
            assert "child failed" in exc.message
        else:
            raise AssertionError("expected ActionRunFailed")

    assert collected == [{"marker": "late"}]
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-err") == []


async def test_child_cancellation_restores_registry_counts(
    tmp_path: pathlib.Path,
) -> None:
    """A cancelled child run still releases its registry subscription."""
    client = wm_testing.FakeErClient()
    _project, runner, ws_context = _make_project(tmp_path, client)

    async def _hanging_send(
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
    ) -> typing.Any:
        _ = (method, params, timeout, on_response)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _run() -> None:
        collected: list[typing.Any] = []
        await _run_and_collect(ws_context, tmp_path, "tok-cancel", collected)

    with (
        mock.patch.object(
            proxy_utils.runner_start_service,
            "get_or_start_runner_with_auto_prepare",
            return_value=runner,
        ),
        mock.patch.object(client, "send_request", _hanging_send),
    ):
        task = asyncio.ensure_future(_run())
        await asyncio.sleep(0.05)
        _ = task.cancel()
        try:
            await task
        except (asyncio.CancelledError, ActionRunFailed, ActionCancelledError):
            pass

    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-cancel") == []


async def test_cancellation_during_discovery_aborts_dispatch(
    tmp_path: pathlib.Path,
) -> None:
    """Cancelling while the runner starts never dispatches the action."""
    client = wm_testing.FakeErClient()
    client.configure_response(
        wm_testing.make_run_action_response(result_by_format={}, status="streamed")
    )
    _project, runner, ws_context = _make_project(tmp_path, client)
    started = asyncio.Event()

    async def _never_ready(**kwargs: typing.Any) -> typing.Any:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _run() -> None:
        collected: list[typing.Any] = []
        await _run_and_collect(ws_context, tmp_path, "tok-wait", collected)

    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        _never_ready,
    ):
        task = asyncio.ensure_future(_run())
        await asyncio.wait_for(started.wait(), timeout=5)
        _ = task.cancel()
        try:
            await task
        except (asyncio.CancelledError, ActionRunFailed):
            pass

    assert client.sent_requests == []
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-wait") == []


async def test_live_call_survives_sibling_completion(tmp_path: pathlib.Path) -> None:
    """Completing A leaves B's subscription registered and B still receives data."""
    client = _SiblingClient()
    client.configure_response(
        wm_testing.make_run_action_response(result_by_format={}, status="streamed")
    )
    _project, runner, ws_context = _make_project(tmp_path, client)
    client.runner = runner

    async def _collect(token: str, out: list[typing.Any]) -> None:
        await _run_and_collect(ws_context, tmp_path, token, out)

    collected_a: list[typing.Any] = []
    collected_b: list[typing.Any] = []
    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        return_value=runner,
    ):
        task_b = asyncio.ensure_future(_collect("tok-B", collected_b))
        await asyncio.wait_for(client.b_entered.wait(), timeout=5)
        assert len(runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-B")) == 1
        await _collect("tok-A", collected_a)
        assert len(runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-B")) == 1
        assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-A") == []
        runner_manager.admit_runner_progress(
            runner, _partial_params("tok-A", {"marker": "stale-A"})
        )
        client.b_release.set()
        await asyncio.wait_for(task_b, timeout=5)

    assert collected_a == [{"marker": "A#0"}]
    assert collected_b == [{"marker": "B#0"}, {"marker": "B#1"}]
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, "tok-B") == []


class _SiblingClient(wm_testing.FakeErClient):
    """Admits per-token values; the B response waits for test release."""

    def __init__(self) -> None:
        super().__init__()
        self.runner: typing.Any = None
        self.b_entered = asyncio.Event()
        self.b_release = asyncio.Event()

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
    ) -> typing.Any:
        assert self.runner is not None
        token = (params.options or {}).get("partialResultToken")
        assert isinstance(token, (int, str))
        if token == "tok-B":
            runner_manager.admit_runner_progress(
                self.runner, _partial_params("tok-B", {"marker": "B#0"})
            )
            self.b_entered.set()
            await self.b_release.wait()
            runner_manager.admit_runner_progress(
                self.runner, _partial_params("tok-B", {"marker": "B#1"})
            )
        else:
            runner_manager.admit_runner_progress(
                self.runner, _partial_params(token, {"marker": "A#0"})
            )
        return await super().send_request(
            method, params, timeout=timeout, on_response=on_response
        )


async def test_retired_token_publication_is_dropped(tmp_path: pathlib.Path) -> None:
    """A partial on a retired child token never reaches a live stream."""
    retired = str(uuid.uuid4())
    active = str(uuid.uuid4())
    assert retired != active
    client = _GatedClient(active)
    client.configure_response(
        wm_testing.make_run_action_response(result_by_format={}, status="streamed")
    )
    _project, runner, ws_context = _make_project(tmp_path, client)
    client.runner = runner

    async def _collect(token: str, out: list[typing.Any]) -> None:
        await _run_and_collect(ws_context, tmp_path, token, out)

    retired_out: list[typing.Any] = []
    with mock.patch.object(
        proxy_utils.runner_start_service,
        "get_or_start_runner_with_auto_prepare",
        return_value=runner,
    ):
        await _collect(retired, retired_out)
        assert retired_out == []

        live_out: list[typing.Any] = []
        task = asyncio.ensure_future(_collect(active, live_out))
        await asyncio.wait_for(client.entered.wait(), timeout=5)
        runner_manager.admit_runner_progress(
            runner, _partial_params(retired, {"marker": "stale"})
        )
        await asyncio.sleep(0.02)
        assert live_out == []
        runner_manager.admit_runner_progress(
            runner, _partial_params(active, {"marker": "live"})
        )
        client.release.set()
        await asyncio.wait_for(task, timeout=5)
        assert live_out == [{"marker": "live"}]

    assert runner.stream_registry.matching(StreamChannel.PARTIAL, retired) == []
    assert runner.stream_registry.matching(StreamChannel.PARTIAL, active) == []


class _GatedClient(wm_testing.FakeErClient):
    """Holds the gated token's response until the test releases it."""

    def __init__(self, gated_token: str) -> None:
        super().__init__()
        self.runner: typing.Any = None
        self.gated_token = gated_token
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
        on_response: typing.Any | None = None,
    ) -> typing.Any:
        token = (params.options or {}).get("partialResultToken")
        if token == self.gated_token:
            self.entered.set()
            await self.release.wait()
        return await super().send_request(
            method, params, timeout=timeout, on_response=on_response
        )
