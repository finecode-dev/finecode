"""The shared start decision, refusal, texts and deadline (D6, D1, D2).

The three adapters consume one implementation of the D6 start decision, the
refused-at-start result, the D1 texts and the D2 deadline arithmetic — so the
table below is asserted once, here, rather than once per backend.
"""

from __future__ import annotations

import inspect
import typing
from typing import Self

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.iprojectactionrunner import ActionRunCancelled
from finecode_extension_api.interfaces.iuserprompt import PromptAvailability
from finecode_extension_api.interfaces.iworkslots import WorkSlotScopeError
from finecode_extension_runner.testing._stubs import FakeUserPrompt

from fine_agent import (
    AGENT_QUESTION_OPT_IN_KEY,
    BACKEND_TIMEOUT_MARGIN_SEC,
    QUESTION_DECLINED_REPLY,
    QUESTION_GRACE_SEC,
    QUESTION_MAX_DEADLINE_SEC,
    QUESTION_MIN_DEADLINE_SEC,
    AgentBackend,
    AgentQuestionMode,
    AgentRunStatus,
    QuestionShape,
    QuestionStart,
    RouteAgentTaskHandler,
    RouteAgentTaskHandlerConfig,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
    missing_shapes_refusal_error,
    question_deadline_sec,
    question_not_asked_error,
    refused_at_start_result,
    resolve_question_mode,
    resolve_question_start,
    settle_budget_question_error,
    unanswered_question_error,
)
from fine_agent.question_support import (
    QUESTION_NOT_SUPPORTED_REPLY,
    START_REFUSAL_ERROR,
)


def _prompt(can_ask: bool, checks: list[int] | None = None) -> typing.Any:
    """A scripted probe; `Any` because the double duck-types `IUserPrompt`."""
    prompt = FakeUserPrompt(
        availability=PromptAvailability(can_ask=can_ask),
    )
    if checks is not None:
        original = prompt.check_availability

        async def _counting() -> PromptAvailability:
            checks.append(1)
            return await original()

        prompt.check_availability = _counting
    return prompt


@pytest.mark.parametrize(
    ("opted_in", "can_ask", "override", "mode", "calls"),
    [
        (False, True, False, AgentQuestionMode.WITHOUT_QUESTIONS, 0),
        (False, False, True, AgentQuestionMode.WITHOUT_QUESTIONS, 0),
        (True, True, False, AgentQuestionMode.WITH_QUESTIONS, 1),
        (True, True, True, AgentQuestionMode.WITH_QUESTIONS, 1),
        (True, False, True, AgentQuestionMode.WITHOUT_QUESTIONS, 1),
        (True, False, False, AgentQuestionMode.REFUSED, 1),
    ],
)
async def test_the_start_decision_table(
    opted_in: bool, can_ask: bool, override: bool, mode: AgentQuestionMode, calls: int
) -> None:
    """A run that did not opt in never probes; a passed probe starts with
    questions; a failed one is overridden or refused."""
    checks: list[int] = []
    result = await resolve_question_mode(
        _prompt(can_ask, checks),
        opted_in=opted_in,
        start_without_questions=override,
    )

    assert result is mode
    assert len(checks) == calls


async def test_a_probe_answering_none_reads_as_cannot() -> None:
    """A probe that answers with no record is not a passed check."""

    class _NonePrompt:
        async def check_availability(self) -> None:
            return None

    assert (
        await resolve_question_mode(
            typing.cast(typing.Any, _NonePrompt()),
            opted_in=True,
            start_without_questions=False,
        )
        is AgentQuestionMode.REFUSED
    )


async def test_cancellation_of_the_probe_propagates() -> None:
    """A run torn down mid-probe is not a refused one."""

    class _CancellingPrompt:
        async def check_availability(self) -> PromptAvailability:
            raise ActionRunCancelled("torn down")

    with pytest.raises(ActionRunCancelled):
        await resolve_question_mode(
            typing.cast(typing.Any, _CancellingPrompt()),
            opted_in=True,
            start_without_questions=False,
        )


async def test_a_misplaced_probe_is_loud() -> None:
    """A probe made inside a work-slot scope raises instead of refusing."""

    class _ScopedPrompt:
        async def check_availability(self) -> PromptAvailability:
            raise WorkSlotScopeError("inside acquire")

    with pytest.raises(WorkSlotScopeError):
        await resolve_question_mode(
            typing.cast(typing.Any, _ScopedPrompt()),
            opted_in=True,
            start_without_questions=False,
        )


