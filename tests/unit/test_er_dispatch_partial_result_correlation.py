"""Delegated partial-result correlation isolates concurrent parent calls."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import pathlib
import typing
import uuid
from unittest import mock

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, elicitation_bridge
from finecode.wm_server.services.run_service import er_dispatch
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed

_CANONICAL_SOURCE = "test.actions.TestAction"


class _FakeStreamingCtx:
    def __init__(
        self,
        partials: list[dict],
        responses: list[typing.Any],
    ) -> None:
        self._partials = partials
        self.responses = responses

    def __aiter__(self) -> typing.AsyncIterator[dict]:
        async def _gen() -> typing.AsyncIterator[dict]:
            for item in self._partials:
                yield item

        return _gen()

    @property
    def progress(self) -> None:
        return None


def _make_params(
    partial_token: int | str | None = 7,
) -> _internal_client_types.RunActionInProjectParams:
    return _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=partial_token,
        progress_token=None,
        run_id="run-1",
    )


def _make_context(
    dir_path: pathlib.Path,
) -> tuple[typing.Any, typing.Any, typing.Any]:
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(
        working_dir_path=dir_path, env_name="test_env", client=client
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return runner, client, ws_context


def _fake_response(
    return_code: int = 0, json_result: dict | None = None, status: str = "streamed"
) -> typing.Any:
    class _Resp:
        def __init__(self) -> None:
            self.status = status
            self.result_by_format = {"json": json_result or {}}
            self.return_code = return_code

    return _Resp()


def _patch_streaming_for_markers(
    markers_by_call: list[list[dict]],
    captured_tokens: list[typing.Any],
) -> typing.Any:
    calls = {"index": 0}

    @contextlib.asynccontextmanager
    async def _fake(
        self: typing.Any,
        *,
        partial_result_token: typing.Any,
        **kwargs: typing.Any,
    ) -> collections.abc.AsyncGenerator[_FakeStreamingCtx, None]:
        _ = (self, kwargs)
        captured_tokens.append(partial_result_token)
        idx = calls["index"]
        calls["index"] += 1
        partials = markers_by_call[idx] if idx < len(markers_by_call) else []
        yield _FakeStreamingCtx(partials, [_fake_response()])

    return mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _fake,
    )


async def _run_bridge(
    runner: typing.Any,
    client: typing.Any,
    ws_context: typing.Any,
    params: _internal_client_types.RunActionInProjectParams,
) -> typing.Any:
    with mock.patch.object(
        elicitation_bridge,
        "originating_client_for_run",
        return_value=None,
    ):
        return await er_dispatch._BridgeHandlers()._run_action_in_project(
            runner,
            params,
            er_dispatch.ProjectExecutor(ws_context),
            elicitation_bridge.RunDispatchOrigin(connection=None),
        )


async def test_concurrent_equal_int_tokens_are_isolated(
    tmp_path: pathlib.Path,
) -> None:
    """Equal parent tokens still deliver only their own child partials."""
    runner_a, client_a, ws_a = _make_context(tmp_path)
    runner_b, client_b, ws_b = _make_context(tmp_path)
    captured: list[typing.Any] = []
    markers = [[{"json": {"marker": "A#0"}}], [{"json": {"marker": "B#0"}}]]

    with _patch_streaming_for_markers(markers, captured):
        await asyncio.gather(
            _run_bridge(runner_a, client_a, ws_a, _make_params(partial_token=7)),
            _run_bridge(runner_b, client_b, ws_b, _make_params(partial_token=7)),
        )

    assert len(captured) == 2
    assert captured[0] != captured[1]
    for token in captured:
        assert isinstance(token, str)
        _ = uuid.UUID(token)
        assert token != "7"
        assert token != 7  # type: ignore[comparison-overlap]
    for _, notif in client_a.sent_requests:
        assert notif.token == 7
        assert json.loads(notif.value) == {"marker": "A#0"}
    for _, notif in client_b.sent_requests:
        assert notif.token == 7
        assert json.loads(notif.value) == {"marker": "B#0"}


async def test_concurrent_equal_string_tokens_are_isolated(
    tmp_path: pathlib.Path,
) -> None:
    """Equal string parent tokens still deliver only their own child partials."""
    runner_a, client_a, ws_a = _make_context(tmp_path)
    runner_b, client_b, ws_b = _make_context(tmp_path)
    captured: list[typing.Any] = []
    markers = [[{"json": {"marker": "A#0"}}], [{"json": {"marker": "B#0"}}]]

    with _patch_streaming_for_markers(markers, captured):
        await asyncio.gather(
            _run_bridge(runner_a, client_a, ws_a, _make_params(partial_token="shared")),
            _run_bridge(runner_b, client_b, ws_b, _make_params(partial_token="shared")),
        )

    assert len(captured) == 2
    assert captured[0] != captured[1]
    for token in captured:
        assert isinstance(token, str)
        _ = uuid.UUID(token)
        assert token != "shared"
    assert [n.token for _, n in client_a.sent_requests] == ["shared"]
    assert [n.token for _, n in client_b.sent_requests] == ["shared"]


async def test_absent_partial_token_uses_non_streaming_branch(
    tmp_path: pathlib.Path,
) -> None:
    """Without a partial token the bridge never opens a streaming run."""
    runner, client, ws_context = _make_context(tmp_path)
    params = _make_params(partial_token=None)
    with (
        mock.patch.object(
            er_dispatch.ProjectExecutor,
            "run_action_with_partial_results",
        ) as streaming_mock,
        mock.patch.object(
            er_dispatch.ProjectExecutor,
            "run_action",
            return_value=_fake_response(
                return_code=0, json_result={"ok": True}, status="ok"
            ),
        ) as run_mock,
        mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ),
    ):
        result = await er_dispatch._BridgeHandlers()._run_action_in_project(
            runner,
            params,
            er_dispatch.ProjectExecutor(ws_context),
            elicitation_bridge.RunDispatchOrigin(connection=None),
        )
    streaming_mock.assert_not_called()
    run_mock.assert_called_once()
    assert result.return_code == 0
    assert result.result == {"ok": True}
    assert client.sent_requests == []


async def test_zero_and_empty_string_remain_streaming_tokens(
    tmp_path: pathlib.Path,
) -> None:
    """Falsy but present tokens still select the streamed branch."""
    for present in (0, ""):
        runner, client, ws_context = _make_context(tmp_path)
        captured: dict[str, typing.Any] = {}

        @contextlib.asynccontextmanager
        async def _fake(
            self: typing.Any,
            *,
            partial_result_token: typing.Any,
            _captured: dict[str, typing.Any] = captured,
            **kwargs: typing.Any,
        ) -> collections.abc.AsyncGenerator[_FakeStreamingCtx, None]:
            _ = (self, kwargs)
            _captured["token"] = partial_result_token
            yield _FakeStreamingCtx([{"json": {"text": "p"}}], [_fake_response()])

        params = _make_params(partial_token=present)
        with (
            mock.patch.object(
                er_dispatch.ProjectExecutor,
                "run_action_with_partial_results",
                _fake,
            ),
            mock.patch.object(
                elicitation_bridge,
                "originating_client_for_run",
                return_value=None,
            ),
        ):
            await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                params,
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )
        child = captured["token"]
        assert isinstance(child, str)
        _ = uuid.UUID(child)
        assert [n.token for _, n in client.sent_requests] == [present]


async def test_direct_final_fallback_emits_once_on_parent_token(
    tmp_path: pathlib.Path,
) -> None:
    """An empty partial stream forwards the final JSON to the parent token."""
    runner, client, ws_context = _make_context(tmp_path)

    @contextlib.asynccontextmanager
    async def _fake(
        self: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_FakeStreamingCtx, None]:
        _ = (self, kwargs)
        yield _FakeStreamingCtx(
            [], [_fake_response(json_result={"text": "final"}, status="ok")]
        )

    with (
        mock.patch.object(
            er_dispatch.ProjectExecutor,
            "run_action_with_partial_results",
            _fake,
        ),
        mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ),
    ):
        result = await er_dispatch._BridgeHandlers()._run_action_in_project(
            runner,
            _make_params(partial_token=9),
            er_dispatch.ProjectExecutor(ws_context),
            elicitation_bridge.RunDispatchOrigin(connection=None),
        )
    assert result.return_code == 0
    assert len(client.sent_requests) == 1
    _, notif = client.sent_requests[0]
    assert notif.token == 9
    assert json.loads(notif.value) == {"text": "final"}


async def test_streamed_completion_carries_no_result_data(
    tmp_path: pathlib.Path,
) -> None:
    """A streamed reply is completion metadata only, never result data."""
    runner, client, ws_context = _make_context(tmp_path)

    @contextlib.asynccontextmanager
    async def _fake(
        self: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_FakeStreamingCtx, None]:
        _ = (self, kwargs)
        yield _FakeStreamingCtx([{"json": {"text": "p"}}], [_fake_response()])

    with (
        mock.patch.object(
            er_dispatch.ProjectExecutor,
            "run_action_with_partial_results",
            _fake,
        ),
        mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ),
    ):
        result = await er_dispatch._BridgeHandlers()._run_action_in_project(
            runner,
            _make_params(partial_token=11),
            er_dispatch.ProjectExecutor(ws_context),
            elicitation_bridge.RunDispatchOrigin(connection=None),
        )
    assert result.return_code == 0
    assert result.result is None
    assert [n.token for _, n in client.sent_requests] == [11]


async def test_child_error_is_not_replaced_by_success(
    tmp_path: pathlib.Path,
) -> None:
    """A failing child run still fails the delegated call."""
    runner, _client, ws_context = _make_context(tmp_path)

    @contextlib.asynccontextmanager
    async def _failing(
        *args: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[typing.NoReturn, None]:
        raise ActionRunFailed("child failed")
        yield  # pragma: no cover

    with (
        mock.patch.object(
            er_dispatch.ProjectExecutor,
            "run_action_with_partial_results",
            _failing,
        ),
        mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ),
    ):
        try:
            await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                _make_params(partial_token=13),
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )
        except ActionRunFailed as exc:
            assert "child failed" in exc.message
        else:
            raise AssertionError("expected ActionRunFailed")
