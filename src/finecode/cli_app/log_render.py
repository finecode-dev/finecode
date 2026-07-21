# docs: docs/wm-protocol.md
"""Pure rendering of ``server/logRecords`` notification params into CLI text lines.

No I/O — kept separate from run_cmd.py so it is trivially unit-testable.
"""

from __future__ import annotations


_USER_MESSAGE_LOG_LEVEL = {
    "ERROR": "ERROR",
    "WARNING": "WARNING",
    "INFO": "INFO",
    "LOG": "INFO",
    "DEBUG": "DEBUG",
}


def user_message_log_level(message_type: str) -> str:
    """Map a ``server/userMessage`` type (a ``user_messages.UserMessageType`` name)
    onto a loguru level name, so the message can be logged through the same sink as
    the CLI's own logs. ``LOG`` has no loguru equivalent and is treated as ``INFO``.
    """
    return _USER_MESSAGE_LOG_LEVEL.get(message_type, "INFO")


def render_log_records(params: dict) -> list[str]:
    """Render a ``server/logRecords`` notification's params into display lines."""
    lines = [
        f"[{r.get('level', '')}] {r.get('source', '')} {r.get('group', '')}: {r.get('message', '')}"
        for r in (params or {}).get("records", [])
    ]
    dropped = (params or {}).get("droppedCount")
    if dropped:
        lines.append(f"... dropped {dropped} log records")
    return lines


__all__ = ["render_log_records", "user_message_log_level"]
