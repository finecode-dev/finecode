"""Router unit tests with controlled iterator doubles."""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import typing

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import ResourceUri

from fine_agent import backend_support
from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)
from fine_agent.route_agent_task_handler import (
    AgentTaskRoute,
    RouteAgentTaskHandler,
    RouteAgentTaskHandlerConfig,
)
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
    AgentSessionRef,
    RunAgentTaskRunContext,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)

_CANONICAL = {
    AgentBackend.PI: "fine_agent_pi.run_pi_agent_task_action.RunPiAgentTaskAction",
    AgentBackend.CLAUDE_CODE: (
        "fine_agent_claude_code.run_claude_code_agent_task_action."
        "RunClaudeCodeAgentTaskAction"
    ),
    AgentBackend.CODEX: (
        "fine_agent_codex.run_codex_agent_task_action.RunCodexAgentTaskAction"
    ),
}


class _RecordingProgress:
    def __init__(self, title: str) -> None:
        self.title = title
        self.reports: list[tuple[str | None, int | None]] = []

    async def __aenter__(self) -> _RecordingProgress:
        return self

    async def __aexit__(self, *exc: typing.Any) -> bool:
        return False

    async def report(
        self, message: str | None = None, percentage: int | None = None
    ) -> None:
        self.reports.append((message, percentage))


class _StubProjectInfo:
    def __init__(self, project_dir: pathlib.Path = pathlib.Path("/ws/proj")) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self._project_dir


class _StubContext:
    def __init__(self, meta: code_action.RunActionMeta) -> None:
        self.meta = meta
        self.titles: list[str] = []
        self._progress = _RecordingProgress("stub")

    def progress(self, title: str, **kwargs: typing.Any) -> _RecordingProgress:
        _ = kwargs
        self.titles.append(title)
        prog = _RecordingProgress(title)
        self._progress = prog
        return prog


def _meta() -> code_action.RunActionMeta:
    return code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.USER,
        dev_env=code_action.DevEnv.CI,
        orchestration_depth=3,
    )


def _config() -> RouteAgentTaskHandlerConfig:
    return RouteAgentTaskHandlerConfig(
        default_backend=AgentBackend.PI,
        profiles={
            "implement_plan": AgentTaskRoute(
                backend=AgentBackend.CLAUDE_CODE, profile="implementation"
            ),
            "review": AgentTaskRoute(backend=AgentBackend.PI, profile="review"),
            "quick": AgentTaskRoute(backend=AgentBackend.PI, profile=None),
        },
    )


