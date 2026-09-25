"""Tests for the shared task-action helpers.

These helpers are the contract every task action is built from: a template that
refuses to render half-filled, a payload-to-slots mapping that cannot silently
drop a field, and a run-and-structure step that keeps the agent's own failure
distinct from a type mismatch.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_runner.impls.dataclass_codec import DataclassCodec

from fine_agent import task_support
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)


@dataclasses.dataclass
class _SlotsPayload(code_action.RunActionPayload):
    plan_path: ResourceUri
    label: str


@dataclasses.dataclass
class _UnslottablePayload(code_action.RunActionPayload):
    count: int


@dataclasses.dataclass
class _Report:
    answer: str


def _meta() -> code_action.RunActionMeta:
    return code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
    )


class _StubRunner:
    def __init__(self, result: RunAgentTaskRunResult) -> None:
        self.result = result
        self.calls: list[
            tuple[str, RunAgentTaskRunPayload, code_action.RunActionMeta]
        ] = []

    async def run_action(
        self,
        action_type,
        payload: RunAgentTaskRunPayload,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> RunAgentTaskRunResult:
        self.calls.append((action_type.source, payload, meta))
        return self.result


def test_render_template_substitutes_every_slot() -> None:
    assert (
        task_support.render_template(
            "plan {{plan_path}} ({{kind}})", {"plan_path": "a.md", "kind": "md"}
        )
        == "plan a.md (md)"
    )


def test_render_template_names_every_unfilled_slot() -> None:
    """An unfilled slot would reach the model as a literal `{{name}}`."""
    with pytest.raises(task_support.TemplateError) as exc_info:
        task_support.render_template("{{one}} and {{two}}", {"one": "1"})

    message = str(exc_info.value)
    assert "two" in message
    assert "one" not in message.split("unused")[0]


def test_render_template_names_every_unused_slot() -> None:
    """A provided-but-unused slot is a dropped input, not a harmless extra."""
    with pytest.raises(task_support.TemplateError) as exc_info:
        task_support.render_template("{{one}}", {"one": "1", "extra": "2"})

    message = str(exc_info.value)
    assert "extra" in message
    assert "unfilled" not in message


def test_render_template_names_both_directions_in_one_error() -> None:
    """Both sides are fixed in one edit instead of one run per mismatch."""
    with pytest.raises(task_support.TemplateError) as exc_info:
        task_support.render_template("{{one}} {{missing}}", {"one": "1", "extra": "2"})

    message = str(exc_info.value)
    assert "missing" in message
    assert "extra" in message


def test_slots_from_payload_gives_one_slot_per_field() -> None:
    payload = _SlotsPayload(
        plan_path=path_to_resource_uri(pathlib.Path("/tmp/plan.md")), label="x"
    )

    assert task_support.slots_from_payload(payload) == {
        "plan_path": "/tmp/plan.md",
        "label": "x",
    }


def test_slots_from_payload_rejects_a_non_text_field() -> None:
    """Only str and ResourceUri can become text; anything else is a decision
    the task must make explicitly."""
    with pytest.raises(task_support.TemplateError):
        task_support.slots_from_payload(_UnslottablePayload(count=1))


async def test_run_structured_task_structures_a_settled_answer() -> None:
    runner = _StubRunner(
        RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED,
            output="the raw text",
            structured_output={"answer": "42"},
        )
    )
    codec = DataclassCodec()

    outcome = await task_support.run_structured_task(
        action_runner=runner,
        codec=codec,
        meta=_meta(),
        prompt="do it",
        profile="my_task",
        report_type=_Report,
    )

    assert outcome.report == _Report(answer="42")
    assert outcome.error is None
    _, payload, _ = runner.calls[0]
    assert payload.prompt == "do it"
    assert payload.profile == "my_task"
    assert payload.output_schema == codec.json_schema(_Report)


async def test_run_structured_task_reports_a_type_mismatch() -> None:
    """JSON that parses but does not fit the report type is a task failure, not
    a backend failure, and the message must say what did not fit."""
    runner = _StubRunner(
        RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED,
            output="the raw text",
            structured_output={"wrong": True},
        )
    )

    outcome = await task_support.run_structured_task(
        action_runner=runner,
        codec=DataclassCodec(),
        meta=_meta(),
        prompt="do it",
        profile=None,
        report_type=_Report,
    )

    assert outcome.report is None
    assert outcome.error is not None
    assert outcome.error.startswith("agent output does not match _Report:")
    assert "extra fields found" in outcome.error


async def test_run_structured_task_keeps_the_backend_error() -> None:
    """A backend failure is more specific than "the value did not fit", and the
    run's agent result is what carries the raw output."""
    runner = _StubRunner(
        RunAgentTaskRunResult(
            status=AgentRunStatus.FAILED,
            output="partial",
            error="no fenced json block",
        )
    )

    outcome = await task_support.run_structured_task(
        action_runner=runner,
        codec=DataclassCodec(),
        meta=_meta(),
        prompt="do it",
        profile=None,
        report_type=_Report,
    )

    assert outcome.report is None
    assert outcome.error == "no fenced json block"
    assert outcome.agent.output == "partial"
