"""Wire format for the Claude Code CLI in print mode (``claude -p``).

Kept separate from the handler so the protocol can be tested against a fake
``claude`` without spawning a model, and so the places where the shape is
surprising are documented in one file.

Four things about this stream are not what a reader would assume:

- **The ``result`` frame is the outcome, not the exit code.** It carries
  ``subtype`` (``success``/``error_max_turns``/``error_during_execution``/...)
  and ``is_error``; a run that ends without one was killed or died mid-flight,
  whatever the exit status says.
- **``result.result`` is the final answer, already assembled.** The ``assistant``
  frames stream the same text interleaved with ``tool_use`` and thinking blocks,
  so they are a fallback for runs that never reach a ``result``, not the primary
  source.
- **``result.usage`` is not the run's usage.** It reports the *last* request to
  the main model only. ``modelUsage`` is the per-model total for the whole run,
  including the auxiliary models the CLI uses for its own bookkeeping -- a run
  whose main model spent 2 input tokens can have spent 500 more elsewhere.
- **Frame types beyond these four exist and will keep appearing**
  (``rate_limit_event`` today, more later). Unknown types are ignored rather
  than treated as errors, so a CLI upgrade does not break the handler.

This module is where the CLI's wire shape stops: it converts to the action's
own types (``AgentRunUsage``) rather than handing dictionaries upward, so the
handler contains no knowledge of the CLI's field names.
"""

import dataclasses
import enum
import json
from typing import Any

from fine_agent.run_agent_task_action import AgentRunUsage


class ClaudeEvent(enum.StrEnum):
    """Frame ``type`` values this integration reacts to. Not exhaustive."""

    SYSTEM = "system"
    ASSISTANT = "assistant"
    USER = "user"
    RESULT = "result"


_SUCCESS_SUBTYPE = "success"


@dataclasses.dataclass(frozen=True)
class RunResult:
    """The ``result`` frame, in the terms the handler decides an outcome in."""

    settled: bool
    """``subtype`` was ``success`` *and* ``is_error`` was not set. Both are
    checked: they are separate fields and a future subtype may be neither
    clearly a success nor clearly a failure."""
    output: str
    turns: int | None
    usage: AgentRunUsage | None
    error: str | None
    """Why the run did not settle, or `None` when it did."""
    denied_tools: tuple[str, ...]
    """Tools the CLI refused to run because nothing could approve them.

    Empty on a run that needed no approval *and* on one whose permission mode
    granted it up front -- the two are indistinguishable here, and both are
    fine. A non-empty list on a failed run is the closest thing the CLI reports
    to "this needed a human"."""
    structured_output: Any = None
    """The data of the run's last structured-output call, or `None`.

    Present only on a settled run whose requested schema the CLI actually
    applied; a settled run without it is a failure the handler reports.
    """


def parse_frame(line: str) -> dict[str, Any] | None:
    """Decode one stdout line, or `None` if it is not a JSON object.

    The CLI documents strict JSONL on this path, but a stray warning on stdout
    must not end a run -- the caller logs and skips.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        frame = json.loads(stripped)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


def session_id(frame: dict[str, Any]) -> str | None:
    """The session the run is recorded under.

    Worth logging: the CLI persists the transcript, so this is how a human
    inspects afterwards what an agent did to their files (``claude --resume``).
    Every frame carries it; the ``system``/``init`` one arrives first.
    """
    value = frame.get("session_id")
    return value if isinstance(value, str) else None


def _content_blocks(frame: dict[str, Any]) -> list[dict[str, Any]]:
    message = frame.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def assistant_text(frame: dict[str, Any]) -> str:
    """The text an ``assistant`` frame carries, ignoring its other blocks.

    Thinking and tool-call blocks ride the same message and are not output, so
    they must not be appended to the answer.
    """
    return "".join(
        block.get("text", "")
        for block in _content_blocks(frame)
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def assistant_tool_names(frame: dict[str, Any]) -> list[str]:
    """Tools this assistant turn invoked, for progress reporting."""
    return [
        str(block.get("name") or "unknown")
        for block in _content_blocks(frame)
        if block.get("type") == "tool_use"
    ]


def _int_or_none(value: Any) -> int | None:
    """A token count, or `None` if the CLI did not report one.

    `bool` is excluded explicitly because it is an `int` in Python, and a stray
    `true` on the wire silently becoming `1` token is exactly the kind of
    made-up figure `AgentRunUsage` exists to keep out.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value)


def _sum_reported(values: list[int | None]) -> int | None:
    """Total across the models that reported, or `None` if none did.

    Adding the same figure across models is not the derivation `AgentRunUsage`
    forbids: every term was reported by the backend and they measure the same
    thing, whereas a derived `total_tokens` would be a figure nobody stood
    behind. `total_tokens` is therefore still left unset below -- the CLI
    reports no total, and input plus output would not be one anyway.
    """
    reported = [value for value in values if value is not None]
    return sum(reported) if reported else None