class _FakeRunner:
    def __init__(
        self,
        results: list[RunAgentTaskRunResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[dict[str, typing.Any]] = []

    async def _gen(
        self, results: list[RunAgentTaskRunResult]
    ) -> typing.AsyncIterator[RunAgentTaskRunResult]:
        for item in results:
            yield item

    def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: typing.Any,
        caller_kwargs: typing.Any = None,
        *,
        progress: typing.Any = None,
    ) -> typing.AsyncIterator[RunAgentTaskRunResult]:
        self.calls.append(
            {
                "ref": action_type,
                "payload": payload,
                "meta": meta,
                "progress": progress,
            }
        )
        if self.error is not None:
            raise self.error
        return self._gen(self.results)


def _full_result(status: AgentRunStatus) -> RunAgentTaskRunResult:
    return RunAgentTaskRunResult(
        status=status,
        output="text",
        structured_output={"answer": 42, "extra": "kept"},
        turns=3,
        usage=AgentRunUsage(
            input_tokens=10,
            output_tokens=20,
            total_tokens=35,
            approx_cost_usd=0.001,
            provider="p",
            model="m",
        ),
        duration_sec=1.5,
        error=None if status is AgentRunStatus.SETTLED else "oops",
        session=_session_ref(),
        observation=AgentRunObservation(
            backend=AgentBackend.PI,
            backend_version="v1",
            session_id="s",
            history_root=None,
            history_status=HistoryDiscoveryStatus.AVAILABLE,
            files=[],
            related_status=RelatedHistoryStatus.SCANNED,
            usage_provenance=UsageProvenance(
                token_source=UsageSource.UNKNOWN,
                token_scope=UsageScope.UNKNOWN,
                cost_source=UsageSource.UNKNOWN,
                cost_scope=UsageScope.UNKNOWN,
            ),
            gaps=[],
        ),
    )


_SESSION_ID = "11111111-2222-3333-8444-555555555555"
_PROJECT_URI = ResourceUri("file:///ws/proj")


def _session_ref(
    session_id: str = _SESSION_ID,
    backend: AgentBackend = AgentBackend.PI,
    project_dir: ResourceUri = _PROJECT_URI,
) -> AgentSessionRef:
    return AgentSessionRef(
        backend=backend, session_id=session_id, project_dir=project_dir
    )


async def test_default_uses_default_backend_with_top_level() -> None:
    """Missing payload profile selects the default backend's top-level settings."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    payload = RunAgentTaskRunPayload(
        prompt="do", profile=None, output_schema={"type": "object"}
    )
    result = await handler.run(payload, typing.cast(RunAgentTaskRunContext, ctx))
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["ref"].source == _CANONICAL[AgentBackend.PI]
    assert call["ref"].action_type is None
    assert call["ref"].result_type is RunAgentTaskRunResult
    assert call["payload"].profile is None
    assert call["meta"] is ctx.meta
    assert call["progress"] is ctx._progress
    assert payload.profile is None
    assert result.status is AgentRunStatus.SETTLED
    assert ctx.titles == ["Agent task (pi)"]


async def test_two_roles_share_backend_with_different_bundles() -> None:
    """Two roles can name different bundles on one backend."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    await handler.run(
        RunAgentTaskRunPayload(prompt="a", profile="review"),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    await handler.run(
        RunAgentTaskRunPayload(prompt="b", profile="quick"),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert runner.calls[0]["payload"].profile == "review"
    assert runner.calls[0]["ref"].source == _CANONICAL[AgentBackend.PI]
    assert runner.calls[1]["payload"].profile is None
    assert runner.calls[1]["ref"].source == _CANONICAL[AgentBackend.PI]


async def test_unknown_role_fails_without_delegation() -> None:
    """Unknown roles fail fast with the shared wording and no child call."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="x", profile="nope", capture_history=True),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.status is AgentRunStatus.FAILED
    assert result.duration_sec == 0.0
    assert result.error == backend_support.unknown_profile_error(
        "nope", _config().profiles
    )
    assert "implement_plan" in (result.error or "")
    assert runner.calls == []
    assert ctx.titles == []


async def test_missing_target_names_backend_and_source() -> None:
    """An unconfigured backend action fails naming both parts."""
    runner = _FakeRunner(error=iprojectactionrunner.ActionRunFailed("no such action"))
    cfg = RouteAgentTaskHandlerConfig(default_backend=AgentBackend.CODEX, profiles={})
    handler = RouteAgentTaskHandler(
        cfg,
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="x"),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.status is AgentRunStatus.FAILED
    assert "codex" in (result.error or "")
    assert _CANONICAL[AgentBackend.CODEX] in (result.error or "")
    assert "no such action" in (result.error or "")
    assert len(runner.calls) == 1
    assert runner.calls[0]["ref"].action_type is None


async def test_continuation_delegates_with_its_reference_intact() -> None:
    """A valid reference reaches the leaf with its fields and role mapping."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    ref = _session_ref()
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="again", profile="review", continue_session=ref),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.status is AgentRunStatus.SETTLED
    assert len(runner.calls) == 1
    assert runner.calls[0]["payload"].continue_session == ref
    assert runner.calls[0]["payload"].profile == "review"
    assert result.session == _session_ref()


async def test_continuation_rejects_a_partial_id_without_delegation() -> None:
    """A name or truncated ID fails before any backend or project check."""
    for bad_id in ("", "thread-1", "11111111-2222-3333-8444-555555555555"[:8]):
        runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
        handler = RouteAgentTaskHandler(
            _config(),
            typing.cast(typing.Any, runner),
            typing.cast(typing.Any, _StubProjectInfo()),
        )
        ctx = _StubContext(_meta())
        result = await handler.run(
            RunAgentTaskRunPayload(
                prompt="x",
                profile="unknown-role",
                continue_session=_session_ref(session_id=bad_id),
            ),
            typing.cast(RunAgentTaskRunContext, ctx),
        )
        assert result.status is AgentRunStatus.FAILED
        assert "session ID" in (result.error or "")
        assert runner.calls == []


async def test_continuation_backend_mismatch_beats_project_mismatch() -> None:
    """The backend check runs before the project check."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    ref = _session_ref(
        backend=AgentBackend.CODEX, project_dir=ResourceUri("file:///elsewhere")
    )
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="x", continue_session=ref),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.status is AgentRunStatus.FAILED
    assert "codex" in (result.error or "")
    assert runner.calls == []


async def test_continuation_project_mismatch_beats_unknown_role() -> None:
    """An unknown role is reported only after project agreement."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    ref = _session_ref(project_dir=ResourceUri("file:///elsewhere"))
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="x", profile="nope", continue_session=ref),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.status is AgentRunStatus.FAILED
    assert "elsewhere" in (result.error or "")
    assert runner.calls == []


