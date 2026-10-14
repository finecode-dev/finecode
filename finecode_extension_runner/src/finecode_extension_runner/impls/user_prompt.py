from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from finecode_extension_api.interfaces.iprojectactionrunner import ActionRunCancelled
from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationOutcome,
    ElicitationResult,
    IUserPrompt,
)
from loguru import logger

from finecode_extension_runner import er_errors, run_context

__all__ = ["UserPrompt"]

_ELICIT = "finecode/elicit"


class UserPrompt(IUserPrompt):
    """Calls the WM back-channel ``finecode/elicit`` and never lets it fail loudly.

    The ER does not know who is connected, whether they can answer, or how long
    they may take -- the WM holds the run's originating connection and owns the
    deadline (ADR-0082 rules 1 and 4). So this implementation is only a
    translation layer: it sends the question and turns whatever comes back,
    including nothing at all, into an :class:`ElicitationResult`.

    **Nothing raises out of** :meth:`ask_choice`. A WM with no client-connection
    layer answers with a JSON-RPC error; a dead socket raises a transport error;
    a WM-side deadline returns "unavailable" as data. All three mean the same
    thing to a handler -- no human decision is coming -- and a handler that had
    to catch exceptions for that would be a handler nobody could run in
    automation (ADR-0082 rule 3).

    **Cancellation is the deliberate exception.** It means the whole run is
    being torn down rather than the ask failing, so it propagates instead of
    becoming an outcome — a handler told "nobody could be asked" would take its
    unattended branch and carry on working for a run that no longer exists. It
    reaches this layer two ways: as ``asyncio.CancelledError`` (a
    ``BaseException``, so the catch below never sees it) and as
    ``er_errors.WmCommunicationCancelled``, the WM's ``REQUEST_CANCELLED``
    translated by ``er_server`` — an ordinary ``Exception`` that must therefore
    be re-raised explicitly, as :class:`ActionRunCancelled` so handlers see the
    same cancellation type every other back-channel call raises.
    """

    def __init__(
        self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]] | None
    ) -> None:
        self._send = send_request_to_wm

    async def ask_choice(
        self,
        message: str,
        options: list[str],
        *,
        default: str | None = None,
        timeout_sec: float = 300.0,
    ) -> ElicitationResult:
        if not options:
            # A question with no answers cannot be put to anyone, and asking the
            # WM to try would only spend a round trip on finding that out.
            logger.warning("ask_choice called with no options; nobody can answer that")
            return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)
        if self._send is None:
            # No WM at all: an ER exercised standalone (tests, tooling). Nobody
            # to ask is exactly what that is.
            return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)

        try:
            raw = await self._send(
                _ELICIT,
                {
                    "message": message,
                    "options": list(options),
                    "default": default,
                    "timeoutSec": timeout_sec,
                    # Which run is asking. The WM resolves the person to put the
                    # question to from this and nothing else: the project this
                    # runner serves cannot identify a run, since two clients may
                    # be running the same project at the same moment.
                    "runId": run_context.current_run_id(),
                },
            )
        except er_errors.WmCommunicationCancelled as exception:
            # Not a failure to get an answer: the run itself is being cancelled,
            # and a handler that heard "nobody could be asked" would keep going.
            raise ActionRunCancelled(exception.message) from exception
        # The remaining failure modes are open-ended (transport errors, WM error
        # responses, serialization), and every one of them is the same answer to
        # the caller. Narrowing this would only turn a typed outcome back into
        # an exception for the cases nobody enumerated.
        except Exception as exception:  # noqa: BLE001
            logger.warning(f"Could not ask the user: {exception}")
            return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)

        return self._to_result(raw, options)

    @staticmethod
    def _to_result(raw: Any, options: list[str]) -> ElicitationResult:
        """Structure the WM's answer, treating anything unexpected as no answer."""
        if not isinstance(raw, dict):
            logger.warning(f"Unexpected elicitation response: {raw!r}")
            return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)

        outcome = raw.get("outcome")
        if outcome == ElicitationOutcome.DECLINED.value:
            return ElicitationResult(outcome=ElicitationOutcome.DECLINED)
        if outcome == ElicitationOutcome.ANSWERED.value:
            value = raw.get("value")
            if value in options:
                return ElicitationResult(
                    outcome=ElicitationOutcome.ANSWERED, value=value
                )
            # "Answered" with something that was never offered is not an answer
            # to this question, and passing it through would hand the handler a
            # value its own option set says cannot occur.
            logger.warning(
                f"Elicitation answered with {value!r}, which was not offered; "
                f"treating it as unanswered"
            )
        elif outcome != ElicitationOutcome.UNAVAILABLE.value:
            logger.warning(f"Unknown elicitation outcome {outcome!r}")
        return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)
