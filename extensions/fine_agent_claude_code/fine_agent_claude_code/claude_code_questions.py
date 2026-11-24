"""Question mapping for Claude Code permission prompts.

Pure mapping between claude's ``can_use_tool`` wire shapes and the
``IUserPrompt`` asks put to the run's originating person. The handler owns
transport, ordering and run endings; this module owns texts, parsing and the
answer encoding recorded in the smoke.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import typing

from fine_agent.question_support import (
    QUESTION_DECLINED_REPLY,
    QUESTION_GRACE_SEC,
    question_deadline_sec,
)
from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationOutcome,
    IUserPrompt,
)

from fine_agent_claude_code.claude_code_control import (
    PermissionAllow,
    PermissionDeny,
)

__all__ = [
    "PERMISSION_DENIED_BY_PERSON_REPLY",
    "AskEntry",
    "AskOption",
    "GroupEnd",
    "GroupOutcome",
    "PermissionEnd",
    "ask_group",
    "ask_permission",
    "disallowed_tool_names",
    "group_reply",
    "permission_question",
]

PERMISSION_DENIED_BY_PERSON_REPLY: typing.Final[str] = (
    "The person denied permission for this tool call. "
    "This is a decision, not an error: continue without it, or choose another approach."
)

OTHER_LABEL: typing.Final[str] = "Other (type an answer)"
OTHER_DESCRIPTION: typing.Final[str] = "Type your own answer next"

_ALLOW_ONCE: typing.Final[str] = "Allow once"
_DENY: typing.Final[str] = "Deny"

_PERMISSION_DESCRIPTIONS: typing.Final[dict[str, str]] = {
    _ALLOW_ONCE: "Run this one call; a later call asks again",
    _DENY: "Refuse; Claude is told and continues",
}


@dataclasses.dataclass(frozen=True)
class AskOption:
    label: str
    description: str | None


@dataclasses.dataclass(frozen=True)
class AskEntry:
    question: str
    header: str | None
    options: tuple[AskOption, ...]
    multi_select: bool


class GroupEnd(enum.StrEnum):
    ANSWERED = "answered"
    DECLINED = "declined"
    UNAVAILABLE = "unavailable"


@dataclasses.dataclass(frozen=True)
class GroupOutcome:
    end: GroupEnd
    answers: tuple[tuple[str, str], ...]
    ending_question: str | None
    declined_or_unasked: tuple[str, ...]


class PermissionEnd(enum.StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    DECLINED = "declined"
    UNAVAILABLE = "unavailable"


def _question_timeout() -> float:
    deadline = question_deadline_sec(None)
    assert deadline is not None
    return deadline


def _cutoff() -> float:
    return _question_timeout() + QUESTION_GRACE_SEC


def parse_ask_user_question(input: object) -> tuple[AskEntry, ...] | None:
    """Parse an ``AskUserQuestion`` input, or `None` when not representable."""
    if not isinstance(input, dict):
        return None
    questions = input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    seen: set[str] = set()
    entries: list[AskEntry] = []
    for raw in questions:
        if not isinstance(raw, dict):
            return None
        question = raw.get("question")
        if not isinstance(question, str) or not question:
            return None
        if question in seen:
            return None
        seen.add(question)
        header = raw.get("header")
        header_value = header if isinstance(header, str) and header else None
        options = raw.get("options")
        if not isinstance(options, list) or not options:
            return None
        parsed_options: list[AskOption] = []
        labels: set[str] = set()
        for raw_option in options:
            if not isinstance(raw_option, dict):
                return None
            label = raw_option.get("label")
            if (
                not isinstance(label, str)
                or not label
                or not label.strip()
                or label in labels
            ):
                return None
            labels.add(label)
            description = raw_option.get("description")
            parsed_options.append(
                AskOption(
                    label=label,
                    description=description
                    if isinstance(description, str) and description
                    else None,
                )
            )
        multi = raw.get("multiSelect")
        entries.append(
            AskEntry(
                question=question,
                header=header_value,
                options=tuple(parsed_options),
                multi_select=multi is True,
            )
        )
    return tuple(entries)


def _entry_message(entry: AskEntry, index: int, total: int) -> str:
    message = f"{entry.header}: {entry.question}" if entry.header else entry.question
    if total > 1:
        message = f"Question {index} of {total}. {message}"
    return message


def _other_label(taken: set[str]) -> str:
    candidate = OTHER_LABEL
    suffix = 2
    while candidate in taken:
        candidate = f"{OTHER_LABEL} {suffix}"
        suffix += 1
    return candidate


async def ask_group(
    user_prompt: IUserPrompt, entries: tuple[AskEntry, ...]
) -> GroupOutcome:
    """Ask a group entry by entry, stopping at the first non-answer."""
    answers: list[tuple[str, str]] = []
    total = len(entries)
    for position, entry in enumerate(entries, start=1):
        message = _entry_message(entry, position, total)
        taken = {option.label for option in entry.options}
        other = _other_label(taken)
        try:
            if entry.multi_select:
                result = await _ask_multi(user_prompt, message, entry, other)
            else:
                result = await _ask_single(user_prompt, message, entry, other)
        except TimeoutError:
            return GroupOutcome(
                end=GroupEnd.UNAVAILABLE,
                answers=tuple(answers),
                ending_question=entry.question,
                declined_or_unasked=tuple(e.question for e in entries[position - 1 :]),
            )
        if result is None:
            continue
        end, answer = result
        if end is GroupEnd.ANSWERED:
            assert answer is not None
            answers.append((entry.question, answer))
            continue
        return GroupOutcome(
            end=end,
            answers=tuple(answers),
            ending_question=entry.question,
            declined_or_unasked=tuple(e.question for e in entries[position - 1 :]),
        )
    return GroupOutcome(
        end=GroupEnd.ANSWERED,
        answers=tuple(answers),
        ending_question=None,
        declined_or_unasked=(),
    )


async def _ask_single(
    user_prompt: IUserPrompt, message: str, entry: AskEntry, other: str
) -> tuple[GroupEnd, str | None] | None:
    """Ask one single-select entry; `None` is unreachable, kept for symmetry."""
    async with asyncio.timeout(_cutoff()):
        result = await user_prompt.ask_choice(
            message,
            [option.label for option in entry.options] + [other],
            descriptions={
                option.label: option.description
                for option in entry.options
                if option.description is not None
            }
            | {other: OTHER_DESCRIPTION},
            timeout_sec=_question_timeout(),
        )
    if result.outcome is ElicitationOutcome.ANSWERED:
        if result.value == other:
            typed = await _ask_other_text(user_prompt, message)
            if typed is None:
                return None
            end, text = typed
            if end is not GroupEnd.ANSWERED:
                return end, None
            return GroupEnd.ANSWERED, text
        return GroupEnd.ANSWERED, result.value
    if result.outcome is ElicitationOutcome.DECLINED:
        return GroupEnd.DECLINED, None
    return GroupEnd.UNAVAILABLE, None


async def _ask_multi(
    user_prompt: IUserPrompt, message: str, entry: AskEntry, other: str
) -> tuple[GroupEnd, str | None] | None:
    async with asyncio.timeout(_cutoff()):
        result = await user_prompt.ask_multi_choice(
            message,
            [option.label for option in entry.options] + [other],
            descriptions={
                option.label: option.description
                for option in entry.options
                if option.description is not None
            }
            | {other: OTHER_DESCRIPTION},
            timeout_sec=_question_timeout(),
        )
    if result.outcome is ElicitationOutcome.ANSWERED:
        chosen = set(result.values or ())
        picked = [option.label for option in entry.options if option.label in chosen]
        if other in chosen:
            typed = await _ask_other_text(user_prompt, message)
            if typed is None:
                return None
            end, text = typed
            if end is not GroupEnd.ANSWERED:
                return end, None
            picked.append(text or "")
        if not picked and other not in chosen:
            return GroupEnd.ANSWERED, ""
        return GroupEnd.ANSWERED, ", ".join(picked)
    if result.outcome is ElicitationOutcome.DECLINED:
        return GroupEnd.DECLINED, None
    return GroupEnd.UNAVAILABLE, None


async def _ask_other_text(
    user_prompt: IUserPrompt, message: str
) -> tuple[GroupEnd, str | None] | None:
    async with asyncio.timeout(_cutoff()):
        result = await user_prompt.ask_text(
            f"{message}\nYour own answer:",
            multiline=False,
            timeout_sec=_question_timeout(),
        )
    if result.outcome is ElicitationOutcome.ANSWERED:
        return GroupEnd.ANSWERED, result.value if result.value is not None else ""
    if result.outcome is ElicitationOutcome.DECLINED:
        return GroupEnd.DECLINED, None
    return GroupEnd.UNAVAILABLE, None


def group_reply(
    outcome: GroupOutcome, original_input: dict[str, object]
) -> PermissionAllow | PermissionDeny:
    """Build the control reply for a finished group."""
    if outcome.end is GroupEnd.ANSWERED:
        answers = dict(outcome.answers)
        return PermissionAllow(
            behavior="allow",
            updatedInput={**original_input, "answers": answers},
        )
    if outcome.end is GroupEnd.DECLINED:
        lines = [QUESTION_DECLINED_REPLY]
        if outcome.answers:
            named = "; ".join(f'"{q}"="{a}"' for q, a in outcome.answers)
            lines.append(f"Answered before the person declined: {named}")
        declined = ", ".join(f'"{q}"' for q in outcome.declined_or_unasked)
        lines.append(f"Declined or not asked: {declined}")
        return PermissionDeny(behavior="deny", message="\n".join(lines))
    raise ValueError(f"no reply for group end {outcome.end}")


def permission_question(tool_name: str) -> str:
    return f"Allow Claude Code to use {tool_name}?"


def _permission_message(
    tool_name: str, tool_input: dict[str, object], description: str | None
) -> str:
    lines = [f"Claude Code asks to use {tool_name}."]
    if isinstance(description, str) and description:
        lines.append(description)
    lines.append("Input:")
    rendered = json.dumps(tool_input, indent=2, ensure_ascii=False)
    if len(rendered) > 4000:
        lines.append(rendered[:4000])
        lines.append(f"… (input truncated: 4000 of {len(rendered)} characters shown)")
    else:
        lines.append(rendered)
    return "\n".join(lines)


async def ask_permission(
    user_prompt: IUserPrompt,
    tool_name: str,
    tool_input: dict[str, object],
    description: str | None,
) -> PermissionEnd:
    """Ask to run one non-question tool call."""
    message = _permission_message(tool_name, tool_input, description)
    try:
        async with asyncio.timeout(_cutoff()):
            result = await user_prompt.ask_choice(
                message,
                [_ALLOW_ONCE, _DENY],
                descriptions=dict(_PERMISSION_DESCRIPTIONS),
                timeout_sec=_question_timeout(),
            )
    except TimeoutError:
        return PermissionEnd.UNAVAILABLE
    if result.outcome is ElicitationOutcome.ANSWERED:
        if result.value == _ALLOW_ONCE:
            return PermissionEnd.ALLOWED
        return PermissionEnd.DENIED
    if result.outcome is ElicitationOutcome.DECLINED:
        return PermissionEnd.DECLINED
    return PermissionEnd.UNAVAILABLE


def disallowed_tool_names(patterns: list[str]) -> frozenset[str]:
    """Tool names blocked outright, before any ask."""
    return frozenset(
        pattern.split("(")[0] if "(" in pattern else pattern for pattern in patterns
    )