async def test_continuation_unknown_role_fails_without_delegation() -> None:
    """An unresolved role selects no backend, so nothing is delegated."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    result = await handler.run(
        RunAgentTaskRunPayload(
            prompt="x", profile="nope", continue_session=_session_ref()
        ),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.status is AgentRunStatus.FAILED
    assert result.error == backend_support.unknown_profile_error(
        "nope", _config().profiles
    )
    assert runner.calls == []


async def test_continuation_yields_to_pending_cancellation() -> None:
    """A caller that already went away stops the run before any check."""
    runner = _FakeRunner(results=[_full_result(AgentRunStatus.SETTLED)])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    task = asyncio.create_task(
        handler.run(
            RunAgentTaskRunPayload(prompt="x", continue_session=_session_ref()),
            typing.cast(RunAgentTaskRunContext, ctx),
        )
    )
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.calls == []


async def test_later_missing_session_clears_an_earlier_reference() -> None:
    """Merging is last-writer-wins: a missing reference clears the old one."""
    first = _full_result(AgentRunStatus.SETTLED)
    second = dataclasses.replace(
        _full_result(AgentRunStatus.SETTLED), output="second", session=None
    )
    runner = _FakeRunner(results=[first, second])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="x"),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.output == "second"
    assert result.session is None


async def test_empty_stream_is_an_explained_failure() -> None:
    """No yielded result is a router failure, not a silent success."""
    runner = _FakeRunner(results=[])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="x"),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.status is AgentRunStatus.FAILED
    assert "no result" in (result.error or "").lower()
    assert (result.duration_sec or 0) >= 0


async def test_result_replacement_across_partials() -> None:
    """Later partials replace earlier ones; usage is never summed."""
    first = _full_result(AgentRunStatus.SETTLED)
    first = dataclasses.replace(
        first,
        output="first",
        usage=AgentRunUsage(input_tokens=1, output_tokens=1),
    )
    second = _full_result(AgentRunStatus.SETTLED)
    second = dataclasses.replace(
        second,
        output="second",
        usage=AgentRunUsage(input_tokens=100, output_tokens=200),
    )
    runner = _FakeRunner(results=[first, second])
    handler = RouteAgentTaskHandler(
        _config(),
        typing.cast(typing.Any, runner),
        typing.cast(typing.Any, _StubProjectInfo()),
    )
    ctx = _StubContext(_meta())
    result = await handler.run(
        RunAgentTaskRunPayload(prompt="x"),
        typing.cast(RunAgentTaskRunContext, ctx),
    )
    assert result.output == "second"
    assert result.usage is not None
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 200


async def test_all_statuses_and_fields_survive() -> None:
    """Every status and every result field passes through unchanged."""
    for status in (
        AgentRunStatus.SETTLED,
        AgentRunStatus.FAILED,
        AgentRunStatus.ABORTED,
        AgentRunStatus.REFUSED_INTERACTION,
    ):
        expected = _full_result(status)
        runner = _FakeRunner(results=[expected])
        handler = RouteAgentTaskHandler(
            _config(),
            typing.cast(typing.Any, runner),
            typing.cast(typing.Any, _StubProjectInfo()),
        )
        ctx = _StubContext(_meta())
        payload = RunAgentTaskRunPayload(
            prompt="p",
            profile="implement_plan",
            output_schema={"type": "object"},
            capture_history=True,
        )
        result = await handler.run(payload, typing.cast(RunAgentTaskRunContext, ctx))
        assert result.status is status
        assert result.output == "text"
        assert result.structured_output == {"answer": 42, "extra": "kept"}
        assert result.turns == 3
        assert result.usage is not None
        assert result.usage.total_tokens == 35
        assert result.duration_sec == 1.5
        assert result.session == _session_ref()
        assert result.observation is not None
        assert result.observation.backend is AgentBackend.PI
        call = runner.calls[0]
        assert call["payload"].prompt == "p"
        assert call["payload"].output_schema == {"type": "object"}
        assert call["payload"].capture_history is True
        assert call["payload"].profile == "implementation"
        assert payload.profile == "implement_plan"
