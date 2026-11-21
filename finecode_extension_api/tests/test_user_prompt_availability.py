"""An older runner that lacks ``check_availability`` reads as "cannot" (C26).

``check_availability`` was added after ``ask_choice``. A third-party
implementation that subclasses ``IUserPrompt`` without overriding it must not
fail when an adapter probes it: the Protocol body answers "cannot", which is
the fail-safe direction.
"""

from __future__ import annotations

from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationResult,
    IUserPrompt,
    PromptAvailability,
)


class _LegacyPrompt(IUserPrompt):
    """An implementation from before ``check_availability`` existed."""

    async def init(self) -> None:
        pass

    async def ask_choice(
        self,
        message: str,
        options: list[str],
        *,
        default: str | None = None,
        timeout_sec: float = 300.0,
    ) -> ElicitationResult:
        raise AssertionError("this probe must not send a question")


async def test_a_prompt_without_the_method_reads_as_cannot() -> None:
    """The Protocol default answers without sending anything or raising."""
    result = await _LegacyPrompt().check_availability()

    assert result == PromptAvailability(can_ask=False)