def test_the_refused_result_carries_no_run_data() -> None:
    """A refused-at-start run never reached a backend, so it reports none of
    one — and its error names the start check and the override, not CI."""
    result = refused_at_start_result()

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.output == ""
    assert result.turns is None
    assert result.usage is None
    assert result.session is None
    assert result.observation is None
    assert result.duration_sec is None
    assert result.error is not None
    assert "start_without_questions" in result.error
    assert "CI" not in result.error


def test_the_opt_in_key_name() -> None:
    """One key name for every backend's config and profile (D6.declare)."""
    assert AGENT_QUESTION_OPT_IN_KEY == "may_ask_questions"


def test_the_override_defaults_off_and_is_keyword_only() -> None:
    """Today's callers behave as before; the field cannot shift positionally."""
    assert RunAgentTaskRunPayload(prompt="x").start_without_questions is False
    assert (
        inspect.Parameter.KEYWORD_ONLY
        is inspect.signature(RunAgentTaskRunPayload)
        .parameters["start_without_questions"]
        .kind
    )


async def test_the_router_forwards_the_override_unchanged() -> None:
    """The router replaces only the profile; the override rides along."""

    received: list[dict[str, typing.Any]] = []

    class _RecordingRunner:
        def run_action_iter(
            self,
            ref: object,
            payload: RunAgentTaskRunPayload,
            meta: object,
            caller_kwargs: object = None,
            *,
            progress: object = None,
        ) -> typing.AsyncIterator[RunAgentTaskRunResult]:
            async def _gen() -> typing.AsyncIterator[RunAgentTaskRunResult]:
                yield RunAgentTaskRunResult(status=AgentRunStatus.SETTLED)

            received.append({"payload": payload, "meta": meta})
            return _gen()

    handler = RouteAgentTaskHandler(
        RouteAgentTaskHandlerConfig(default_backend=AgentBackend.PI, profiles={}),
        typing.cast(typing.Any, _RecordingRunner()),
        typing.cast(typing.Any, None),
    )
    meta = code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.USER,
        dev_env=code_action.DevEnv.CI,
        orchestration_depth=0,
    )

    class _Ctx:
        def __init__(self) -> None:
            self.meta = meta
            self._titles: list[str] = []

        def progress(self, title: str, **kwargs: typing.Any) -> _Progress:
            return _Progress()

    class _Progress:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    handler_ctx = typing.cast(typing.Any, _Ctx())
    await _drain(
        handler.run(
            RunAgentTaskRunPayload(prompt="x", start_without_questions=True),
            handler_ctx,
        )
    )

    assert len(received) == 1
    assert received[0]["payload"].start_without_questions is True
    assert received[0]["meta"] is handler_ctx.meta


async def _drain(
    gen: typing.AsyncIterator[RunAgentTaskRunResult],
) -> list[RunAgentTaskRunResult]:
    return [result async for result in gen]


@pytest.mark.parametrize(
    ("backend_timeout", "deadline"),
    [
        (None, 900.0),
        (100.0, 93.0),
        (8.0, 1.0),
        (7.999, None),
        (2.0, None),
        (2000.0, 900.0),
    ],
)
def test_the_question_deadline(
    backend_timeout: float | None, deadline: float | None
) -> None:
    """No backend timeout gets the maximum; otherwise the timeout minus grace
    and margin, capped — and nothing below the 1 s floor is asked at all."""
    assert question_deadline_sec(backend_timeout) == deadline


def test_the_deadline_constants() -> None:
    """The 8 s not-ask threshold falls out of the floor, grace and margin."""
    assert QUESTION_MAX_DEADLINE_SEC == 900.0
    assert QUESTION_MIN_DEADLINE_SEC == 1.0
    assert QUESTION_GRACE_SEC == 5.0
    assert BACKEND_TIMEOUT_MARGIN_SEC == 2.0
    assert (
        QUESTION_MIN_DEADLINE_SEC + QUESTION_GRACE_SEC + BACKEND_TIMEOUT_MARGIN_SEC
        == 8.0
    )


def test_the_declined_reply_is_not_an_answer() -> None:
    """A declined question tells the agent to continue without the answer."""
    assert QUESTION_DECLINED_REPLY
    assert "declined" in QUESTION_DECLINED_REPLY


def _shaped_prompt(
    can_ask: bool,
    *,
    text: bool = False,
    multi: bool = False,
    checks: list[int] | None = None,
) -> typing.Any:
    prompt = FakeUserPrompt(
        availability=PromptAvailability(
            can_ask=can_ask, can_ask_text=text, can_ask_multi_choice=multi
        ),
    )
    if checks is not None:
        original = prompt.check_availability

        async def _counting() -> PromptAvailability:
            checks.append(1)
            return await original()

        prompt.check_availability = _counting
    return prompt


