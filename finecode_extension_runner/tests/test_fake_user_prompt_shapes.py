"""FakeUserPrompt records every shape from the same script queue (issue 983, A12)."""

from __future__ import annotations

from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationOutcome,
    ElicitationResult,
    PromptAvailability,
)

from finecode_extension_runner.testing import FakeUserPrompt, RecordedAsk


async def test_fake_answers_all_shapes_from_one_queue() -> None:
    prompt = FakeUserPrompt(
        answers=[
            ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="x"),
            ElicitationResult(outcome=ElicitationOutcome.ANSWERED, values=("a",)),
            ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="y"),
        ]
    )
    assert (await prompt.ask_text("Name?")).value == "x"
    assert (await prompt.ask_multi_choice("Pick?", ["a"])).values == ("a",)
    assert (await prompt.ask_choice("Pick?", ["a", "b"])).value == "y"


async def test_fake_records_each_ask() -> None:
    prompt = FakeUserPrompt()
    await prompt.ask_choice("c?", ["a", "b"], default="a", descriptions={"a": "A"})
    await prompt.ask_text("t?", multiline=True, initial_value="d", placeholder="p")
    await prompt.ask_multi_choice("m?", ["a"], default=["a"], descriptions={"a": "A"})
    assert [ask.kind for ask in prompt.asks] == ["choice", "text", "multiChoice"]
    choice, text, multi = prompt.asks
    assert isinstance(choice, RecordedAsk)
    assert choice.options == ("a", "b")
    assert text.multiline is True
    assert text.initial_value == "d"
    assert multi.default == ("a",)


async def test_fake_keeps_asked_for_choice() -> None:
    prompt = FakeUserPrompt(
        availability=PromptAvailability(can_ask=True, can_ask_text=True)
    )
    await prompt.ask_choice("c?", ["a"])
    assert prompt.asked == [("c?", ["a"])]
    assert (await prompt.check_availability()).can_ask_text is True
