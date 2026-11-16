"""WM producer forwards child work progress on a distinct token."""

from __future__ import annotations

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

_CANONICAL_SOURCE = "test.actions.TestAction"


class _FakeProgressList:
    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def __aiter__(self) -> typing.AsyncIterator[dict]:
        async def _gen() -> typing.AsyncIterator[dict]:
            for item in self._items:
                yield item

        return _gen()


class _FakeStreamingCtx:
    def __init__(
        self,
        partials: list[dict],
        progresses: list[dict] | None,
        responses: list[typing.Any],
    ) -> None:
        self._partials = partials
        self._progresses = progresses
        self.responses = responses

    def __aiter__(self) -> typing.AsyncIterator[dict]:
        async def _gen() -> typing.AsyncIterator[dict]:
            for item in self._partials:
                yield item

        return _gen()

    @property
    def progress(self) -> _FakeProgressList | None:
        if self._progresses is None:
            return None
        return _FakeProgressList(self._progresses)


def _make_params(
    partial_token: int | str | None = 7,
    progress_token: int | str | None = None,
) -> _internal_client_types.RunActionInProjectParams:
    return _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=partial_token,
        progress_token=progress_token,
        run_id="run-1",
    )


def _make_context(dir_path: pathlib.Path) -> tuple[typing.Any, typing.Any, typing.Any]:
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


def _fake_response(return_code: int = 0, json_result: dict | None = None) -> typing.Any:
    class _Resp:
        def __init__(self) -> None:
            self.status = "ok" if json_result is not None else "streamed"
            self.result_by_format = {"json": json_result or {}}
            self.return_code = return_code

    return _Resp()


def _patch_streaming(
    partials: list[dict],
    progresses: list[dict] | None,
    responses: list[typing.Any],
    captured: dict[str, typing.Any],
):
    @contextlib.asynccontextmanager
    async def _fake_run_action_with_partial_results(
        self: typing.Any,
        *,
        action_source: typing.Any,
        params: typing.Any,
        project_path: typing.Any,
        partial_result_token: typing.Any,
        run_trigger: typing.Any,
        dev_env: typing.Any,
        orchestration_depth: typing.Any,
        caller_kwargs: typing.Any,
        origin: typing.Any,
        progress_token: typing.Any = None,
        **kwargs: typing.Any,
    ) -> collections.abc.AsyncGenerator[_FakeStreamingCtx, None]:
        _ = (
            action_source,
            params,
            project_path,
            run_trigger,
            dev_env,
            orchestration_depth,
            caller_kwargs,
            kwargs,
        )
        captured["progress_token"] = progress_token
        captured["partial_result_token"] = partial_result_token
        captured["origin"] = origin
        yield _FakeStreamingCtx(partials, progresses, responses)

    return mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _fake_run_action_with_partial_results,
    )


async def test_progress_and_partial_tokens_stay_separate(
    tmp_path: pathlib.Path,
) -> None:
    """Progress values go to the work-progress token, partials to the partial token."""
    runner, client, ws_context = _make_context(tmp_path)
    captured: dict[str, typing.Any] = {}
    partials = [{"json": {"text": "p1"}}, {"json": {"text": "p2"}}]
    progresses = [
        {"type": "report", "message": "hello", "percentage": 10},
        {"type": "report", "message": "world", "percentage": 50},
    ]
    responses = [_fake_response()]

    params = _make_params(partial_token=7, progress_token="work-1")
    with _patch_streaming(partials, progresses, responses, captured):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            result = await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                params,
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    assert result.return_code == 0
    child_token = captured["partial_result_token"]
    assert isinstance(child_token, str)
    _ = uuid.UUID(child_token)
    assert child_token != "7"
    child_progress_token = captured["progress_token"]
    assert isinstance(child_progress_token, str)
    _ = uuid.UUID(child_progress_token)
    assert child_progress_token != "work-1"
    assert child_progress_token != child_token
    by_token: dict[typing.Any, list[typing.Any]] = {}
    for method, notif in client.sent_requests:
        assert method == "$/progress"
        by_token.setdefault(notif.token, []).append(json.loads(notif.value))
    assert by_token[7] == [{"text": "p1"}, {"text": "p2"}]
    assert by_token["work-1"] == progresses


