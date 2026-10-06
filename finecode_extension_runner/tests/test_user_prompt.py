"""``ask_choice`` never raises, and never invents an answer (ADR-0082 rule 3).

A handler that had to catch exceptions to find out that nobody was there is a
handler nobody can run unattended, and one that cannot tell "a person refused"
from "no person" cannot decide between retrying, defaulting and aborting. Both
properties are the interface's whole point, so both are pinned here.
"""

from __future__ import annotations

import asyncio

import pytest
from finecode_extension_api.interfaces.iprojectactionrunner import ActionRunCancelled
from finecode_extension_api.interfaces.iuserprompt import ElicitationOutcome

from finecode_extension_runner import er_errors, run_context
from finecode_extension_runner.impls.user_prompt import UserPrompt


def _prompt_returning(value: object) -> UserPrompt:
    async def _send(_method: str, _params: dict) -> object:
        return value

    return UserPrompt(_send)


def _prompt_raising(exception: BaseException) -> UserPrompt:
    async def _send(_method: str, _params: dict) -> object:
        raise exception

    return UserPrompt(_send)


async def test_an_answer_comes_back_as_answered() -> None:
    prompt = _prompt_returning({"outcome": "answered", "value": "revert"})
    result = await prompt.ask_choice("keep it?", ["keep", "revert"])
    assert result.outcome is ElicitationOutcome.ANSWERED
    assert result.value == "revert"


async def test_a_refusal_stays_distinct_from_having_nobody_to_ask() -> None:
    prompt = _prompt_returning({"outcome": "declined"})
    result = await prompt.ask_choice("keep it?", ["keep", "revert"])
    assert result.outcome is ElicitationOutcome.DECLINED
    assert result.value is None


@pytest.mark.parametrize(
    "failure",
    [
        er_errors.WmCommunicationError("socket is gone"),
        RuntimeError("something nobody enumerated"),
        ConnectionResetError(),
    ],
)
async def test_a_transport_failure_is_an_outcome_not_an_exception(
    failure: Exception,
) -> None:
    prompt = _prompt_raising(failure)
    result = await prompt.ask_choice("keep it?", ["keep"])
    assert result.outcome is ElicitationOutcome.UNAVAILABLE


async def test_cancellation_still_propagates() -> None:
    """A run being torn down is not an ask that failed to be answered."""
    prompt = _prompt_raising(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await prompt.ask_choice("keep it?", ["keep"])


async def test_a_cancelled_run_is_not_reported_as_nobody_to_ask() -> None:
    """Cancelling a run mid-question must not look like an unattended one.

    The WM signals cancellation as an ordinary error over the back-channel, so
    it reaches this layer as a plain exception. Reported as "unavailable" the
    handler would take its unattended branch and keep working on a run that no
    longer exists.
    """
    prompt = _prompt_raising(er_errors.WmCommunicationCancelled("run cancelled"))
    with pytest.raises(ActionRunCancelled):
        await prompt.ask_choice("keep it?", ["keep"])


async def test_the_wm_s_own_unavailable_is_passed_through() -> None:
    prompt = _prompt_returning({"outcome": "unavailable"})
    result = await prompt.ask_choice("keep it?", ["keep"])
    assert result.outcome is ElicitationOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "answer",
    [
        {"outcome": "answered", "value": "something nobody offered"},
        {"outcome": "answered"},
        {"outcome": "who knows"},
        {},
        "not even a dict",
    ],
)
async def test_an_answer_that_was_never_offered_is_no_answer(answer: object) -> None:
    prompt = _prompt_returning(answer)
    result = await prompt.ask_choice("keep it?", ["keep", "revert"])
    assert result.outcome is ElicitationOutcome.UNAVAILABLE
    assert result.value is None


async def test_no_wm_at_all_is_nobody_to_ask() -> None:
    """An ER exercised standalone has no back-channel, which is not a defect."""
    result = await UserPrompt(None).ask_choice("keep it?", ["keep"])
    assert result.outcome is ElicitationOutcome.UNAVAILABLE


async def test_a_question_with_no_options_is_not_sent() -> None:
    sent: list[tuple[str, dict]] = []

    async def _send(method: str, params: dict) -> object:
        sent.append((method, params))
        return {"outcome": "answered", "value": "x"}

    result = await UserPrompt(_send).ask_choice("keep it?", [])
    assert result.outcome is ElicitationOutcome.UNAVAILABLE
    assert sent == []


async def test_the_question_goes_out_as_finecode_elicit() -> None:
    sent: list[tuple[str, dict]] = []

    async def _send(method: str, params: dict) -> object:
        sent.append((method, params))
        return {"outcome": "answered", "value": "keep"}

    await UserPrompt(_send).ask_choice(
        "keep it?", ["keep", "revert"], default="keep", timeout_sec=12.5
    )

    assert sent == [
        (
            "finecode/elicit",
            {
                "message": "keep it?",
                "options": ["keep", "revert"],
                "default": "keep",
                "timeoutSec": 12.5,
                "runId": None,
            },
        )
    ]


async def test_the_question_names_the_run_that_is_asking() -> None:
    """The WM has nothing else to address it by.

    Two clients can be running the same project at the same moment, so the
    runner that asks does not identify whose question this is — only the run
    does (ADR-0082 rule 1).
    """
    sent: list[tuple[str, dict]] = []

    async def _send(method: str, params: dict) -> object:
        sent.append((method, params))
        return {"outcome": "answered", "value": "keep"}

    with run_context.run("run-0042"):
        await UserPrompt(_send).ask_choice("keep it?", ["keep"])

    assert sent[0][1]["runId"] == "run-0042"


async def test_a_question_outside_any_run_names_none() -> None:
    """Nothing is invented for code that is not executing a dispatched run."""
    sent: list[tuple[str, dict]] = []

    async def _send(method: str, params: dict) -> object:
        sent.append((method, params))
        return {"outcome": "answered", "value": "keep"}

    await UserPrompt(_send).ask_choice("keep it?", ["keep"])

    assert sent[0][1]["runId"] is None
