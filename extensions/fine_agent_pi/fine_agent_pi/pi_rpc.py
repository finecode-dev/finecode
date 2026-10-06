"""Wire format for pi's RPC mode (`pi --mode rpc`).

Kept separate from the handler so the protocol can be tested against a fake pi
without spawning a model, and so the places where pi's shape is surprising are
documented in one file.

Three things about this protocol are not what a reader would assume:

- **It is not JSON-RPC.** Commands are bare ``{"type": ...}`` objects written to
  stdin; replies are ``{"type": "response", "command": ..., "success": ...}``
  carrying back an ``id`` that the *client* chose. There is no ``jsonrpc``
  field, no method/params envelope.
- **``agent_settled``, not ``agent_end``, means the run is over.** ``agent_end``
  fires when one low-level run finishes and carries ``willRetry``; after a
  transient provider error it fires again. Finishing on it truncates the run.
- **A failed model call still settles, and still exits 0.** A provider error is
  reported only as ``stopReason: "error"`` plus ``errorMessage`` inside the
  ``message_end``/``turn_end`` frames; the run then emits ``agent_settled`` and
  the process exits ``0``. Nothing outside those two frames distinguishes a run
  that answered from one that never reached the model.
- **UI replies are not uniformly shaped.** ``confirm`` answers with
  ``confirmed``; ``select``/``input``/``editor`` answer with ``value``. A single
  generic responder gets ``confirm`` wrong.
- **The same usage figures arrive twice.** ``message_end`` and ``turn_end``
  carry the same ``message``, so a reader that accumulates from both counts
  every message twice and doubles the reported bill.

This module is where pi's wire shape stops: it converts to the action's own
types (``AgentRunUsage``) rather than handing dictionaries upward, so the
handler contains no knowledge of pi's field names.
"""

import dataclasses
import enum
import json
from typing import Any

from fine_agent.run_agent_task_action import AgentRunUsage


class PiEvent(enum.StrEnum):
    """Event ``type`` values this integration reacts to.

    Not exhaustive -- pi emits more, and unknown types are ignored rather than
    treated as errors so a pi upgrade does not break the handler.
    """

    AGENT_SETTLED = "agent_settled"
    AGENT_END = "agent_end"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_END = "tool_execution_end"
    AUTO_RETRY_START = "auto_retry_start"
    COMPACTION_START = "compaction_start"
    EXTENSION_UI_REQUEST = "extension_ui_request"
    RESPONSE = "response"


class UiMethod(enum.StrEnum):
    """UI request methods that expect a reply."""

    SELECT = "select"
    CONFIRM = "confirm"
    INPUT = "input"
    EDITOR = "editor"


FIRE_AND_FORGET_UI_METHODS = frozenset(
    {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}
)
"""UI methods pi emits without waiting for anything.

Enumerated rather than inferred as "not in `UiMethod`", because those are three
different cases and only two of them are safe. A method that is neither known
to expect a reply nor known to be fire-and-forget is a method this integration
does not understand, and the safe response to it is to refuse -- not to assume
silence is acceptable. Inferring would silently route every future pi dialog
into the do-nothing branch, where pi's own timeout resolves it with a default
nobody chose.
"""


_SESSION_STATS = "get_session_stats"
"""The command name, which is also how its response is recognised."""


class UiDisposition(enum.StrEnum):
    REPLY = "reply"
    IGNORE = "ignore"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class UiRequest:
    request_id: str
    method: str
    """Raw string, not `UiMethod`: an unrecognised method must survive parsing
    so the handler can fail closed on it rather than crash here. Empty means the
    frame carried no usable method at all -- also fail-closed, via `UNKNOWN`."""
    title: str
    timeout_ms: int | None
    """How long pi will wait before auto-resolving with its own default. The
    reason answering is mandatory rather than optional."""

    @property
    def disposition(self) -> UiDisposition:
        if self.method in tuple(UiMethod):
            return UiDisposition.REPLY
        if self.method in FIRE_AND_FORGET_UI_METHODS:
            return UiDisposition.IGNORE
        return UiDisposition.UNKNOWN


