"""Shared question support for the agent backends.

Every backend's top-level config and profile opts in with the same key
(`AGENT_QUESTION_OPT_IN_KEY`), decides the same start from the same probe
(`resolve_question_mode`), reports the same refusal (`refused_at_start_result`)
and the same texts, and waits the same deadline (`question_deadline_sec`).
"""

from __future__ import annotations

import enum
import typing

from finecode_extension_api.interfaces.iuserprompt import (
    QUESTION_MAX_DEADLINE_SEC,
    IUserPrompt,
    PromptAvailability,
)

from fine_agent.run_agent_task_action import AgentRunStatus, RunAgentTaskRunResult

AGENT_QUESTION_OPT_IN_KEY: typing.Final = "may_ask_questions"
"""The opt-in key every backend's top-level config and profile uses.

`bool`, default `False`, inherited by profiles like `settle_timeout_sec`.
"""


class AgentQuestionMode(enum.StrEnum):
    WITHOUT_QUESTIONS = "without_questions"
    WITH_QUESTIONS = "with_questions"
    REFUSED = "refused"


async def resolve_question_mode(
    user_prompt: IUserPrompt,
    *,
    opted_in: bool,
    start_without_questions: bool,
) -> AgentQuestionMode:
    """Decide how an agent run starts from the opt-in and the availability probe.

    A run that did not opt in never probes: it starts without questions and
    costs no check. An opted-in run probes once; a passed probe starts with
    questions, a failed one starts without them when the caller overrode, and
    refuses otherwise. A probe answer that is not a `PromptAvailability` reads
    as "cannot". `ActionRunCancelled` and
    `WorkSlotScopeError` propagate.
    """
    if not opted_in:
        return AgentQuestionMode.WITHOUT_QUESTIONS
    availability = await user_prompt.check_availability()
    if isinstance(availability, PromptAvailability) and availability.can_ask is True:
        return AgentQuestionMode.WITH_QUESTIONS
    if start_without_questions:
        return AgentQuestionMode.WITHOUT_QUESTIONS
    return AgentQuestionMode.REFUSED


def refused_at_start_result() -> RunAgentTaskRunResult:
    """The result of a run refused at the start check.

    Reports no duration: a refused-at-start run never reached a backend.
    """
    return RunAgentTaskRunResult(
        status=AgentRunStatus.REFUSED_INTERACTION,
        output="",
        turns=None,
        usage=None,
        duration_sec=None,
        error=START_REFUSAL_ERROR,
        observation=None,
        session=None,
    )


QUESTION_DECLINED_REPLY: typing.Final[str] = (
    "The person declined to answer this question. "
    "This is not an answer: continue without it, or choose another approach."
)

START_REFUSAL_ERROR: typing.Final[str] = (
    "This task may ask questions (its profile sets may_ask_questions), "
    "but the client that started this run cannot answer questions: "
    "it did not declare that it can, or it is no longer connected. "
    "The backend was not started. "
    "Pass start_without_questions=true to run it without questions."
)


def unanswered_question_error(question: str) -> str:
    """The run stopped because nobody answered its question."""
    return f"No person answered the question, so the run stopped: {question}"


def question_not_asked_error(question: str, backend_timeout_sec: float) -> str:
    """The backend's own dialog timeout left no time to ask."""
    return (
        "The question was not asked: the backend's own "
        f"{backend_timeout_sec:g}s dialog timeout leaves no time for a person "
        f"to answer: {question}"
    )


def settle_budget_question_error(question: str, settle_timeout_sec: float) -> str:
    """The settle budget ran out while a question was pending."""
    return (
        f"The settle budget ({settle_timeout_sec:g}s) ran out while a question "
        f"was pending: {question}"
    )


QUESTION_MIN_DEADLINE_SEC: typing.Final = 1.0
"""Floor below which a question is not asked.
"""

QUESTION_GRACE_SEC: typing.Final = 5.0
"""How long after a question's deadline the adapter still waits for the WM's
outcome before acting on its own cutoff."""

BACKEND_TIMEOUT_MARGIN_SEC: typing.Final = 2.0
"""Safety margin between the backend starting its dialog timer and the adapter
receiving the request. Covers the transport delay."""


def question_deadline_sec(backend_timeout_sec: float | None) -> float | None:
    """How long a question waits for a person, or `None` for "do not ask".

    With no backend timeout the question gets the maximum deadline. Otherwise
    the deadline is the backend timeout minus the grace and the margin, capped
    at the maximum — and `None` when that leaves less than the 1 s floor, which
    means the run must not ask at all. The not-ask threshold is therefore 8 s.
    """
    if backend_timeout_sec is None:
        return QUESTION_MAX_DEADLINE_SEC
    deadline = min(
        QUESTION_MAX_DEADLINE_SEC,
        backend_timeout_sec - QUESTION_GRACE_SEC - BACKEND_TIMEOUT_MARGIN_SEC,
    )
    if deadline < QUESTION_MIN_DEADLINE_SEC:
        return None
    return deadline


__all__ = [
    "AGENT_QUESTION_OPT_IN_KEY",
    "BACKEND_TIMEOUT_MARGIN_SEC",
    "QUESTION_DECLINED_REPLY",
    "QUESTION_GRACE_SEC",
    "QUESTION_MAX_DEADLINE_SEC",
    "QUESTION_MIN_DEADLINE_SEC",
    "START_REFUSAL_ERROR",
    "AgentQuestionMode",
    "question_deadline_sec",
    "question_not_asked_error",
    "refused_at_start_result",
    "resolve_question_mode",
    "settle_budget_question_error",
    "unanswered_question_error",
]
