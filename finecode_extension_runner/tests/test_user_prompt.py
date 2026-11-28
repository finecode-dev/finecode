"""``ask_choice`` never raises, and never invents an answer (ADR-0082 rule 3).

A handler that had to catch exceptions to find out that nobody was there is a
handler nobody can run unattended, and one that cannot tell "a person refused"
from "no person" cannot decide between retrying, defaulting and aborting. Both
properties are the interface's whole point, so both are pinned here.
"""

from __future__ import annotations

import asyncio
import typing

import finecode_jsonrpc as finecode_jsonrpc_module
import pytest
from finecode_extension_api.interfaces.iprojectactionrunner import ActionRunCancelled
from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationOutcome,
    PromptAvailability,
)
from finecode_extension_api.interfaces.iworkslots import WorkSlotScopeError

from finecode_extension_runner import er_errors, er_server, run_context
from finecode_extension_runner.impls.user_prompt import UserPrompt
from finecode_extension_runner.process_slots import ProcessSlots
from finecode_extension_runner.work_slots import WorkSlots


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


# ---------------------------------------------------------------------------
# check_availability: the start probe, answered without a question
# ---------------------------------------------------------------------------


def _available(value: object) -> UserPrompt:
    async def _send(_method: str, _params: dict) -> object:
        return value

    return UserPrompt(_send)


async def test_no_wm_at_all_cannot_be_asked() -> None:
    """An ER exercised standalone has no back-channel, which is not a defect."""
    result = await UserPrompt(None).check_availability()

    assert result == PromptAvailability(can_ask=False)


async def test_a_true_answer_is_the_only_yes() -> None:
    """Only a dict result whose ``canAnswer`` is exactly ``True`` reads "can"."""
    assert await _available({"canAnswer": True}).check_availability() == (
        PromptAvailability(can_ask=True)
    )


@pytest.mark.parametrize(
    "answer",
    [
        {"canAnswer": False},
        {"canAnswer": "yes"},
        {"canAnswer": 1},
        {"canAnswer": None},
        {},
        "not even a dict",
        None,
    ],
)
async def test_anything_but_true_is_cannot(answer: object) -> None:
    """An older WM answers method-not-found (an error, not data); a malformed
    result is the same "cannot" rather than a crash."""
    assert await _available(answer).check_availability() == PromptAvailability(
        can_ask=False
    )


@pytest.mark.parametrize(
    "failure",
    [
        er_errors.WmCommunicationError("socket is gone"),
        RuntimeError("something nobody enumerated"),
        ConnectionResetError(),
    ],
)
async def test_a_failed_probe_is_cannot_not_an_exception(
    failure: Exception,
) -> None:
    """Transport errors and the WM's own errors (an older WM's method-not-found
    arrives as one) all mean the same thing: nobody's availability is known."""
    assert await _prompt_raising(failure).check_availability() == PromptAvailability(
        can_ask=False
    )


async def test_a_method_not_found_becomes_cannot_through_the_sender() -> None:
    """The real sender translates the older WM's method-not-found into an
    error, which the probe reads as "cannot" rather than raising."""

    class _OldWm:
        async def send_request_to_wm(self, method: str, params: dict) -> typing.Any:
            raise finecode_jsonrpc_module.JsonRpcError(
                finecode_jsonrpc_module.METHOD_NOT_FOUND, "no such method"
            )

    sender = er_server.make_wm_request_sender(_OldWm())  # type: ignore[arg-type]
    assert await UserPrompt(sender).check_availability() == PromptAvailability(
        can_ask=False
    )


async def test_probe_cancellation_still_propagates() -> None:
    """Cancelling a run mid-probe must not look like an unattended one."""
    with pytest.raises(ActionRunCancelled):
        await _prompt_raising(
            er_errors.WmCommunicationCancelled("run cancelled")
        ).check_availability()


async def test_a_probe_inside_a_work_slot_is_loud() -> None:
    """A check made where it would wait on the slot it holds is a programming
    error (C27): it must raise, never silently refuse a capable run."""
    gate = ProcessSlots(target=8)
    scopes = WorkSlots(gate)

    class _NeverReached:
        async def send_request_to_wm(self, method: str, params: dict) -> typing.Any:
            raise AssertionError("the scope guard must fire first")

    sender = er_server.make_wm_request_sender(_NeverReached())  # type: ignore[arg-type]
    prompt = UserPrompt(sender)

    async with scopes.acquire():
        with pytest.raises(WorkSlotScopeError):
            await prompt.check_availability()


async def test_the_probe_goes_out_as_finecode_elicit_availability() -> None:
    """The probe names its own method and the asking run, and nothing else."""
    sent: list[tuple[str, dict]] = []

    async def _send(method: str, params: dict) -> object:
        sent.append((method, params))
        return {"canAnswer": True}

    with run_context.run("run-0042"):
        result = await UserPrompt(_send).check_availability()

    assert result == PromptAvailability(can_ask=True)
    assert sent == [("finecode/elicitAvailability", {"runId": "run-0042"})]