def _single(values: list[str | None]) -> str | None:
    """The one value everything agrees on, or `None` if they differ.

    A run that used two models has no single model, and naming one of them
    beside a cost covering both would misattribute the cost.
    """
    distinct = {value for value in values if value}
    return distinct.pop() if len(distinct) == 1 else None


def _usage_from_model_usage(
    model_usage: dict[str, Any], cost_usd: float | None
) -> AgentRunUsage | None:
    """Whole-run usage, totalled over the models the CLI reports per model.

    Preferred over the frame's own ``usage``, which covers only the last request
    to the main model. A run that delegates to a cheaper model for its internal
    bookkeeping spends real tokens there, and they appear only here.
    """
    entries = [entry for entry in model_usage.values() if isinstance(entry, dict)]
    if not entries:
        return None

    return AgentRunUsage(
        input_tokens=_sum_reported(
            [_int_or_none(e.get("inputTokens")) for e in entries]
        ),
        output_tokens=_sum_reported(
            [_int_or_none(e.get("outputTokens")) for e in entries]
        ),
        cache_read_tokens=_sum_reported(
            [_int_or_none(e.get("cacheReadInputTokens")) for e in entries]
        ),
        cache_write_tokens=_sum_reported(
            [_int_or_none(e.get("cacheCreationInputTokens")) for e in entries]
        ),
        approx_cost_usd=cost_usd,
        provider=_single([e.get("provider") for e in entries]),
        model=_single([e.get("canonicalModel") for e in entries]),
    )


def _usage_from_result_usage(
    usage: dict[str, Any], cost_usd: float | None, model: str | None
) -> AgentRunUsage:
    """Fallback for a ``result`` frame that reports no ``modelUsage``.

    Understates a run that used more than one model, which is why it is the
    fallback -- but it is what the backend said, and reporting nothing at all
    would lose a real figure.
    """
    return AgentRunUsage(
        input_tokens=_int_or_none(usage.get("input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
        cache_read_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int_or_none(usage.get("cache_creation_input_tokens")),
        approx_cost_usd=cost_usd,
        model=model,
    )


def result_usage(frame: dict[str, Any], model: str | None) -> AgentRunUsage | None:
    cost = _float_or_none(frame.get("total_cost_usd"))

    model_usage = frame.get("modelUsage")
    if isinstance(model_usage, dict):
        usage = _usage_from_model_usage(model_usage, cost)
        if usage is not None:
            return usage

    reported = frame.get("usage")
    if isinstance(reported, dict):
        return _usage_from_result_usage(reported, cost, model)

    # Nothing but a price, which is still worth reporting: it is the figure a
    # caller is most likely to be asked about.
    return (
        AgentRunUsage(approx_cost_usd=cost, model=model) if cost is not None else None
    )


def _denied_tools(frame: dict[str, Any]) -> tuple[str, ...]:
    denials = frame.get("permission_denials")
    if not isinstance(denials, list):
        return ()
    return tuple(
        str(denial.get("tool_name") or denial.get("tool") or "unknown")
        for denial in denials
        if isinstance(denial, dict)
    )


def _failure_reason(frame: dict[str, Any]) -> str:
    """Why a non-settling run stopped, named as precisely as the frame allows.

    The subtype is always present and always the most specific label the CLI
    has; the other two fields are where a cause, when there is one, is written.
    """
    subtype = str(frame.get("subtype") or "unknown")
    detail = frame.get("api_error_status") or frame.get("result")
    detail = detail.strip() if isinstance(detail, str) else ""
    return (
        f"claude ended with {subtype}: {detail}"
        if detail
        else (f"claude ended with {subtype}")
    )


def parse_result(frame: dict[str, Any], model: str | None) -> RunResult:
    """Read the terminal ``result`` frame."""
    settled = frame.get("subtype") == _SUCCESS_SUBTYPE and not frame.get("is_error")
    output = frame.get("result")

    return RunResult(
        settled=settled,
        # On a failed run the same field holds the error message rather than an
        # answer, and it is reported as `error` instead -- see `_failure_reason`.
        output=output if settled and isinstance(output, str) else "",
        turns=_int_or_none(frame.get("num_turns")),
        usage=result_usage(frame, model),
        error=None if settled else _failure_reason(frame),
        denied_tools=_denied_tools(frame),
        # Only a settled run's answer is read: on a failed subtype the field is
        # absent, and returning one would contradict the failure.
        structured_output=frame.get("structured_output") if settled else None,
    )
