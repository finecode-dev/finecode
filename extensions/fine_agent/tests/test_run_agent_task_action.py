"""The parts of the agent-task result that no backend is needed to exercise.

`AgentRunUsage` exists to keep "the backend did not say" distinct from "the
backend said zero", so most of what is worth testing here is what happens to
the unreported fields.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri
from finecode_extension_runner._converter import converter
from finecode_extension_runner import partial_result_sender
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session

from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    NativeHistoryFile,
    NativeHistoryFormat,
    NativeHistoryRole,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
    AgentSessionRef,
    RunAgentTaskAction,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)


def test_combining_leaves_a_figure_neither_side_reported_unreported() -> None:
    """Summing to `0` would report a run as having read no input, when the
    truth is that nobody counted."""
    combined = AgentRunUsage(input_tokens=10).combine(AgentRunUsage(input_tokens=5))

    assert combined.input_tokens == 15
    assert combined.output_tokens is None
    assert combined.total_tokens is None
    assert combined.approx_cost_usd is None


def test_combining_keeps_what_one_side_reported() -> None:
    combined = AgentRunUsage(input_tokens=10).combine(AgentRunUsage(output_tokens=5))

    assert combined.input_tokens == 10
    assert combined.output_tokens == 5


def test_combining_two_models_names_neither() -> None:
    """A cost that spans two models cannot be attributed to one of them, and
    naming the last one seen would misattribute the whole figure."""
    combined = AgentRunUsage(
        approx_cost_usd=0.01, provider="deepseek", model="a"
    ).combine(AgentRunUsage(approx_cost_usd=0.02, provider="deepseek", model="b"))

    assert combined.approx_cost_usd == 0.03
    assert combined.provider == "deepseek"
    assert combined.model is None


def test_usage_text_reports_the_halves_when_there_is_no_total() -> None:
    """Adding them into a total the backend never reported would invent the one
    figure a reader is most likely to quote."""
    text = AgentRunUsage(input_tokens=3200, output_tokens=891).to_text()

    assert text == "in 3,200 / out 891"


def test_usage_text_prefers_the_reported_total() -> None:
    text = AgentRunUsage(
        input_tokens=3200,
        output_tokens=891,
        total_tokens=9000,
        approx_cost_usd=0.014,
        provider="deepseek",
        model="deepseek-v4-flash",
    ).to_text()

    assert text == "9,000 tokens · ~$0.0140 · deepseek/deepseek-v4-flash"


def test_usage_text_is_empty_when_nothing_was_reported() -> None:
    assert AgentRunUsage().to_text() == ""


def test_result_text_appends_what_the_run_cost() -> None:
    result = RunAgentTaskRunResult(
        status=AgentRunStatus.SETTLED,
        output="the answer",
        usage=AgentRunUsage(total_tokens=9000, approx_cost_usd=0.014),
        duration_sec=42.06,
    )

    assert result.to_text() == "the answer\n\n9,000 tokens · ~$0.0140 · 42.1s"


def test_result_text_reports_the_cost_of_a_failed_run_too() -> None:
    result = RunAgentTaskRunResult(
        status=AgentRunStatus.FAILED,
        error="402: Insufficient Balance",
        usage=AgentRunUsage(total_tokens=120),
        duration_sec=3.0,
    )

    assert result.to_text() == (
        "[failed] 402: Insufficient Balance\n\n120 tokens · 3.0s"
    )


def test_result_text_has_no_footer_when_nothing_is_known() -> None:
    result = RunAgentTaskRunResult(status=AgentRunStatus.SETTLED, output="the answer")

    assert result.to_text() == "the answer"


def test_running_result_text_is_only_its_output() -> None:
    result = RunAgentTaskRunResult(
        status=AgentRunStatus.RUNNING,
        output="current answer",
        usage=AgentRunUsage(total_tokens=10),
        duration_sec=3.0,
    )

    assert result.to_text() == "current answer"


def test_running_result_has_error_return_code() -> None:
    assert (
        RunAgentTaskRunResult(status=AgentRunStatus.RUNNING).return_code
        is code_action.RunReturnCode.ERROR
    )


def test_merging_carries_the_accounting_fields() -> None:
    """`update` copies field by field, so a field added to the result and not
    to it is dropped on merge -- silently, and only in the merged path."""
    target = RunAgentTaskRunResult()
    target.update(
        RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED,
            output="the answer",
            turns=3,
            usage=AgentRunUsage(total_tokens=9000),
            duration_sec=42.0,
        )
    )

    assert target.turns == 3
    assert target.usage == AgentRunUsage(total_tokens=9000)
    assert target.duration_sec == 42.0


def _sample_observation() -> AgentRunObservation:
    return AgentRunObservation(
        backend=AgentBackend.PI,
        backend_version="1.3.0",
        session_id="sess-1",
        history_root=None,
        history_status=HistoryDiscoveryStatus.AVAILABLE,
        files=[],
        related_status=RelatedHistoryStatus.UNAVAILABLE,
        usage_provenance=UsageProvenance(
            token_source=UsageSource.PI_STATS,
            token_scope=UsageScope.SESSION,
            cost_source=UsageSource.UNKNOWN,
            cost_scope=UsageScope.UNKNOWN,
        ),
        gaps=[],
    )


def _session_ref(backend: AgentBackend) -> AgentSessionRef:
    return AgentSessionRef(
        backend=backend,
        session_id="11111111-2222-3333-8444-555555555555",
        project_dir=ResourceUri("file:///ws/proj"),
    )


def test_payload_defaults_to_no_capture() -> None:
    assert RunAgentTaskRunPayload(prompt="do it").capture_history is False


def test_payload_defaults_to_no_continuation() -> None:
    """Old payload constructions keep working and start a fresh session."""
    assert RunAgentTaskRunPayload(prompt="do it").continue_session is None
    assert RunAgentTaskRunPayload("do it", None, None).continue_session is None


def test_payload_and_result_round_trip_every_backend() -> None:
    """References for each backend survive the WM converter unchanged."""
    for backend in (
        AgentBackend.PI,
        AgentBackend.CLAUDE_CODE,
        AgentBackend.CODEX,
    ):
        ref = _session_ref(backend)
        payload = RunAgentTaskRunPayload(prompt="x", continue_session=ref)
        revived_payload = converter.structure(
            converter.unstructure(payload), RunAgentTaskRunPayload
        )
        assert revived_payload.continue_session == ref

        result = RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED, output="hi", session=ref
        )
        revived = converter.structure(
            converter.unstructure(result), RunAgentTaskRunResult
        )
        assert isinstance(revived.session, AgentSessionRef)
        assert revived.session == ref
        assert revived.session.backend is backend


def test_result_defaults_to_no_observation() -> None:
    assert RunAgentTaskRunResult().observation is None


def test_merging_carries_the_session() -> None:
    """A merged result keeps the reported session, independently of capture."""
    target = RunAgentTaskRunResult()
    ref = _session_ref(AgentBackend.CODEX)
    target.update(RunAgentTaskRunResult(status=AgentRunStatus.SETTLED, session=ref))

    assert target.session == ref


def test_merging_clears_a_stale_session() -> None:
    """Last-writer-wins: a newer result without a session clears the old one."""
    target = RunAgentTaskRunResult(
        status=AgentRunStatus.SETTLED, session=_session_ref(AgentBackend.PI)
    )
    target.update(RunAgentTaskRunResult(status=AgentRunStatus.SETTLED))

    assert target.session is None


def test_merging_carries_the_observation() -> None:
    target = RunAgentTaskRunResult()
    observation = _sample_observation()
    target.update(
        RunAgentTaskRunResult(status=AgentRunStatus.SETTLED, observation=observation)
    )

    assert target.observation == observation


def test_terminal_partial_replaces_every_running_snapshot_field() -> None:
    running = RunAgentTaskRunResult(
        status=AgentRunStatus.RUNNING,
        output="working",
    )
    terminal = RunAgentTaskRunResult(
        status=AgentRunStatus.FAILED,
        output="final error output",
        structured_output={"answer": "no"},
        turns=4,
        usage=AgentRunUsage(total_tokens=24),
        duration_sec=7.5,
        error="backend failed",
        observation=_sample_observation(),
        session=_session_ref(AgentBackend.PI),
    )

    running.update(terminal)

    assert running == terminal


async def _running_only_handler(
    payload: RunAgentTaskRunPayload,
    run_context: code_action.RunActionContext[RunAgentTaskRunPayload],
) -> AsyncIterator[RunAgentTaskRunResult]:
    _ = payload, run_context
    yield RunAgentTaskRunResult(
        status=AgentRunStatus.RUNNING,
        output="snapshot",
    )


def _discard_partial(
    _token: int | str,
    _value: code_action.RunActionResult,
    _formats: list[str] | None,
) -> None:
    return None


async def test_running_only_run_ends_with_error_without_a_terminal_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_action_service,
        "partial_result_sender",
        partial_result_sender.PartialResultSender(
            sender=_discard_partial,
            wait_time_ms=300,
        ),
    )
    action_source = (
        f"{RunAgentTaskAction.__module__}.{RunAgentTaskAction.__qualname__}"
    )
    handler_source = (
        f"{_running_only_handler.__module__}.{_running_only_handler.__qualname__}"
    )
    actions = {
        RunAgentTaskAction.__name__: {
            "source": action_source,
            "handlers": [{"name": "synthetic", "source": handler_source}],
        }
    }
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        token_result = await session.run_action(
            RunAgentTaskAction.__name__,
            RunAgentTaskRunPayload(prompt="test"),
            partial_result_token="tok",
        )
        plain_result = await session.run_action(
            RunAgentTaskAction.__name__, RunAgentTaskRunPayload(prompt="test")
        )

    assert token_result == plain_result
    assert isinstance(token_result, RunAgentTaskRunResult)
    assert token_result.status is AgentRunStatus.RUNNING
    assert token_result.return_code is code_action.RunReturnCode.ERROR


def test_result_converter_round_trip_preserves_observation_types() -> None:
    observation = AgentRunObservation(
        backend=AgentBackend.PI,
        backend_version="1.3.0",
        session_id="sess-1",
        history_root=ResourceUri("file:///tmp/hist"),
        history_status=HistoryDiscoveryStatus.AVAILABLE,
        files=[
            NativeHistoryFile(
                role=NativeHistoryRole.MAIN,
                source_uri=ResourceUri("file:///tmp/hist/main.jsonl"),
                native_relative_path="main.jsonl",
                native_session_id="sess-1",
                format=NativeHistoryFormat.PI,
            )
        ],
        related_status=RelatedHistoryStatus.UNAVAILABLE,
        usage_provenance=UsageProvenance(
            token_source=UsageSource.PI_STATS,
            token_scope=UsageScope.SESSION,
            cost_source=UsageSource.UNKNOWN,
            cost_scope=UsageScope.UNKNOWN,
        ),
        gaps=[],
    )
    result = RunAgentTaskRunResult(
        status=AgentRunStatus.SETTLED,
        output="hi",
        observation=observation,
    )

    revived = converter.structure(converter.unstructure(result), RunAgentTaskRunResult)

    assert isinstance(revived.observation, AgentRunObservation)
    assert revived.observation.backend is AgentBackend.PI
    assert isinstance(revived.observation.files[0], NativeHistoryFile)
    assert revived.observation.files[0].role is NativeHistoryRole.MAIN
    assert revived.observation.files[0].format is NativeHistoryFormat.PI
    assert revived.observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert revived.observation.usage_provenance.token_source is UsageSource.PI_STATS


def test_payload_converter_round_trip_preserves_capture_flag() -> None:
    revived = converter.structure(
        converter.unstructure(RunAgentTaskRunPayload(prompt="x", capture_history=True)),
        RunAgentTaskRunPayload,
    )

    assert revived.capture_history is True
