# docs: docs/reference/actions.md
import dataclasses
import enum
import typing

from finecode_extension_api import code_action, textstyler


class AgentRunStatus(enum.StrEnum):
    """How an agent run ended.

    Distinguishes the ways a run can stop producing output, because a caller
    that only sees empty `output` cannot tell "the agent had nothing to say"
    from "the agent was never asked".
    """

    SETTLED = "settled"
    """The agent finished its work and nothing further is pending."""
    FAILED = "failed"
    """The backend stopped before settling, or exited with an error."""
    ABORTED = "aborted"
    """The caller cancelled the run."""
    REFUSED_INTERACTION = "refused_interaction"
    """The agent asked the user something and no answer was available.

    Distinct from `FAILED`: nothing went wrong, the run simply needed a human
    and was configured not to wait for one. This is the status that tells a
    non-interactive caller (CI) that its policy, not the agent, ended the run.
    """


_Number = typing.TypeVar("_Number", int, float)


def _add(left: _Number | None, right: _Number | None) -> _Number | None:
    """Sum two possibly-unreported figures.

    `None` means "not reported", so it is not `0`: two unreported figures stay
    unreported, and an unreported one alongside a reported one contributes
    nothing rather than erasing what is known. See `AgentRunUsage.combine` for
    why that asymmetry is safe in practice.
    """
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _same_or_none(left: str | None, right: str | None) -> str | None:
    """The value both sides agree on, or `None` if they name different things.

    A run that switched models has no single model, and naming one of them
    beside a cost that covers both would misattribute the cost.
    """
    if left is None or right is None:
        return left or right
    return left if left == right else None


@dataclasses.dataclass(frozen=True)
class AgentRunUsage:
    """What one agent run consumed, as reported by the backend that ran it.

    **Every field is optional and independently so.** Backends differ in what
    they expose -- one reports a full cache breakdown, another only a grand
    total, a locally hosted one has no prices at all -- so a partially filled
    instance is the normal case here, not a degraded one. `None` always means
    "this backend did not tell us", never "zero": a handler that fills a gap
    with `0` reports a run as free, or as having read no input, when the truth
    is that nobody said.

    Handlers report what the backend reported and nothing else. Deriving
    `total_tokens` by summing, pricing tokens against a table the handler
    carries, or converting currencies all produce a number indistinguishable
    from one the provider stood behind, and only one of those is worth acting
    on.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None

    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    """Not universal: several backends report a single "cached" figure with no
    read/write split, and some report none at all."""

    total_tokens: int | None = None
    """Reported as-is, never derived. It may exceed input plus output -- usage
    from the agent's own tools, from context compaction, from summarisation --
    and for some backends it is the only figure available."""

    approx_cost_usd: float | None = None
    """The backend's own estimate, in USD.

    Not a bill. It is that backend's price table applied to its own token
    counts: the table can be stale, absent for a model it does not know, and
    for a subscription-covered agent it is what the run *would* have cost at
    API rates rather than anything that was charged. A backend that prices in
    another currency reports `None` rather than converting.
    """

    provider: str | None = None
    model: str | None = None
    """What produced the figures above. Without them a cost cannot be compared
    against anything or explained to anyone."""

    def combine(self, other: "AgentRunUsage") -> "AgentRunUsage":
        """Add `other` to this usage, for backends that report incrementally.

        Most agent backends stream usage per message and leave the totalling to
        the client, so this lives on the type rather than being rewritten,
        subtly differently, in each backend's handler.

        Counters sum, with an unreported side contributing nothing. That
        understates if a backend reports usage for only *some* of its messages
        -- but reporting is a property of the backend, not of the individual
        message, so a partially reporting stream is not a real case. A handler
        that does encounter one should set the whole field to `None` itself
        rather than let this quietly produce a floor.
        """
        return AgentRunUsage(
            input_tokens=_add(self.input_tokens, other.input_tokens),
            output_tokens=_add(self.output_tokens, other.output_tokens),
            cache_read_tokens=_add(self.cache_read_tokens, other.cache_read_tokens),
            cache_write_tokens=_add(self.cache_write_tokens, other.cache_write_tokens),
            total_tokens=_add(self.total_tokens, other.total_tokens),
            approx_cost_usd=_add(self.approx_cost_usd, other.approx_cost_usd),
            provider=_same_or_none(self.provider, other.provider),
            model=_same_or_none(self.model, other.model),
        )

    def to_text(self) -> str:
        """One line naming only what was actually reported, or `""` if nothing
        was."""
        parts: list[str] = []

        if self.total_tokens is not None:
            parts.append(f"{self.total_tokens:,} tokens")
        elif self.input_tokens is not None or self.output_tokens is not None:
            # No total to show, so show the halves that exist rather than
            # adding them up into a total the backend never reported.
            counted = [
                f"{label} {value:,}"
                for label, value in (
                    ("in", self.input_tokens),
                    ("out", self.output_tokens),
                )
                if value is not None
            ]
            parts.append(" / ".join(counted))

        if self.approx_cost_usd is not None:
            parts.append(f"~${self.approx_cost_usd:.4f}")

        origin = "/".join(part for part in (self.provider, self.model) if part)
        if origin:
            parts.append(origin)

        return " · ".join(parts)


@dataclasses.dataclass
class RunAgentTaskRunPayload(code_action.RunActionPayload):
    prompt: str
    """The task for the agent, in natural language.

    Deliberately the only input. Which model runs it is handler config, not
    payload, so the same task definition is portable across setups.
    """


class RunAgentTaskRunContext(code_action.RunActionContext[RunAgentTaskRunPayload]): ...


@dataclasses.dataclass
class RunAgentTaskRunResult(code_action.RunActionResult):
    status: AgentRunStatus = AgentRunStatus.FAILED
    """Defaults to `FAILED` so a result that was never populated does not read
    as a successful empty run."""
    output: str = ""
    """The agent's final text."""
    turns: int | None = None
    """Assistant turns the agent took, and a sign of looping.

    `None` where the backend does not expose turn boundaries -- which is not
    the same as `0`, a run that took no turns at all.
    """
    usage: AgentRunUsage | None = None
    """What the run consumed, as far as the backend reported it.

    `None` means the backend reported nothing, which is distinct from an
    `AgentRunUsage()` with every field unset: that one says the backend
    answered and had nothing to give.

    Populated on failed, refused and timed-out runs too, not only settled
    ones. A run that spent real money and *then* failed is exactly when the
    number is worth having.
    """
    duration_sec: float | None = None
    """Wall-clock time the run took.

    Measured by the handler rather than reported by the backend, so unlike
    `usage` it is available whatever the backend is willing to say. `None` only
    where a result was built without one.
    """
    error: str | None = None
    """Why the run did not settle. `None` whenever `status` is `SETTLED`."""

    def update(self, other: code_action.RunActionResult) -> None:
        # Within-project merge only (R-302). This action takes exactly one
        # handler (see `RunAgentTaskAction`), so merging is last-writer-wins
        # rather than an accumulation -- there is no meaningful way to combine
        # two agents' independent attempts at one task.
        if not isinstance(other, RunAgentTaskRunResult):
            return

        self.status = other.status
        self.output = other.output
        self.turns = other.turns
        self.usage = other.usage
        self.duration_sec = other.duration_sec
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        if self.status is AgentRunStatus.SETTLED:
            body = self.output or "Agent settled without producing any output."
        else:
            body = f"[{self.status}] {self.error or 'no further detail'}"

        footer = self._cost_line()
        return f"{body}\n\n{footer}" if footer else body

    def _cost_line(self) -> str:
        """What the run cost, for a human reading the run go by.

        Rendered here rather than folded into `output`: `output` is the agent's
        own text and stays exactly that for anyone consuming it as data.
        """
        parts = [self.usage.to_text()] if self.usage is not None else []
        if self.duration_sec is not None:
            parts.append(f"{self.duration_sec:.1f}s")
        return " · ".join(part for part in parts if part)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.status is AgentRunStatus.SETTLED:
            return code_action.RunReturnCode.SUCCESS
        return code_action.RunReturnCode.ERROR