async def test_elicit_reaches_the_wm_without_a_transport_timeout() -> None:
    """The WM enforces the question deadline (C6): the ER transport must not
    impose its own. A ``finecode/elicit`` sent through the guarded sender
    arrives at ``send_request_to_wm`` with no timeout attached."""
    seen: dict[str, object] = {}

    class _Session:
        async def send_request(
            self, method: str, params: dict, **kwargs: object
        ) -> object:
            seen["method"] = method
            seen["kwargs"] = kwargs
            return {"outcome": "unavailable"}

    server = er_server.ErServer()
    server._session = _Session()  # type: ignore[assignment]
    sender = er_server.make_wm_request_sender(server)

    await UserPrompt(sender).ask_choice("keep it?", ["keep"])

    assert seen["method"] == "finecode/elicit"
    assert "timeout" not in typing.cast(dict, seen["kwargs"])


# ---------------------------------------------------------------------------
# Text and multi-select shapes (issue 983: A3-A11)
# ---------------------------------------------------------------------------


def _scripted_sender(
    availability: object | BaseException, answer: object | BaseException
) -> tuple[UserPrompt, list[tuple[str, dict]]]:
    sent: list[tuple[str, dict]] = []

    async def _send(method: str, params: dict) -> object:
        sent.append((method, params))
        if method == "finecode/elicitAvailability":
            if isinstance(availability, BaseException):
                raise availability
            return availability
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return UserPrompt(_send), sent


def _text_available() -> dict:
    return {"canAnswer": True, "canAnswerText": True}


def _multi_available() -> dict:
    return {"canAnswer": True, "canAnswerMultiChoice": True}


@pytest.mark.parametrize("text", ["", "a\nb", "  x  ", "draft"])
async def test_text_exact_value_comes_back(text: str) -> None:
    prompt, _ = _scripted_sender(
        _text_available(), {"outcome": "answered", "value": text}
    )
    result = await prompt.ask_text("Name?", initial_value="draft")
    assert result.outcome is ElicitationOutcome.ANSWERED
    assert result.value == text
    assert result.values is None


async def test_multi_empty_and_reordered() -> None:
    prompt, _ = _scripted_sender(
        _multi_available(), {"outcome": "answered", "values": []}
    )
    result = await prompt.ask_multi_choice("Pick?", ["a", "b", "c"])
    assert result.values == ()

    prompt2, _ = _scripted_sender(
        _multi_available(), {"outcome": "answered", "values": ["c", "a"]}
    )
    result2 = await prompt2.ask_multi_choice("Pick?", ["a", "b", "c"])
    assert result2.values == ("a", "c")
    assert result2.value is None


async def test_multi_preselection_unchanged_comes_back_ordered() -> None:
    prompt, sent = _scripted_sender(
        _multi_available(), {"outcome": "answered", "values": ["c", "a"]}
    )
    result = await prompt.ask_multi_choice("Pick?", ["a", "b", "c"], default=["c", "a"])
    assert result.values == ("a", "c")
    assert sent[1][1]["default"] == ["c", "a"]


@pytest.mark.parametrize("outcome", ["declined", "unavailable"])
async def test_declined_and_unavailable_carry_no_values(outcome: str) -> None:
    prompt, _ = _scripted_sender(_text_available(), {"outcome": outcome})
    result = await prompt.ask_text("Name?", initial_value="draft")
    assert result.value is None
    assert result.values is None
    prompt2, _ = _scripted_sender(_multi_available(), {"outcome": outcome})
    result2 = await prompt2.ask_multi_choice("Pick?", ["a"], default=["a"])
    assert result2.value is None
    assert result2.values is None


async def test_cancelled_run_raises_for_new_shapes() -> None:
    prompt, _ = _scripted_sender(
        _text_available(), er_errors.WmCommunicationCancelled("gone")
    )
    with pytest.raises(ActionRunCancelled):
        await prompt.ask_text("Name?")
    prompt2, _ = _scripted_sender(
        _multi_available(), er_errors.WmCommunicationCancelled("gone")
    )
    with pytest.raises(ActionRunCancelled):
        await prompt2.ask_multi_choice("Pick?", ["a"])


@pytest.mark.parametrize(
    "availability",
    [
        er_errors.WmCommunicationError("method not found"),
        {"canAnswer": True},
        {"canAnswer": True, "canAnswerText": False},
    ],
)
async def test_text_probe_refusal_sends_no_elicit(availability: object) -> None:
    prompt, sent = _scripted_sender(availability, {"outcome": "answered", "value": "x"})
    result = await prompt.ask_text("Name?")
    assert result.outcome is ElicitationOutcome.UNAVAILABLE
    assert [m for m, _ in sent] == ["finecode/elicitAvailability"]