async def test_equal_parent_tokens_get_distinct_child_tokens(
    tmp_path: pathlib.Path,
) -> None:
    """Two concurrent calls sharing a parent token still get distinct child tokens."""
    runner, _client, ws_context = _make_context(tmp_path)
    captured_a: dict[str, typing.Any] = {}
    captured_b: dict[str, typing.Any] = {}
    responses = [_fake_response()]

    params = _make_params(partial_token=21, progress_token="work-shared")
    with _patch_streaming([], [], responses, captured_a):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                params,
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )
    with _patch_streaming([], [], responses, captured_b):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                params,
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    token_a = captured_a["progress_token"]
    token_b = captured_b["progress_token"]
    assert isinstance(token_a, str) and isinstance(token_b, str)
    _ = uuid.UUID(token_a)
    _ = uuid.UUID(token_b)
    assert token_a != "work-shared"
    assert token_b != "work-shared"
    assert token_a != token_b
    assert token_a != captured_a["partial_result_token"]
    assert token_b != captured_b["partial_result_token"]


async def test_final_return_fallback_still_applies_with_progress(
    tmp_path: pathlib.Path,
) -> None:
    """An empty partial stream still forwards the final JSON to the partial token."""
    runner, client, ws_context = _make_context(tmp_path)
    captured: dict[str, typing.Any] = {}
    progresses = [{"type": "report", "message": "only", "percentage": None}]
    responses = [_fake_response(json_result={"text": "final"})]

    params = _make_params(partial_token=9, progress_token="work-2")
    with _patch_streaming([], progresses, responses, captured):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                params,
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    by_token: dict[typing.Any, list[typing.Any]] = {}
    for method, notif in client.sent_requests:
        by_token.setdefault(notif.token, []).append(json.loads(notif.value))
    assert by_token[9] == [{"text": "final"}]
    assert by_token["work-2"] == progresses


async def test_no_progress_request_is_unchanged(tmp_path: pathlib.Path) -> None:
    """Without a progress token the producer passes None and sends no progress."""
    runner, client, ws_context = _make_context(tmp_path)
    captured: dict[str, typing.Any] = {}
    partials = [{"json": {"text": "p"}}]
    responses = [_fake_response()]

    params = _make_params(partial_token=11, progress_token=None)
    with _patch_streaming(partials, None, responses, captured):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            await er_dispatch._BridgeHandlers()._run_action_in_project(
                runner,
                params,
                er_dispatch.ProjectExecutor(ws_context),
                elicitation_bridge.RunDispatchOrigin(connection=None),
            )

    assert captured["progress_token"] is None
    tokens = [notif.token for _, notif in client.sent_requests]
    assert tokens == [11]


async def test_streaming_failure_propagates(tmp_path: pathlib.Path) -> None:
    """A failing streaming run raises instead of replying success."""
    from finecode.wm_server.services.run_service.exceptions import ActionRunFailed

    runner, _client, ws_context = _make_context(tmp_path)

    @contextlib.asynccontextmanager
    async def _failing(
        *args: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[typing.NoReturn, None]:
        raise ActionRunFailed("child failed")
        yield  # pragma: no cover

    params = _make_params(partial_token=13, progress_token="work-3")
    with mock.patch.object(
        er_dispatch.ProjectExecutor,
        "run_action_with_partial_results",
        _failing,
    ):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=None,
        ):
            try:
                await er_dispatch._BridgeHandlers()._run_action_in_project(
                    runner,
                    params,
                    er_dispatch.ProjectExecutor(ws_context),
                    elicitation_bridge.RunDispatchOrigin(connection=None),
                )
            except ActionRunFailed as exc:
                assert "child failed" in exc.message
            else:
                raise AssertionError("expected ActionRunFailed")


async def test_origin_is_inherited_from_calling_run(tmp_path: pathlib.Path) -> None:
    """The nested run inherits the calling run's originating client."""
    runner, _client, ws_context = _make_context(tmp_path)
    captured: dict[str, typing.Any] = {}
    sentinel = object()

    params = _make_params(partial_token=15, progress_token="work-4")
    with _patch_streaming([{"json": {"text": "p"}}], [], [_fake_response()], captured):
        with mock.patch.object(
            elicitation_bridge,
            "originating_client_for_run",
            return_value=sentinel,
        ) as origin_mock:
            origin = er_dispatch._origin_of_calling_run(params.run_id)
            assert origin.connection is sentinel
            origin_mock.assert_called_once_with("run-1")
            await er_dispatch._BridgeHandlers().run_action_in_project(
                runner, params, ws_context
            )

    assert captured["origin"].connection is sentinel