class RunAgentTaskAction(
    code_action.Action[
        RunAgentTaskRunPayload,
        RunAgentTaskRunContext,
        RunAgentTaskRunResult,
    ]
):
    """Delegate a task to an AI coding agent and return what it produced.

    Project-scoped: an agent task acts on one project's files, so per-project
    dispatch is correct rather than duplicative (R-108 -- trigger (d) does not
    apply, for the same reason it does not apply to `create_git_tag`).

    **Exactly one handler.** Unlike most actions, this one gains nothing from
    the declarative merge across handlers that the action model is built for:
    two agents independently attempting the same task and merging their outputs
    is meaningless, and both would write to the same files. Swap backends by
    *replacing* the registered handler in config, never by registering a second
    one. Nothing in the framework enforces this -- `update()` degrades to
    last-writer-wins if it is violated, which is a poor outcome, not a safe one.

    Handler contract:
    - Set `status` on every path; `FAILED` is the default so an unpopulated
      result never reads as success.
    - Populate `error` whenever `status` is not `SETTLED`.
    - Report execution narrative through progress, never the model's text
      (R-304) -- the text is result data and belongs in `output`.
    - Report `usage` with what the backend reported and nothing more, leaving
      what it did not report as `None`. Never derive a figure the backend did
      not give (see `AgentRunUsage`), and report usage on the failure paths as
      well as the settled one -- a run bills for what it spent before it broke.
    - Measure `duration_sec` itself. It is the one cost figure that does not
      depend on the backend being willing to report anything.

    Backends vary in what they can account for, and the result type is built
    for that: a handler that can report only elapsed time is a complete
    implementation of this contract, not a partial one.
    """

    DESCRIPTION = "Delegate a task to an AI coding agent and return its output."
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
    PAYLOAD_TYPE = RunAgentTaskRunPayload
    RUN_CONTEXT_TYPE = RunAgentTaskRunContext
    RESULT_TYPE = RunAgentTaskRunResult
