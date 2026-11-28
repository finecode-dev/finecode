from __future__ import annotations

import dataclasses
import enum
import typing

from finecode_extension_api import service

__all__ = [
    "ElicitationOutcome",
    "ElicitationResult",
    "IUserPrompt",
    "PromptAvailability",
]


class ElicitationOutcome(enum.StrEnum):
    """How an ask ended.

    Three outcomes rather than two, because a handler that cannot tell "a person
    said no" from "no person was there" cannot decide what to do next. Refused
    is a decision and should usually be honoured — abort, or take the safe
    branch. Nobody-to-ask is the absence of a decision, and the right response
    is whatever the action would have done in a pipeline: apply a default, skip
    the interactive step, or fail loudly if the step was mandatory. Collapsing
    the two would make every action that asks either unusable in CI or unsafe
    with a person present.
    """

    ANSWERED = "answered"
    """A person chose one of the offered options; ``value`` holds it."""

    DECLINED = "declined"
    """A person was asked and refused, dismissed or cancelled the question."""

    UNAVAILABLE = "unavailable"
    """Nobody could be asked.

    The run has no reachable originating client, the client that started it
    cannot put questions to anyone (a CLI in a pipeline, an AI client without
    the capability), it disconnected while the question was outstanding, or the
    deadline passed with no answer. From the handler's point of view these are
    one situation: this run is not going to get a human decision.
    """


@dataclasses.dataclass(frozen=True)
class ElicitationResult:
    """The outcome of one ask, plus the answer when there is one."""

    outcome: ElicitationOutcome
    value: str | None = None
    """The chosen option, or the exact text that was submitted.

    Set only when ``outcome`` is ``ANSWERED``.

    Never filled in from the ask's ``default`` on any other outcome: a default
    the handler supplied is the handler's own fallback, and returning it as if a
    person had picked it would erase the distinction the outcomes exist for.
    """
    values: tuple[str, ...] | None = None
    """The chosen options, in the order they were offered.

    Set only for a multi-select ask whose ``outcome`` is ``ANSWERED``; ``()``
    is the empty selection. ``None`` on every other outcome and shape.
    """


QUESTION_MAX_DEADLINE_SEC: typing.Final = 900.0
"""Ceiling on how long a question waits for a person.

The WM clamps every ask's ``timeout_sec`` to this, and agent adapters size
their own deadlines against it, so the two cannot drift apart.
"""


@dataclasses.dataclass(frozen=True)
class PromptAvailability:
    """Whether a question could be put to a person in the current run right now."""

    can_ask: bool
    can_ask_text: bool = False
    """Whether a free-text question could be put to a person right now."""
    can_ask_multi_choice: bool = False
    """Whether a multi-select question could be put to a person right now."""


