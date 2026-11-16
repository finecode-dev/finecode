"""Streaming depth metadata: executor increment and transport serialization."""

from __future__ import annotations

import contextlib
import inspect
import pathlib
import typing
from unittest import mock

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services.run_service import project_executor, proxy_utils
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.execution_scopes import (
    DEFAULT_ORCHESTRATION_POLICY,
)

_CANONICAL_SOURCE = "test.actions.TestAction"


def _make_executor(tmp_path: pathlib.Path):
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return project_executor.ProjectExecutor(ws_context), ws_context


class _FakeCtx:
    def __init__(self) -> None:
        self.responses: list[typing.Any] = []

    def __aiter__(self) -> typing.AsyncIterator[typing.Any]:
        async def _gen() -> typing.AsyncIterator[typing.Any]:
            if False:
                yield {}

        return _gen()

    @property
    def progress(self) -> None:
        return None


async def test_executor_forwards_parent_depth_plus_one(tmp_path: pathlib.Path) -> None:
    """The streaming executor increments depth like the plain executor does."""
    executor, _ws_context = _make_executor(tmp_path)
    captured: dict[str, typing.Any] = {}

    @contextlib.asynccontextmanager
    async def _fake_run_with_partial_results(
        **kwargs: typing.Any,
    ) -> typing.AsyncGenerator[_FakeCtx, None]:
        captured.update(kwargs)
        yield _FakeCtx()

    with mock.patch.object(
        proxy_utils, "run_with_partial_results", _fake_run_with_partial_results
    ):
        async with executor.run_action_with_partial_results(
            action_source=_CANONICAL_SOURCE,
            params={},
            project_path=tmp_path,
            partial_result_token="tok",
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            orchestration_depth=3,
            origin=None,
        ):
            pass
    assert captured["orchestration_depth"] == 4


async def test_transport_serializes_depth_in_meta() -> None:
    """The WM->ER transport carries depth under the supported meta key."""
    captured: dict[str, typing.Any] = {}

    async def _fake_run_action_in_runner(
        action_name: str,
        params: dict[str, typing.Any],
        runner: runner_client.ExtensionRunnerInfo,
        options: dict[str, typing.Any] | None = None,
    ) -> runner_client.RunActionResponse:
        captured.update(options or {})
        return runner_client.RunActionResponse(
            result_by_format={}, return_code=0, status="ok"
        )

    runner = wm_testing.make_running_runner(working_dir_path=pathlib.Path("/tmp"))
    with mock.patch.object(
        proxy_utils, "run_action_in_runner", _fake_run_action_in_runner
    ):
        await proxy_utils.run_action_and_notify(
            action_name="test_action",
            params={},
            partial_result_token="tok",
            runner=runner,
            run_trigger=runner_client.RunActionTrigger.USER,
            dev_env=runner_client.DevEnv.CI,
            wal_run_id="wal-1",
            result_formats=None,
            progress_token="work-1",
            caller_kwargs={"k": "v"},
            orchestration_depth=5,
            wal_writer=None,
        )
    assert captured["meta"]["orchestrationDepth"] == 5
    assert captured["meta"]["trigger"] == "user"
    assert captured["meta"]["devEnv"] == "ci"
    assert captured["partialResultToken"] == "tok"
    assert captured["progressToken"] == "work-1"
    assert captured["runId"] == "wal-1"
    assert captured["callerKwargs"] == {"k": "v"}


async def test_streaming_refuses_at_max_depth_before_dispatch(
    tmp_path: pathlib.Path,
) -> None:
    """Depth-limit refusal happens before any runner dispatch."""
    executor, _ws_context = _make_executor(tmp_path)
    called = False

    @contextlib.asynccontextmanager
    async def _fake_run_with_partial_results(
        **kwargs: typing.Any,
    ) -> typing.AsyncGenerator[_FakeCtx, None]:
        nonlocal called
        called = True
        yield _FakeCtx()

    with mock.patch.object(
        proxy_utils, "run_with_partial_results", _fake_run_with_partial_results
    ):
        try:
            async with executor.run_action_with_partial_results(
                action_source=_CANONICAL_SOURCE,
                params={},
                project_path=tmp_path,
                partial_result_token="tok",
                run_trigger=runner_client.RunActionTrigger.USER,
                dev_env=runner_client.DevEnv.CI,
                orchestration_depth=DEFAULT_ORCHESTRATION_POLICY.max_recursion_depth,
                origin=None,
            ):
                pass
        except ActionRunFailed as exc:
            assert "Orchestration depth" in exc.message
        else:
            raise AssertionError("expected ActionRunFailed")
    assert called is False


def test_streaming_helper_keeps_zero_default() -> None:
    """Direct streaming callers keep depth zero unless they opt in."""
    sig = inspect.signature(proxy_utils.run_with_partial_results)
    param = sig.parameters["orchestration_depth"]
    assert param.default == 0
    assert param.kind == inspect.Parameter.KEYWORD_ONLY


def test_streaming_helper_wal_writer_defaults_to_none() -> None:
    """Only the delegated path records streaming lifecycle events."""
    sig = inspect.signature(proxy_utils.run_with_partial_results)
    param = sig.parameters["wal_writer"]
    assert param.default is None
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
