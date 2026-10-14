"""Helpers for actions whose job is to run an agent against a prompt template.

A task action has the same three steps every time: render a template from its
typed payload, ask `run_agent_task` for a structured answer, and structure that
answer into the task's report type. Only the template, the report type and the
profile name differ, so the steps live here rather than being rewritten per
task.
"""

from __future__ import annotations

import dataclasses
import re
import typing

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    idataclasscodec,
    iprojectactionrunner,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskAction,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)

__all__ = [
    "StructuredTaskOutcome",
    "TemplateError",
    "render_template",
    "run_structured_task",
    "slots_from_payload",
]

T = typing.TypeVar("T")

_SLOT_RE = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


class TemplateError(ValueError):
    """A template and its slots do not match.

    Both failure directions matter: an unfilled slot would reach the model as
    literal `{{name}}`, and an unused slot means the caller supplied something
    the template ignores, which is a silently dropped input rather than a
    harmless extra.
    """


def render_template(template: str, slots: typing.Mapping[str, str]) -> str:
    """Substitute every ``{{name}}`` in *template* with its slot value.

    Raises one :class:`TemplateError` naming both the unfilled and the unused
    slots, so a mismatch is fixed in one edit instead of one slot per run. The
    template has no literal ``{{``, so there is no escaping syntax.
    """
    used = set(_SLOT_RE.findall(template))
    provided = set(slots)
    unfilled = used - provided
    unused = provided - used
    if unfilled or unused:
        parts: list[str] = []
        if unfilled:
            parts.append(f"unfilled slots: {', '.join(sorted(unfilled))}")
        if unused:
            parts.append(f"unused slots: {', '.join(sorted(unused))}")
        raise TemplateError("; ".join(parts))
    return _SLOT_RE.sub(lambda match: slots[match.group(1)], template)


def slots_from_payload(
    payload: code_action.RunActionPayload,
) -> dict[str, str]:
    """One template slot per payload field.

    Only ``str`` and ``ResourceUri`` fields become slots: a template is text,
    and turning an arbitrary value into text is a decision the task should make
    explicitly rather than the renderer guessing.
    """
    try:
        hints = typing.get_type_hints(type(payload))
    except Exception:
        hints = {}

    slots: dict[str, str] = {}
    for field in dataclasses.fields(payload):
        value = getattr(payload, field.name)
        field_type = hints.get(field.name, type(None))
        if field_type is ResourceUri:
            slots[field.name] = str(resource_uri_to_path(value))
        elif field_type is str:
            slots[field.name] = value
        else:
            raise TemplateError(
                f"payload field {field.name!r} has type {field_type!r}; "
                "only str and ResourceUri fields can be template slots"
            )
    return slots


@dataclasses.dataclass
class StructuredTaskOutcome(typing.Generic[T]):
    """What a structured task action has to report, before it is mapped.

    `agent` is always present so a caller can surface the run's status, raw
    output and usage on the failure paths too. `report` is populated only when
    the agent settled and its output fit `report_type`; `error` says why it did
    not, and is `None` exactly when `report` is populated.
    """

    report: T | None
    agent: RunAgentTaskRunResult
    error: str | None


async def run_structured_task(
    *,
    action_runner: iprojectactionrunner.IProjectActionRunner,
    codec: idataclasscodec.IDataclassCodec,
    meta: code_action.RunActionMeta,
    prompt: str,
    profile: str | None,
    report_type: type[T],
) -> StructuredTaskOutcome[T]:
    """Run `run_agent_task` for *prompt* and structure its answer into *report_type*.

    A backend failure short-circuits before structuring: the agent's own error
    is more specific than "the value did not fit", and `structured_output` is
    absent precisely when the backend already failed. Exceptions from
    `run_action` propagate -- an action that could not be dispatched at all is
    not a report whose type did not match.
    """
    schema = codec.json_schema(report_type)
    agent = await action_runner.run_action(
        action_type=iprojectactionrunner.ActionRef.from_type(RunAgentTaskAction),
        payload=RunAgentTaskRunPayload(
            prompt=prompt,
            profile=profile,
            output_schema=schema,
        ),
        meta=meta,
    )
    if agent.status is not AgentRunStatus.SETTLED:
        return StructuredTaskOutcome(report=None, agent=agent, error=agent.error)

    try:
        report = codec.structure(agent.structured_output, report_type)
    except idataclasscodec.StructureError as error:
        return StructuredTaskOutcome(
            report=None,
            agent=agent,
            error=(
                f"agent output does not match {report_type.__name__}: {error.message}"
            ),
        )
    return StructuredTaskOutcome(report=report, agent=agent, error=None)