class IUserPrompt(service.Service, typing.Protocol):
    """Ask the person who started this run a question, and get their answer.

    Separate from ``IUserMessenger`` on purpose. That interface is three
    synchronous, fire-and-forget methods: they hand a string to whatever is
    connected and return immediately, having no way to fail and nothing to
    report. An ask is the opposite shape in every respect — it is asynchronous,
    it has one addressee rather than everyone connected, it carries a deadline,
    and it returns a value the handler acts on. Bolting it onto the messenger
    would give that interface two contracts and quietly make a "tell" look like
    it might block.

    **Not every stop point fits here.** A question with a small, enumerable set
    of answers does, and so does a short piece of free text — a name, a reason,
    a few lines at most. A decision that needs a person to *review* something
    substantial — a large diff, a set of proposed edits — is not improved by
    being squeezed into a prompt, and is better handed back between runs.

    **Availability is never assumed.** Whether anyone can be asked is settled
    before the question is sent, so an action that asks costs a fast typed
    answer in automation rather than a hang until the deadline. Implementations
    do not raise for any of the ways an ask can fail to be answered; they return
    :class:`ElicitationResult`.
    """

    async def ask_choice(
        self,
        message: str,
        options: list[str],
        *,
        default: str | None = None,
        descriptions: typing.Mapping[str, str] | None = None,
        timeout_sec: float = 300.0,
    ) -> ElicitationResult:
        """Put *message* to the person who started this run and let them pick.

        Args:
            message: The question, as a person should read it. It is displayed
                verbatim, so it should say what happens for each option.
            options: The answers accepted, in the order they should be offered.
                Closed set: an implementation treats anything else as no answer.
            default: What the handler would choose on its own. A display field
                for pre-selection; it is never returned as the answer unless a
                person actually picks it.
            descriptions: Extra detail per option, keyed by option text. A
                display field a surface may show beside the option; entries
                naming an unoffered option are ignored.
            timeout_sec: How long the handler is prepared to wait. The deadline
                is enforced by whoever holds the question — not here — and it
                may be capped; an answer arriving late is discarded rather than
                applied.

        Returns:
            An :class:`ElicitationResult`. Its ``outcome`` distinguishes an
            answer from a refusal from having nobody to ask. On ``ANSWERED``,
            ``value`` holds the chosen option and ``values`` is ``None``.
        """
        ...

    async def ask_text(
        self,
        message: str,
        *,
        multiline: bool = False,
        initial_value: str | None = None,
        placeholder: str | None = None,
        timeout_sec: float = 300.0,
    ) -> ElicitationResult:
        """Ask the person who started this run for a short piece of free text.

        Args:
            message: The question, as a person should read it.
            multiline: When true the surface offers several lines; otherwise one.
                A display field a surface may drop without the handler knowing.
            initial_value: An editable prefill placed in the answer field. When
                the person submits it unchanged, that text is the answer. A
                display field a surface may drop without the handler knowing.
                Longer than 65,536 characters it fails explicitly and is never
                truncated.
            placeholder: A hint shown beside the empty field, for example what
                a good answer looks like. A hint only: it is never returned as
                the answer. A surface may drop it without the handler knowing.
            timeout_sec: How long the handler is prepared to wait. The deadline
                is enforced by whoever holds the question — not here — and it
                may be capped; an answer arriving late is discarded rather than
                applied.

        Returns:
            An :class:`ElicitationResult`. On ``ANSWERED``, ``value`` holds the
            exact submitted text — ``""``, leading and trailing whitespace
            and line breaks included — and ``values`` is ``None``. An answer
            longer than 65,536 characters fails explicitly and is never
            truncated.
        """
        del message, multiline, initial_value, placeholder, timeout_sec
        return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)

    async def ask_multi_choice(
        self,
        message: str,
        options: list[str],
        *,
        default: list[str] | None = None,
        descriptions: typing.Mapping[str, str] | None = None,
        timeout_sec: float = 300.0,
    ) -> ElicitationResult:
        """Put *message* to the person who started this run and let them pick any.

        Args:
            message: The question, as a person should read it.
            options: The answers accepted, in the order they should be offered.
                Closed set: an implementation treats anything else as no answer.
                Empty or with a repeated option, the ask returns
                ``UNAVAILABLE`` without sending anything.
            default: A pre-selection, as option texts. A display field a surface
                may drop without the handler knowing; entries naming an
                unoffered option are ignored.
            descriptions: Extra detail per option, keyed by option text. A
                display field a surface may show beside the option; entries
                naming an unoffered option are ignored.
            timeout_sec: How long the handler is prepared to wait. The deadline
                is enforced by whoever holds the question — not here — and it
                may be capped; an answer arriving late is discarded rather than
                applied.

        Returns:
            An :class:`ElicitationResult`. On ``ANSWERED``, ``values`` holds the
            chosen options in the order they were offered — ``()`` for the
            empty selection — and ``value`` is ``None``.
        """
        del message, options, default, descriptions, timeout_sec
        return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)

    async def check_availability(self) -> PromptAvailability:
        """Say whether a question could be put to a person right now."""
        return PromptAvailability(can_ask=False)
