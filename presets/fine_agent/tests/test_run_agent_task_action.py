"""The parts of the agent-task result that no backend is needed to exercise.

`AgentRunUsage` exists to keep "the backend did not say" distinct from "the
backend said zero", so most of what is worth testing here is what happens to
the unreported fields.
"""

from __future__ import annotations

from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
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