_ALL_SHAPES = frozenset({QuestionShape.TEXT, QuestionShape.MULTI_CHOICE})


@pytest.mark.parametrize(
    ("can_ask", "text", "multi", "override", "mode", "calls"),
    [
        (True, True, True, False, AgentQuestionMode.WITH_QUESTIONS, 1),
        (True, False, True, False, AgentQuestionMode.REFUSED, 1),
        (True, True, False, False, AgentQuestionMode.REFUSED, 1),
        (True, False, False, False, AgentQuestionMode.REFUSED, 1),
        (True, False, True, True, AgentQuestionMode.WITHOUT_QUESTIONS, 1),
        (True, True, False, True, AgentQuestionMode.WITHOUT_QUESTIONS, 1),
        (True, False, False, True, AgentQuestionMode.WITHOUT_QUESTIONS, 1),
        (False, False, False, False, AgentQuestionMode.REFUSED, 1),
        (False, False, False, True, AgentQuestionMode.WITHOUT_QUESTIONS, 1),
    ],
)
async def test_the_shaped_start_decision_table(
    can_ask: bool,
    text: bool,
    multi: bool,
    override: bool,
    mode: AgentQuestionMode,
    calls: int,
) -> None:
    """A shaped start check passes only when every required shape is declared."""
    checks: list[int] = []
    start = await resolve_question_start(
        _shaped_prompt(can_ask, text=text, multi=multi, checks=checks),
        opted_in=True,
        start_without_questions=override,
        required_shapes=_ALL_SHAPES,
    )

    assert start.mode is mode
    assert len(checks) == calls
    if mode is AgentQuestionMode.REFUSED:
        assert start.refusal_error is not None
        assert "start_without_questions" in start.refusal_error
    else:
        assert start.refusal_error is None


async def test_a_non_opted_in_shaped_start_never_probes() -> None:
    """A run that did not opt in pays for no availability check."""
    checks: list[int] = []
    start = await resolve_question_start(
        _shaped_prompt(True, text=True, multi=True, checks=checks),
        opted_in=False,
        start_without_questions=False,
        required_shapes=_ALL_SHAPES,
    )

    assert start.mode is AgentQuestionMode.WITHOUT_QUESTIONS
    assert start.refusal_error is None
    assert checks == []


async def test_a_shaped_refusal_names_the_missing_shapes() -> None:
    """A shaped refusal says which questions the client cannot answer."""
    start = await resolve_question_start(
        _shaped_prompt(True, text=False, multi=True),
        opted_in=True,
        start_without_questions=False,
        required_shapes=_ALL_SHAPES,
    )

    assert start.mode is AgentQuestionMode.REFUSED
    assert start.refusal_error == missing_shapes_refusal_error((QuestionShape.TEXT,))
    assert "text" in (start.refusal_error or "")
    assert "start_without_questions" in (start.refusal_error or "")
    assert "CI" not in (start.refusal_error or "")


async def test_a_shaped_probe_answering_none_reads_as_cannot() -> None:
    """A probe with no record refuses with the unshaped error."""

    class _NonePrompt:
        async def check_availability(self) -> None:
            return None

    start = await resolve_question_start(
        typing.cast(typing.Any, _NonePrompt()),
        opted_in=True,
        start_without_questions=False,
        required_shapes=_ALL_SHAPES,
    )

    assert start.mode is AgentQuestionMode.REFUSED
    assert start.refusal_error == START_REFUSAL_ERROR


def test_the_not_supported_reply_is_not_an_answer() -> None:
    """An unpresentable question tells the agent to continue without it."""
    assert QUESTION_NOT_SUPPORTED_REPLY
    assert "not an answer" in QUESTION_NOT_SUPPORTED_REPLY


def test_a_custom_refusal_error_is_reported() -> None:
    """A refused start can carry the shaped refusal text."""
    assert refused_at_start_result("x").error == "x"
    assert refused_at_start_result().error == START_REFUSAL_ERROR


@pytest.mark.parametrize("question", ["Keep them?", "x"])
def test_the_errors_name_the_question(question: str) -> None:
    """Each error carries the question verbatim, plus its own number."""
    assert question in unanswered_question_error(question)
    budgeted = question_not_asked_error(question, 12.5)
    assert question in budgeted
    assert "12.5" in budgeted
    settled = settle_budget_question_error(question, 30.0)
    assert question in settled
    assert "30" in settled
