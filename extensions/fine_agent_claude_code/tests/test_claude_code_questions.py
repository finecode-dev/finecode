"""Mapping from claude permission prompts to person asks."""

from __future__ import annotations

import asyncio
import typing

import pytest
from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationOutcome,
    ElicitationResult,
)
from finecode_extension_runner.testing._stubs import FakeUserPrompt

from fine_agent_claude_code import claude_code_questions as q


def _answered_choice(value: str) -> ElicitationResult:
    return ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value=value)


def _answered_multi(values: tuple[str, ...]) -> ElicitationResult:
    return ElicitationResult(outcome=ElicitationOutcome.ANSWERED, values=values)


def _declined() -> ElicitationResult:
    return ElicitationResult(outcome=ElicitationOutcome.DECLINED)


def _unavailable() -> ElicitationResult:
    return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)


def test_parse_single_entry() -> None:
    """A recorded AskUserQuestion input parses to one entry."""
    entries = q.parse_ask_user_question(
        {
            "questions": [
                {
                    "question": "Which codename?",
                    "header": "Codename",
                    "multiSelect": False,
                    "options": [
                        {"label": "A", "description": "use A"},
                        {"label": "B", "description": ""},
                    ],
                }
            ]
        }
    )

    assert entries is not None
    assert len(entries) == 1
    assert entries[0].question == "Which codename?"
    assert entries[0].header == "Codename"
    assert entries[0].multi_select is False
    assert entries[0].options[0] == q.AskOption(label="A", description="use A")
    assert entries[0].options[1] == q.AskOption(label="B", description=None)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"questions": []},
        {"questions": "x"},
        {"questions": [{"question": "", "options": [{"label": "A"}]}]},
        {"questions": [{"question": "Q", "options": []}]},
        {"questions": [{"question": "Q", "options": [{"label": "  "}]}]},
        {"questions": [{"question": "Q", "options": [{"label": "A"}, {"label": "A"}]}]},
        {
            "questions": [
                {"question": "Q", "options": [{"label": "A"}]},
                {"question": "Q", "options": [{"label": "B"}]},
            ]
        },
        {"questions": [{"options": [{"label": "A"}]}]},
        {"questions": [{"question": "Q"}]},
    ],
)
def test_parse_rejects_not_representable(payload: object) -> None:
    """Each invalid shape parses to no ask at all."""
    assert q.parse_ask_user_question(payload) is None


async def test_group_message_options_and_descriptions() -> None:
    """Messages, options and descriptions follow the recorded texts."""
    prompt = FakeUserPrompt(answers=[_answered_choice("A")])
    entries = (
        q.AskEntry(
            question="Pick?",
            header="H",
            options=(q.AskOption("A", "desc A"), q.AskOption("B", None)),
            multi_select=False,
        ),
    )

    outcome = await q.ask_group(typing.cast(typing.Any, prompt), entries)

    assert outcome.end is q.GroupEnd.ANSWERED
    assert outcome.answers == (("Pick?", "A"),)
    assert prompt.asks[0].message == "H: Pick?"
    assert prompt.asks[0].timeout_sec == 900.0
    assert "Other (type an answer)" in prompt.asks[0].options
    assert prompt.asks[0].descriptions is not None
    assert prompt.asks[0].descriptions.get("A") == "desc A"


async def test_group_prefix_and_other_collision() -> None:
    """Groups number questions; a taken Other label gains a suffix."""
    prompt = FakeUserPrompt(
        answers=[_answered_choice("A"), _answered_choice("Other (type an answer) 2")]
    )
    entries = (
        q.AskEntry("Q1?", None, (q.AskOption("A", None),), False),
        q.AskEntry(
            "Q2?",
            None,
            (q.AskOption("Other (type an answer)", None),),
            False,
        ),
    )

    group_outcome = await q.ask_group(typing.cast(typing.Any, prompt), entries)

    assert group_outcome.end is q.GroupEnd.UNAVAILABLE
    assert prompt.asks[0].message.startswith("Question 1 of 2. ")
    assert "Other (type an answer) 2" in prompt.asks[1].options