def parse_frame(line: str) -> dict[str, Any] | None:
    """Decode one stdout line, or `None` if it is not a JSON object.

    pi documents strict JSONL, but a stray banner or a warning on stdout must
    not end a run -- the caller logs and skips.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        frame = json.loads(stripped)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


def parse_ui_request(frame: dict[str, Any]) -> UiRequest | None:
    """Read one `extension_ui_request`, or `None` if it cannot be answered.

    Only a missing `id` is unanswerable -- a reply is addressed by it, so there
    is nothing to send. A missing or non-string `method` is a *different* case
    and must not collapse into the same `None`: staying silent there lets pi's
    own timeout resolve the dialog with its default, which is the silent
    auto-approval this integration refuses everywhere else. It is carried
    through as an empty method instead, which `disposition` reports as
    `UNKNOWN` and the handler declines explicitly.
    """
    request_id = frame.get("id")
    if not isinstance(request_id, str):
        return None

    method = frame.get("method")
    timeout = frame.get("timeout")
    return UiRequest(
        request_id=request_id,
        method=method if isinstance(method, str) else "",
        title=str(frame.get("title") or ""),
        timeout_ms=timeout if isinstance(timeout, int) else None,
    )


def text_delta(frame: dict[str, Any]) -> str | None:
    """The assistant text carried by a ``message_update``, if it carries any.

    Thinking and tool-call deltas ride the same event and are not output, so
    they must not be appended to the answer.
    """
    event = frame.get("assistantMessageEvent")
    if not isinstance(event, dict) or event.get("type") != "text_delta":
        return None
    delta = event.get("delta")
    return delta if isinstance(delta, str) else None


def will_retry(frame: dict[str, Any]) -> bool:
    return bool(frame.get("willRetry"))


def message_error(frame: dict[str, Any]) -> str | None:
    """The provider error carried by a ``message_end``/``turn_end``, if any.

    This is the *only* place a failed model call is reported: the run settles
    normally afterwards and pi exits ``0``, so a caller that does not read this
    cannot tell an unanswered run from an answered one.

    The provider and model are folded into the message because the error text
    alone does not say which backend produced it, and the backend is usually
    the thing that has to be fixed -- pi picks its own default when FineCode
    configures neither.
    """
    message = frame.get("message")
    if not isinstance(message, dict) or message.get("stopReason") != "error":
        return None

    detail = message.get("errorMessage")
    detail = (
        detail.strip()
        if isinstance(detail, str) and detail.strip()
        else "no further detail"
    )

    origin = "/".join(
        str(part) for part in (message.get("provider"), message.get("model")) if part
    )
    return f"{origin}: {detail}" if origin else detail


def _int_or_none(value: Any) -> int | None:
    """A token count, or `None` if pi did not report one.

    `bool` is excluded explicitly because it is an `int` in Python, and a
    stray `true` on the wire silently becoming `1` token is exactly the kind
    of made-up figure `AgentRunUsage` exists to keep out.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cost_or_none(value: Any, total_tokens: int | None) -> float | None:
    """pi's cost figure, or `None` when it is a placeholder rather than a price.

    pi always emits a cost, so an unpriced model is indistinguishable from a
    free one at the wire level: both report `0`. Tokens spent at a cost of
    exactly zero is read as "pi has no price for this model" and reported as
    unknown. That is also the honest reading for a locally hosted model, where
    the run is free to the provider but not costless, and where a confident
    `$0.0000` would be the more misleading of the two answers.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value == 0 and total_tokens:
        return None
    return float(value)


def message_usage(frame: dict[str, Any]) -> AgentRunUsage | None:
    """Usage for one assistant message, from a ``message_end`` frame.

    Read only from ``message_end`` -- ``turn_end`` repeats the same message,
    and ``message_update`` carries a *cumulative* figure for the message in
    progress that pi documents as possibly staying zero until the message
    completes. Accumulating either one double-counts or under-counts.

    This is the fallback source. It cannot see usage that pi's own tools and
    its context compaction spent, so `parse_session_stats` supersedes it
    whenever the run survives long enough to ask.
    """
    message = frame.get("message")
    if not isinstance(message, dict):
        return None

    usage = message.get("usage")
    provider = message.get("provider")
    model = message.get("model")
    if not isinstance(usage, dict):
        # A message with no usage at all still names the backend, which is
        # worth keeping: it is what a later cost figure gets attributed to.
        if provider is None and model is None:
            return None
        usage = {}

    total = _int_or_none(usage.get("totalTokens"))
    cost = usage.get("cost")
    return AgentRunUsage(
        input_tokens=_int_or_none(usage.get("input")),
        output_tokens=_int_or_none(usage.get("output")),
        cache_read_tokens=_int_or_none(usage.get("cacheRead")),
        cache_write_tokens=_int_or_none(usage.get("cacheWrite")),
        total_tokens=total,
        approx_cost_usd=(
            _cost_or_none(cost.get("total"), total) if isinstance(cost, dict) else None
        ),
        provider=provider if isinstance(provider, str) else None,
        model=model if isinstance(model, str) else None,
    )


def parse_session_stats(frame: dict[str, Any]) -> AgentRunUsage | None:
    """Session totals from a ``get_session_stats`` response.

    Preferred over summing `message_usage`: pi documents these totals as
    covering usage reported by tools and by compaction/branch-summary
    generation as well as the assistant messages, none of which appears in the
    message frames.

    Carries no `provider`/`model` -- the response does not include them, and
    inventing them from elsewhere in this function would attribute a
    whole-session cost to whichever backend happened to be asked. The caller
    holds that identity and reattaches it.
    """
    if not frame.get("success", False):
        return None

    data = frame.get("data")
    if not isinstance(data, dict):
        return None

    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    total = _int_or_none(tokens.get("total"))

    return AgentRunUsage(
        input_tokens=_int_or_none(tokens.get("input")),
        output_tokens=_int_or_none(tokens.get("output")),
        cache_read_tokens=_int_or_none(tokens.get("cacheRead")),
        cache_write_tokens=_int_or_none(tokens.get("cacheWrite")),
        total_tokens=total,
        approx_cost_usd=_cost_or_none(data.get("cost"), total),
    )


def is_session_stats_response(frame: dict[str, Any]) -> bool:
    """Whether a ``response`` frame answers our stats request.

    Matched on ``command`` rather than on an id we chose: the documented
    request carries no id, so ``command`` is the only correlator pi echoes.
    """
    return frame.get("command") == _SESSION_STATS


def encode(command: dict[str, Any]) -> str:
    """Serialise one command, LF-terminated.

    ``ensure_ascii`` is left on: the transport is bytes and the receiver splits
    on ``\\n``, so escaping non-ASCII removes any question of a multi-byte
    sequence interacting with framing.
    """
    return json.dumps(command) + "\n"


def prompt_command(message: str, request_id: str = "prompt-1") -> str:
    return encode({"id": request_id, "type": "prompt", "message": message})


def abort_command() -> str:
    return encode({"type": "abort"})


def session_stats_command() -> str:
    return encode({"type": _SESSION_STATS})


_CONFIRM_YES = frozenset({"yes", "y", "true", "1", "confirm", "ok", "allow"})
_CONFIRM_NO = frozenset({"no", "n", "false", "0", "deny", "reject"})


def parse_confirm_answer(policy: str) -> bool | None:
    """Read a configured `confirm` policy as the boolean pi expects.

    `None` means the policy is not a yes/no answer at all, which the caller must
    fail closed on: a `confirm` dialog has no free-text answer to fall back to,
    so the alternative to refusing is guessing which way the user meant it.
    """
    normalized = policy.strip().lower()
    if normalized in _CONFIRM_YES:
        return True
    if normalized in _CONFIRM_NO:
        return False
    return None


def ui_response(request: UiRequest, *, cancelled: bool, value: Any = None) -> str:
    """Build the reply for one UI request, shaped for its method.

    `cancelled` is the universal decline. Otherwise `confirm` carries
    `confirmed` and every other method carries `value` -- getting this wrong is
    silent, because pi falls back to its own default on a malformed reply the
    same way it does on no reply at all.

    A `confirm` answer must already be a `bool` (see `parse_confirm_answer`).
    Coercing here with `bool(value)` would answer *yes* to every non-empty
    string, `"no"` included, turning a configured refusal into an approval on
    what is usually a tool-approval gate.
    """
    reply: dict[str, Any] = {
        "type": "extension_ui_response",
        "id": request.request_id,
    }
    if cancelled:
        reply["cancelled"] = True
    elif request.method == UiMethod.CONFIRM:
        if not isinstance(value, bool):
            raise TypeError(
                f"confirm reply needs a bool answer, got {value!r}; "
                "use parse_confirm_answer on the configured policy"
            )
        reply["confirmed"] = value
    else:
        reply["value"] = value
    return encode(reply)
