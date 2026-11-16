"""Prefix-completion forwarding for the direct service, matrix fan-out and API sink.

Every streamed prefix a runner emits must complete through listeners,
aggregation and the local writer handoff before the matching terminal
response is exposed. These tests pin the concurrent-service composition
(task 4): the owned service context yields while producers are still
running, relays carry fences past transformation, matrix variants forward
prefix receipts, and failures surface only after their prefixes.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import pathlib
import types
import typing

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode.wm_server.runner.runner_client import RunActionResponse, RunResultFormat
from finecode.wm_server.services import partial_results_service
from finecode.wm_server.services.run_service import (
    ActionRunFailed,
    DevEnv,
    RunActionTrigger,
    matrix_streaming,
)
from finecode.wm_server.utils.stream_completion import (
    CompletionStream,
    PipelineFailure,
    PrefixFence,
)


def _make_collected(path: pathlib.Path) -> domain.CollectedProject:
    handler = domain.ActionHandler(
        name="h",
        source="test.handlers.TestHandler",
        config={},
        env="dev_workspace",
        dependencies=[],
    )
    action = domain.Action(
        name="lint",
        source="test.actions.LintAction",
        handlers=[handler],
        config={},
    )
    action.canonical_source = "test.actions.LintAction"
    action.scope = domain.ActionScope.PROJECT
    return domain.CollectedProject(
        name=path.name,
        dir_path=path,
        def_path=path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[action],
        services=[],
        action_handler_configs={},
    )


class _Outcome:
    def __init__(self, responses: list[RunActionResponse]) -> None:
        self.responses = responses

    def require(self, _paths: list[pathlib.Path]) -> list[domain.CollectedProject]:
        raise AssertionError("unused")


def _stub_routing(
    monkeypatch: pytest.MonkeyPatch,
    _ws_context: context.WorkspaceContext,
    project: domain.CollectedProject,
) -> None:
    async def _fake_ensure(
        _paths: list[pathlib.Path], _ws_context: context.WorkspaceContext
    ) -> typing.Any:
        class _O:
            def require(
                self, _inner_paths: list[pathlib.Path]
            ) -> list[domain.CollectedProject]:
                return [project]

        return _O()

    async def _noop_start(*args: typing.Any, **kwargs: typing.Any) -> None:
        return None

    monkeypatch.setattr(
        partial_results_service.project_resolution_service,
        "ensure_projects_resolved",
        _fake_ensure,
    )
    monkeypatch.setattr(
        partial_results_service, "start_required_environments", _noop_start
    )


class _ScriptedProxyCtx:
    """Stands in for the delegated ``run_with_partial_results`` context."""

    def __init__(
        self,
        *,
        partial_values: list[dict[str, typing.Any]] | None = None,
        fences: list[PrefixFence] | None = None,
        progress_values: list[dict[str, typing.Any]] | None = None,
        progress_fences: list[PrefixFence] | None = None,
        responses: list[RunActionResponse] | None = None,
        exit_error: BaseException | None = None,
        with_progress: bool = False,
        hold_open: bool = False,
    ) -> None:
        self.partials: CompletionStream[dict[str, typing.Any]] = CompletionStream()
        self.progress: CompletionStream[dict[str, typing.Any]] | None = (
            CompletionStream() if with_progress else None
        )
        self.responses: list[RunActionResponse] = []
        if responses is not None:
            self.responses.extend(responses)
        self._exit_error = exit_error
        self.exited = False
        # Scripted production is queued eagerly, and ended like the real
        # coordinator ends independently of context exit, so relays
        # observing END are never stuck waiting for the body to finish.
        # `hold_open` keeps the streams open for producer-gated tests that
        # end them explicitly once their gate releases.
        for value in partial_values or []:
            self.partials.append(value)
        for fence in fences or []:
            self.partials.forward(fence)
        if self.progress is not None:
            for value in progress_values or []:
                self.progress.append(value)
            for fence in progress_fences or []:
                self.progress.forward(fence)
        if not hold_open:
            self.partials.end()
            if self.progress is not None:
                self.progress.end()

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        self.exited = True
        self.partials.end()
        if self.progress is not None:
            self.progress.end()
        if self._exit_error is not None:
            raise self._exit_error
        return False


def _make_fence() -> PrefixFence:
    return PrefixFence(completion=asyncio.get_running_loop().create_future())


async def _drain(
    stream: typing.AsyncIterable[typing.Any],
) -> list[typing.Any]:
    return [item async for item in stream]


async def _drain_it(it: typing.AsyncIterator[typing.Any]) -> list[typing.Any]:
    return [item async for item in it]


async def test_service_yields_stream_while_producer_is_still_running(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owned service context yields before its coordinator completes, so a
    consumer observes the first partial while the producer is still blocked —
    the old call shape could only return after every producer finished."""
    project = _make_collected(tmp_path / "a")
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path / "a"])
    ws_context.ws_projects[tmp_path / "a"] = project
    _stub_routing(monkeypatch, ws_context, project)

    release = asyncio.Event()
    produced: list[str] = []

    @contextlib.asynccontextmanager
    async def _gated_dispatch(
        **kwargs: typing.Any,
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        inner = _ScriptedProxyCtx(
            responses=[
                RunActionResponse(result_by_format={}, return_code=0, status="streamed")
            ],
            hold_open=True,
        )
        inner.partials.append({"project": "p", "marker": "first"})
        produced.append("first")

        async def _late() -> None:
            await release.wait()
            produced.append("second")
            inner.partials.append({"project": "p", "marker": "second"})
            inner.partials.end()

        late_task = asyncio.ensure_future(_late())
        try:
            yield inner
        finally:
            _ = late_task.cancel()
            await asyncio.gather(late_task, return_exceptions=True)
            inner.partials.end()

    monkeypatch.setattr(
        partial_results_service, "run_with_partial_results", _gated_dispatch
    )

    async with partial_results_service.run_action_with_partial_results(
        action_name="lint",
        project_path=str(tmp_path / "a"),
        params={},
        partial_result_token="tok",
        run_trigger=RunActionTrigger("system"),
        dev_env=DevEnv("ide"),
        ws_context=ws_context,
        origin=None,
        result_formats=["json"],
    ) as stream:
        it = stream.__aiter__()
        first = await asyncio.wait_for(it.__anext__(), timeout=5)
        assert first["resultByFormat"]["marker"] == "first"
        # The producer is still blocked on `release`; the second value cannot
        # be available yet — concurrency, not buffering, delivered the first.
        assert produced == ["first"]
        release.set()
        second = await asyncio.wait_for(it.__anext__(), timeout=5)
        assert second["resultByFormat"]["marker"] == "second"
        rest = await asyncio.wait_for(_drain_it(it), timeout=5)
        assert rest == []
        final = await asyncio.wait_for(stream.final_result(), timeout=5)
        assert final == {"returnCode": 0}


async def test_service_error_surfaces_only_after_its_prefix(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A received runner error waits for its prefix: values and fences drain
    first, and the error is exposed after forwarding, not by cancelling the
    consumer."""
    project = _make_collected(tmp_path / "a")
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path / "a"])
    ws_context.ws_projects[tmp_path / "a"] = project
    _stub_routing(monkeypatch, ws_context, project)

    fence = _make_fence()

    @contextlib.asynccontextmanager
    async def _failing_dispatch(
        **kwargs: typing.Any,
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        inner = _ScriptedProxyCtx(
            partial_values=[{"project": "p", "marker": "only"}],
            fences=[fence],
            responses=[],
            exit_error=ActionRunFailed("runner blew up"),
        )
        async with inner:
            yield inner

    monkeypatch.setattr(
        partial_results_service, "run_with_partial_results", _failing_dispatch
    )

    seen: list[dict[str, typing.Any]] = []
    with pytest.raises(ActionRunFailed, match="runner blew up"):
        async with partial_results_service.run_action_with_partial_results(
            action_name="lint",
            project_path=str(tmp_path / "a"),
            params={},
            partial_result_token="tok",
            run_trigger=RunActionTrigger("system"),
            dev_env=DevEnv("ide"),
            ws_context=ws_context,
            origin=None,
            result_formats=["json"],
        ) as stream:
            seen = await asyncio.wait_for(_drain(stream), timeout=5)
            assert [item["resultByFormat"]["marker"] for item in seen] == ["only"]
            # The fence completed through the consumer handoff before the
            # error surfaced on context exit.
            assert fence.completion.done()
            assert fence.completion.result() is None
            with pytest.raises(ActionRunFailed, match="runner blew up"):
                await asyncio.wait_for(stream.final_result(), timeout=5)


async def test_service_forwards_progress_fence_without_aggregator_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A progress fence is forwarded even when aggregation emits no
    notification — a suppressed end must not strand the runner drain."""
    project = _make_collected(tmp_path / "a")
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path / "a"])
    ws_context.ws_projects[tmp_path / "a"] = project
    _stub_routing(monkeypatch, ws_context, project)

    fence = _make_fence()

    @contextlib.asynccontextmanager
    async def _progress_dispatch(
        **kwargs: typing.Any,
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        # A bare "begin" for one of two projects produces no aggregated
        # output on its own; the fence must still travel.
        inner = _ScriptedProxyCtx(
            progress_values=[],
            progress_fences=[fence],
            responses=[
                RunActionResponse(result_by_format={}, return_code=0, status="streamed")
            ],
            with_progress=True,
        )
        async with inner:
            yield inner

    monkeypatch.setattr(
        partial_results_service, "run_with_partial_results", _progress_dispatch
    )

    async with partial_results_service.run_action_with_partial_results(
        action_name="lint",
        project_path=str(tmp_path / "a"),
        params={},
        partial_result_token="tok",
        run_trigger=RunActionTrigger("system"),
        dev_env=DevEnv("ide"),
        ws_context=ws_context,
        origin=None,
        result_formats=["json"],
        progress_token="prog",
    ) as stream:
        assert stream.progress_stream is not None
        seen = await asyncio.wait_for(_drain(stream.progress_stream), timeout=5)
        assert seen == []
        assert fence.completion.done()
        assert fence.completion.result() is None


async def test_service_emits_direct_final_as_partial(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner that streamed nothing still delivers its final result as a
    partial, preserving the direct-result delivery mode."""
    project = _make_collected(tmp_path / "a")
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path / "a"])
    ws_context.ws_projects[tmp_path / "a"] = project
    _stub_routing(monkeypatch, ws_context, project)

    @contextlib.asynccontextmanager
    async def _direct_dispatch(
        **kwargs: typing.Any,
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        inner = _ScriptedProxyCtx(
            responses=[
                RunActionResponse(
                    result_by_format={"json": {"ok": True}},
                    return_code=0,
                    status="success",
                )
            ]
        )
        async with inner:
            yield inner

    monkeypatch.setattr(
        partial_results_service, "run_with_partial_results", _direct_dispatch
    )

    async with partial_results_service.run_action_with_partial_results(
        action_name="lint",
        project_path=str(tmp_path / "a"),
        params={},
        partial_result_token="tok",
        run_trigger=RunActionTrigger("system"),
        dev_env=DevEnv("ide"),
        ws_context=ws_context,
        origin=None,
        result_formats=["json"],
    ) as stream:
        seen = await asyncio.wait_for(_drain(stream), timeout=5)
        assert len(seen) == 1
        assert seen[0]["resultByFormat"] == {"json": {"ok": True}}


def _make_matrix_action(*, interpreters: list[str]) -> domain.Action:
    handlers = [
        domain.ActionHandler(
            name=f"handler_{interpreter}",
            source="test.handlers.TestHandler",
            config={},
            env=f"testing@{interpreter}",
            dependencies=[],
            interpreter=interpreter,
        )
        for interpreter in interpreters
    ]
    return domain.Action(
        name="test_action",
        source="test.actions.TestAction",
        handlers=handlers,
        config={},
    )


def _make_matrix_project() -> typing.Any:
    return types.SimpleNamespace(dir_path=pathlib.Path("/fake/project"))


def _scripted_matrix_dispatch(
    scripted: dict[str, tuple[list[dict[str, typing.Any]], list[RunActionResponse]]],
    *,
    exit_errors: dict[str, BaseException] | None = None,
) -> typing.Any:
    resolved_errors: dict[str, BaseException] = (
        dict(exit_errors) if exit_errors is not None else {}
    )

    @contextlib.asynccontextmanager
    async def fake_run_with_partial_results(
        *, interpreter: typing.Any = None, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        assert interpreter is not None
        canonical: str = interpreter.canonical
        partials, responses = scripted[canonical]
        inner = _ScriptedProxyCtx(
            partial_values=[*partials],
            responses=[*responses],
            exit_error=resolved_errors.get(canonical),
        )
        async with inner:
            yield inner

    return fake_run_with_partial_results


async def test_matrix_forwards_prefix_fence_to_on_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matrix variant drains its streamed prefix through the new fence
    callback: values reach ``on_partial`` and the fence reaches
    ``on_prefix`` in order."""
    cpython_311 = Interpreter("cpython", "3.11")
    action = _make_matrix_action(interpreters=[cpython_311.canonical])
    fence = _make_fence()

    @contextlib.asynccontextmanager
    async def fake_dispatch(
        *, _interpreter: typing.Any = None, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        inner = _ScriptedProxyCtx(
            partial_values=[{"json": {"a": 1}}],
            fences=[fence],
            responses=[
                RunActionResponse(result_by_format={}, return_code=0, status="streamed")
            ],
        )
        async with inner:
            yield inner

    monkeypatch.setattr(
        matrix_streaming.proxy_utils, "run_with_partial_results", fake_dispatch
    )

    seen_values: list[dict[str, typing.Any]] = []
    seen_fences: list[PrefixFence] = []

    async def on_partial(_canonical: str, value: dict[str, typing.Any]) -> None:
        seen_values.append(value)

    async def on_prefix(received: PrefixFence) -> None:
        seen_fences.append(received)
        if not received.completion.done():
            received.completion.set_result(None)

    _combined, return_code = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_matrix_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=[RunResultFormat("json")],
        partial_result_token="tok",
        run_trigger=RunActionTrigger("system"),
        dev_env=DevEnv("ide"),
        ws_context=typing.cast(typing.Any, None),
        merge_results=False,
        on_partial=on_partial,
        on_prefix=on_prefix,
        origin=None,
    )

    assert return_code == 0
    assert seen_values == [{"json": {"a": 1}}]
    assert seen_fences == [fence]
    assert fence.completion.done()
    assert fence.completion.result() is None


async def test_matrix_stream_only_failure_is_not_synthesized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken output callback fails the matrix run instead of becoming a
    synthetic failed-variant response, and the sibling variant is joined."""
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )

    started: list[str] = []
    finished: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_dispatch(
        *, interpreter: typing.Any = None, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        assert interpreter is not None
        canonical: str = interpreter.canonical
        started.append(canonical)
        try:
            inner = _ScriptedProxyCtx(
                partial_values=[{"json": {"v": canonical}}],
                responses=[
                    RunActionResponse(
                        result_by_format={}, return_code=0, status="streamed"
                    )
                ],
            )
            async with inner:
                yield inner
        finally:
            finished.append(canonical)

    monkeypatch.setattr(
        matrix_streaming.proxy_utils, "run_with_partial_results", fake_dispatch
    )

    async def on_partial(canonical: str, _value: dict[str, typing.Any]) -> None:
        if canonical == cpython_311.canonical:
            raise RuntimeError("sink blew up")

    async def on_prefix(received: PrefixFence) -> None:
        if not received.completion.done():
            received.completion.set_result(None)

    with pytest.raises(PipelineFailure, match="sink blew up") as excinfo:
        (
            _combined,
            _return_code,
        ) = await matrix_streaming.run_matrix_with_partial_results(
            project=_make_matrix_project(),
            action=action,
            action_name="test_action",
            params={},
            result_formats=[RunResultFormat("json")],
            partial_result_token="tok",
            run_trigger=RunActionTrigger("system"),
            dev_env=DevEnv("ide"),
            ws_context=typing.cast(typing.Any, None),
            merge_results=False,
            on_partial=on_partial,
            on_prefix=on_prefix,
            origin=None,
        )

    assert sorted(started) == sorted([cpython_311.canonical, cpython_312.canonical])
    assert sorted(finished) == sorted(started)
    assert isinstance(excinfo.value.cause, RuntimeError)


async def test_matrix_dual_fault_keeps_runner_only_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the runner errored and the prefix handoff also failed, the variant
    keeps its runner-only failed representation — the stream fault is recorded
    on the fence, never merged into the runner result."""
    cpython_311 = Interpreter("cpython", "3.11")
    action = _make_matrix_action(interpreters=[cpython_311.canonical])
    fence = _make_fence()

    @contextlib.asynccontextmanager
    async def fake_dispatch(
        *, _interpreter: typing.Any = None, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        inner = _ScriptedProxyCtx(
            partial_values=[{"json": {"a": 1}}],
            fences=[fence],
            responses=[],
            exit_error=ActionRunFailed("runner blew up"),
        )
        async with inner:
            yield inner

    monkeypatch.setattr(
        matrix_streaming.proxy_utils, "run_with_partial_results", fake_dispatch
    )

    async def on_partial(_canonical: str, _value: dict[str, typing.Any]) -> None:
        raise RuntimeError("sink blew up")

    async def on_prefix(received: PrefixFence) -> None:
        if not received.completion.done():
            received.completion.set_result(None)

    combined, return_code = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_matrix_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=[RunResultFormat("json")],
        partial_result_token="tok",
        run_trigger=RunActionTrigger("system"),
        dev_env=DevEnv("ide"),
        ws_context=typing.cast(typing.Any, None),
        merge_results=False,
        on_partial=on_partial,
        on_prefix=on_prefix,
        origin=None,
    )

    assert return_code == 1
    by_interpreter = combined["json"]
    payload = by_interpreter[cpython_311.canonical]
    assert "runner blew up" in str(payload)
    assert "sink blew up" not in str(combined)
    # The failed handoff settled the fence with the pipeline fault so the
    # runner drain could not strand waiting for an ack that never comes.
    assert fence.completion.done()
    assert isinstance(fence.completion.result(), PipelineFailure)


async def test_service_failed_sink_aborts_producer_without_waiting(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed direct-service sink aborts the producer instead of waiting
    for an eventual runner response."""
    project = _make_collected(tmp_path / "a")
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path / "a"])
    ws_context.ws_projects[tmp_path / "a"] = project
    _stub_routing(monkeypatch, ws_context, project)

    aborted = asyncio.Event()

    @contextlib.asynccontextmanager
    async def _never_finishing_dispatch(
        **kwargs: typing.Any,
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        inner = _ScriptedProxyCtx(
            responses=[
                RunActionResponse(result_by_format={}, return_code=0, status="streamed")
            ],
            hold_open=True,
        )
        try:
            yield inner
        finally:
            aborted.set()

    monkeypatch.setattr(
        partial_results_service, "run_with_partial_results", _never_finishing_dispatch
    )

    with pytest.raises(RuntimeError, match="sink blew up"):
        async with partial_results_service.run_action_with_partial_results(
            action_name="lint",
            project_path=str(tmp_path / "a"),
            params={},
            partial_result_token="tok",
            run_trigger=RunActionTrigger("system"),
            dev_env=DevEnv("ide"),
            ws_context=ws_context,
            origin=None,
            result_formats=["json"],
        ) as _stream:
            raise RuntimeError("sink blew up")
    assert aborted.is_set()


async def test_api_sink_acknowledges_only_after_notify_and_drain() -> None:
    """The direct API sink pattern acknowledges a fence only after the
    notify + writer-drain handoff resumes, never at dequeue time."""
    stream: CompletionStream[dict[str, typing.Any]] = CompletionStream()
    fence = _make_fence()
    stream.append({"marker": "v"})
    stream.forward(fence)
    stream.end()

    order: list[str] = []
    seen: list[dict[str, typing.Any]] = []
    async for value in stream:
        seen.append(value)
        assert not fence.completion.done()
        order.append("notify")
        await asyncio.sleep(0)
        order.append("drain")
    assert seen == [{"marker": "v"}]
    assert fence.completion.done()
    assert fence.completion.result() is None
    assert order == ["notify", "drain"]


async def test_matrix_owner_abort_joins_all_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner cancellation of a matrix run cancels and joins every variant."""
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )
    started: list[str] = []
    finished: list[str] = []
    gate = asyncio.Event()
    both_started = asyncio.Event()

    @contextlib.asynccontextmanager
    async def _blocking_dispatch(
        *, interpreter: typing.Any = None, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[_ScriptedProxyCtx, None]:
        assert interpreter is not None
        canonical: str = interpreter.canonical
        started.append(canonical)
        if len(started) >= 2 and not both_started.is_set():
            both_started.set()
        try:
            inner = _ScriptedProxyCtx(
                responses=[
                    RunActionResponse(
                        result_by_format={}, return_code=0, status="streamed"
                    )
                ],
                hold_open=True,
            )
            async with inner:
                await gate.wait()
                yield inner
        finally:
            finished.append(canonical)

    monkeypatch.setattr(
        matrix_streaming.proxy_utils, "run_with_partial_results", _blocking_dispatch
    )

    async def _on_partial(_canonical: str, _value: dict[str, typing.Any]) -> None:
        return None

    async def _on_prefix(received: PrefixFence) -> None:
        if not received.completion.done():
            received.completion.set_result(None)

    task = asyncio.ensure_future(
        matrix_streaming.run_matrix_with_partial_results(
            project=_make_matrix_project(),
            action=action,
            action_name="test_action",
            params={},
            result_formats=[RunResultFormat("json")],
            partial_result_token="tok",
            run_trigger=RunActionTrigger("system"),
            dev_env=DevEnv("ide"),
            ws_context=typing.cast(typing.Any, None),
            merge_results=False,
            on_partial=_on_partial,
            on_prefix=_on_prefix,
            origin=None,
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=5)
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert sorted(finished) == sorted(started)