@pytest.mark.parametrize(
    "availability",
    [
        er_errors.WmCommunicationError("method not found"),
        {"canAnswer": True},
        {"canAnswer": True, "canAnswerMultiChoice": False},
    ],
)
async def test_multi_probe_refusal_sends_no_elicit(availability: object) -> None:
    prompt, sent = _scripted_sender(
        availability, {"outcome": "answered", "values": ["a"]}
    )
    result = await prompt.ask_multi_choice("Pick?", ["a"])
    assert result.outcome is ElicitationOutcome.UNAVAILABLE
    assert [m for m, _ in sent] == ["finecode/elicitAvailability"]


async def test_cancelling_during_probe_raises() -> None:
    prompt, _ = _scripted_sender(
        er_errors.WmCommunicationCancelled("gone"), {"outcome": "answered"}
    )
    with pytest.raises(ActionRunCancelled):
        await prompt.ask_text("Name?")
    prompt2, _ = _scripted_sender(
        er_errors.WmCommunicationCancelled("gone"), {"outcome": "answered"}
    )
    with pytest.raises(ActionRunCancelled):
        await prompt2.ask_multi_choice("Pick?", ["a"])


async def test_asks_inside_a_work_slot_send_nothing() -> None:
    gate = ProcessSlots(target=8)
    scopes = WorkSlots(gate)

    class _NeverReached:
        async def send_request_to_wm(self, method: str, params: dict) -> typing.Any:
            raise AssertionError("must not send inside a work slot")

    sender = er_server.make_wm_request_sender(_NeverReached())  # type: ignore[arg-type]
    prompt = UserPrompt(sender)
    async with scopes.acquire():
        assert (
            await prompt.ask_choice("q?", ["a"])
        ).outcome is ElicitationOutcome.UNAVAILABLE
        assert (await prompt.ask_text("q?")).outcome is ElicitationOutcome.UNAVAILABLE
        assert (
            await prompt.ask_multi_choice("q?", ["a"])
        ).outcome is ElicitationOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "reply",
    [
        {"outcome": "answered"},
        {"outcome": "answered", "value": 3},
        {"outcome": "answered", "value": None},
    ],
)
async def test_text_without_string_value_is_unavailable(reply: object) -> None:
    prompt, _ = _scripted_sender(_text_available(), reply)
    assert (await prompt.ask_text("Name?")).outcome is ElicitationOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "reply",
    [
        {"outcome": "answered"},
        {"outcome": "answered", "values": ["zzz"]},
        {"outcome": "answered", "values": ["a", "a"]},
        {"outcome": "answered", "values": ["a", 1]},
        {"outcome": "answered", "values": "a"},
        {"outcome": "answered", "value": "a"},
    ],
)
async def test_multi_without_valid_values_is_unavailable(reply: object) -> None:
    prompt, _ = _scripted_sender(_multi_available(), reply)
    result = await prompt.ask_multi_choice("Pick?", ["a", "b"])
    assert result.outcome is ElicitationOutcome.UNAVAILABLE
    assert result.values is None


async def test_multi_empty_or_repeated_options_send_nothing() -> None:
    prompt, sent = _scripted_sender(_multi_available(), {"outcome": "answered"})
    assert (
        await prompt.ask_multi_choice("q?", [])
    ).outcome is ElicitationOutcome.UNAVAILABLE
    assert (
        await prompt.ask_multi_choice("q?", ["a", "a"])
    ).outcome is ElicitationOutcome.UNAVAILABLE
    assert sent == []


async def test_multi_unoffered_default_and_descriptions_left_out() -> None:
    prompt, sent = _scripted_sender(
        _multi_available(), {"outcome": "answered", "values": ["a"]}
    )
    await prompt.ask_multi_choice(
        "Pick?", ["a", "b"], default=["a", "zzz"], descriptions={"a": "A", "zzz": "Z"}
    )
    params = sent[1][1]
    assert params.get("default") == ["a"]
    assert params.get("descriptions") == {"a": "A"}


async def test_choice_without_descriptions_sends_todays_params() -> None:
    prompt, sent = _scripted_sender({"canAnswer": True}, {"outcome": "declined"})
    await prompt.ask_choice("keep it?", ["keep"], default="keep", timeout_sec=12.5)
    assert sent == [
        (
            "finecode/elicit",
            {
                "message": "keep it?",
                "options": ["keep"],
                "default": "keep",
                "timeoutSec": 12.5,
                "runId": None,
            },
        )
    ]


async def test_choice_with_descriptions_adds_only_them() -> None:
    prompt, sent = _scripted_sender(
        {"canAnswer": True}, {"outcome": "answered", "value": "a"}
    )
    await prompt.ask_choice(
        "Pick?", ["a", "b"], descriptions={"a": "First", "zzz": "Z"}
    )
    params = sent[0][1]
    assert params["descriptions"] == {"a": "First"}
    assert "kind" not in params


async def test_choice_answer_survives_a_wm_that_drops_descriptions() -> None:
    async def _send(method: str, params: dict) -> object:
        params.pop("descriptions", None)
        return {"outcome": "answered", "value": "a"}

    result = await UserPrompt(_send).ask_choice("Pick?", ["a"], descriptions={"a": "A"})
    assert result.outcome is ElicitationOutcome.ANSWERED
    assert result.value == "a"
