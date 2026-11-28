from __future__ import annotations

import pytest

from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationOutcome,
    ElicitationResult,
    IUserPrompt,
    PromptAvailability,
)


class MinimalPrompt:
    async def ask_choice(self, message, options, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def check_availability(self):  # type: ignore[no-untyped-def]
        return PromptAvailability(can_ask=False)


async def test_unoverridden_new_methods_return_unavailable() -> None:
    prompt = IUserPrompt.__new__(IUserPrompt)  # type: ignore[abstract]
    # Call the default implementations directly.
    text_result = await IUserPrompt.ask_text(prompt, "Name?")  # type: ignore[abstract]
    multi_result = await IUserPrompt.ask_multi_choice(prompt, "Pick?", ["a"])  # type: ignore[abstract]
    assert text_result.outcome is ElicitationOutcome.UNAVAILABLE
    assert text_result.value is None
    assert text_result.values is None
    assert multi_result.outcome is ElicitationOutcome.UNAVAILABLE
    assert multi_result.value is None
    assert multi_result.values is None


def test_elicitation_result_defaults_and_positional() -> None:
    result = ElicitationResult(ElicitationOutcome.ANSWERED, "a")
    assert result.values is None
    positional = ElicitationResult(ElicitationOutcome.ANSWERED, "a")
    assert positional.value == "a"
    assert positional.values is None
    assert PromptAvailability(can_ask=True).can_ask_text is False
    assert PromptAvailability(can_ask=True).can_ask_multi_choice is False
