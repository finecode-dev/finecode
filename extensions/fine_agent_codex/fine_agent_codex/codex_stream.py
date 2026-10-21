"""Wire format for the Codex CLI in non-interactive mode (`codex exec`).

Kept separate from the handler so the protocol can be tested without spawning
a model, and so the places where the shape is surprising are documented in one
file.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import typing

from fine_agent.run_agent_task_action import AgentRunUsage

__all__ = [
    "CodexErrorFrame",
    "CodexEvent",
    "CodexEventKind",
    "CodexItemFrame",
    "CodexItemKind",
    "CodexUsageFrame",
    "CodexWireFrame",
    "JsonValue",
    "parse_event",
]

JsonValue: typing.TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)


class CodexUsageFrame(typing.TypedDict, total=False):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


class CodexErrorFrame(typing.TypedDict, total=False):
    message: str


class CodexItemFrame(typing.TypedDict, total=False):
    id: str
    type: str
    text: str


class CodexWireFrame(typing.TypedDict, total=False):
    type: str
    thread_id: str
    message: str
    error: CodexErrorFrame
    usage: CodexUsageFrame
    item: CodexItemFrame


class CodexEventKind(enum.StrEnum):
    THREAD_STARTED = "thread.started"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    ITEM_STARTED = "item.started"
    ITEM_UPDATED = "item.updated"
    ITEM_COMPLETED = "item.completed"
    ERROR = "error"


class CodexItemKind(enum.StrEnum):
    AGENT_MESSAGE = "agent_message"
    REASONING = "reasoning"
    COMMAND_EXECUTION = "command_execution"
    FILE_CHANGE = "file_change"
    MCP_TOOL_CALL = "mcp_tool_call"
    WEB_SEARCH = "web_search"
    TODO_LIST = "todo_list"
    ERROR = "error"


@dataclasses.dataclass(frozen=True)
class CodexEvent:
    kind: CodexEventKind
    thread_id: str | None = None
    assistant_text: str | None = None
    usage: AgentRunUsage | None = None
    error: str | None = None
    narrative: str | None = None


def _int_or_none(value: JsonValue | typing.Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_usage(raw: typing.Any) -> AgentRunUsage | None:
    if not isinstance(raw, dict):
        return None
    input_tokens = _int_or_none(raw.get("input_tokens"))
    output_tokens = _int_or_none(raw.get("output_tokens"))
    cached_input = _int_or_none(raw.get("cached_input_tokens"))
    if input_tokens is None and output_tokens is None and cached_input is None:
        return None
    return AgentRunUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached_input,
    )


def _parse_error_message(raw: typing.Any) -> str | None:
    if isinstance(raw, dict):
        message = raw.get("message")
        return message if isinstance(message, str) and message else None
    return None


def _narrative_for_item(item_type: str | None) -> str:
    try:
        kind = CodexItemKind(item_type) if item_type is not None else None
    except ValueError:
        kind = None
    if kind is CodexItemKind.COMMAND_EXECUTION:
        return "Running command"
    if kind is CodexItemKind.FILE_CHANGE:
        return "Applying file changes"
    if kind is CodexItemKind.MCP_TOOL_CALL:
        return "Calling MCP tool"
    if kind is CodexItemKind.WEB_SEARCH:
        return "Searching the web"
    if kind is CodexItemKind.TODO_LIST:
        return "Updating plan"
    if kind is CodexItemKind.REASONING:
        return "Reasoning"
    if kind is CodexItemKind.AGENT_MESSAGE:
        return "Writing answer"
    if kind is CodexItemKind.ERROR:
        return "Agent reported an error"
    return "Processing item"


def parse_event(line: str) -> CodexEvent | None:
    """Decode one stdout line, or `None` if it carries no actionable event."""
    if not line.strip():
        return None
    try:
        frame = json.loads(line)
    except ValueError:
        return None
    if not isinstance(frame, dict):
        return None
    raw_type = frame.get("type")
    if not isinstance(raw_type, str):
        return None
    try:
        kind = CodexEventKind(raw_type)
    except ValueError:
        return None

    if kind is CodexEventKind.THREAD_STARTED:
        thread_id = frame.get("thread_id")
        return CodexEvent(
            kind=kind,
            thread_id=thread_id if isinstance(thread_id, str) else None,
        )

    if kind is CodexEventKind.TURN_STARTED:
        return CodexEvent(kind=kind, narrative="Agent turn started")

    if kind is CodexEventKind.TURN_COMPLETED:
        return CodexEvent(kind=kind, usage=_parse_usage(frame.get("usage")))

    if kind is CodexEventKind.TURN_FAILED:
        message = _parse_error_message(frame.get("error")) or "turn failed"
        return CodexEvent(
            kind=kind, usage=_parse_usage(frame.get("usage")), error=message
        )

    if kind is CodexEventKind.ERROR:
        message = frame.get("message")
        diagnostic = (
            message
            if isinstance(message, str) and message
            else _parse_error_message(frame.get("error"))
        )
        return CodexEvent(kind=kind, error=diagnostic or "agent reported an error")

    item = frame.get("item")
    item_type: str | None = None
    text: str | None = None
    if isinstance(item, dict):
        raw_item_type = item.get("type")
        item_type = raw_item_type if isinstance(raw_item_type, str) else None
        raw_text = item.get("text")
        text = raw_text if isinstance(raw_text, str) else None
    narrative = _narrative_for_item(item_type)
    assistant_text: str | None = None
    if (
        kind is CodexEventKind.ITEM_COMPLETED
        and item_type == CodexItemKind.AGENT_MESSAGE
        and text is not None
    ):
        assistant_text = text
    return CodexEvent(kind=kind, assistant_text=assistant_text, narrative=narrative)