async def test_encodings() -> None:
    """Single, Other and multi answers encode as the smoke recorded."""
    prompt = FakeUserPrompt(
        answers=[
            _answered_choice("A"),
            _answered_choice("Other (type an answer)"),
            _answered_choice("typed alone"),
            _answered_multi(("B", "A")),
            _answered_multi(("A", "Other (type an answer)")),
            _answered_choice("with, comma"),
            _answered_multi(()),
        ]
    )
    single = q.AskEntry(
        "S?", None, (q.AskOption("A", None), q.AskOption("B", None)), False
    )
    multi = q.AskEntry(
        "M?", None, (q.AskOption("A", None), q.AskOption("B", None)), True
    )

    first = await q.ask_group(typing.cast(typing.Any, prompt), (single,))
    assert first.answers == (("S?", "A"),)

    second = await q.ask_group(typing.cast(typing.Any, prompt), (single,))
    assert second.answers == (("S?", "typed alone"),)

    third = await q.ask_group(typing.cast(typing.Any, prompt), (multi,))
    assert third.answers == (("M?", "A, B"),)

    fourth = await q.ask_group(typing.cast(typing.Any, prompt), (multi,))
    assert fourth.answers == (("M?", "A, with, comma"),)

    fifth = await q.ask_group(typing.cast(typing.Any, prompt), (multi,))
    assert fifth.answers == (("M?", ""),)


async def test_decline_at_second_entry_names_answers() -> None:
    """A decline names what was answered and what was not."""
    prompt = FakeUserPrompt(answers=[_answered_choice("A"), _declined(), _declined()])
    entries = (
        q.AskEntry("Q1", None, (q.AskOption("A", None),), False),
        q.AskEntry("Q2", None, (q.AskOption("B", None),), False),
        q.AskEntry("Q3", None, (q.AskOption("C", None),), False),
    )

    outcome = await q.ask_group(typing.cast(typing.Any, prompt), entries)

    assert outcome.end is q.GroupEnd.DECLINED
    assert outcome.answers == (("Q1", "A"),)
    assert outcome.ending_question == "Q2"
    assert outcome.declined_or_unasked == ("Q2", "Q3")
    original: dict[str, object] = {"questions": []}
    reply = q.group_reply(outcome, original)
    assert reply["behavior"] == "deny"
    assert isinstance(reply, dict)
    message = typing.cast(dict[str, typing.Any], reply)["message"]
    assert '"Q1"="A"' in message
    assert '"Q2"' in message and '"Q3"' in message


async def test_unavailable_at_other_text_ends_group() -> None:
    """Nobody-to-ask at an Other follow-up ends the group there."""
    prompt = FakeUserPrompt(
        answers=[_answered_choice("Other (type an answer)"), _unavailable()]
    )
    entries = (q.AskEntry("Q1", None, (q.AskOption("A", None),), False),)

    outcome = await q.ask_group(typing.cast(typing.Any, prompt), entries)

    assert outcome.end is q.GroupEnd.UNAVAILABLE
    assert outcome.ending_question == "Q1"


def test_cutoff_on_virtual_time() -> None:
    """A never-answering ask ends unavailable just after the cutoff."""
    from finecode_extension_runner.testing import run_on_virtual_time  # noqa: PLC0415

    seen: list[bool] = []

    class _HangingPrompt(FakeUserPrompt):
        async def ask_choice(
            self, *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Any:
            try:
                await asyncio.sleep(10_000)
            except asyncio.CancelledError:
                seen.append(True)
                raise
            return _unavailable()

    async def _scenario() -> tuple[q.GroupOutcome, float]:
        prompt = _HangingPrompt()
        entries = (q.AskEntry("Q1", None, (q.AskOption("A", None),), False),)
        outcome = await q.ask_group(typing.cast(typing.Any, prompt), entries)
        return outcome, asyncio.get_running_loop().time()

    outcome, elapsed = run_on_virtual_time(_scenario())

    assert outcome.end is q.GroupEnd.UNAVAILABLE
    assert outcome.ending_question == "Q1"
    assert elapsed == pytest.approx(905.0)
    assert seen == [True]


async def test_permission_message_and_mapping() -> None:
    """Permission asks name the tool, allow with the original input."""
    prompt = FakeUserPrompt(answers=[_answered_choice("Allow once")])

    write_input: dict[str, object] = {"path": "x"}
    end = await q.ask_permission(
        typing.cast(typing.Any, prompt), "Write", write_input, "do it?"
    )

    assert end is q.PermissionEnd.ALLOWED
    assert list(prompt.asks[0].options) == ["Allow once", "Deny"]
    assert "Claude Code asks to use Write." in prompt.asks[0].message

    big = {"blob": "y" * 5000}
    prompt2 = FakeUserPrompt(answers=[_answered_choice("Deny")])
    big_input: dict[str, object] = dict(big)
    end2 = await q.ask_permission(
        typing.cast(typing.Any, prompt2), "Bash", big_input, None
    )
    assert end2 is q.PermissionEnd.DENIED
    assert "truncated" in prompt2.asks[0].message
    assert q.permission_question("Bash") == "Allow Claude Code to use Bash?"


def test_disallowed_names() -> None:
    """Parenthesised patterns block the whole tool."""
    assert q.disallowed_tool_names(["Write", "Bash(rm *)"]) == {"Write", "Bash"}
