"""Pure, synchronous log delivery primitives for streaming WM logs to clients.

No I/O, no asyncio: `conn` is any hashable connection handle. In production
this is an `asyncio.StreamWriter`; in tests it is any sentinel object.
"""

from __future__ import annotations

import dataclasses
import re
import time
from typing import Any, Callable

__all__ = [
    "LOG_LEVEL_VALUES",
    "level_value",
    "ClientLogRecord",
    "SENSITIVE_KEYWORDS",
    "REDACTED",
    "redact",
    "SubscriptionRegistry",
    "FlushCallback",
    "LogBatcher",
    "LOG_RECORDS_METHOD",
    "SUBSCRIBE_METHOD",
    "UNSUBSCRIBE_METHOD",
    "build_log_notification",
]

LOG_LEVEL_VALUES: dict[str, int] = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def level_value(name: str) -> int:
    """Case-insensitive lookup; unknown names fall back to INFO (20)."""
    return LOG_LEVEL_VALUES.get(name.upper(), LOG_LEVEL_VALUES["INFO"])


@dataclasses.dataclass
class ClientLogRecord:
    timestamp: float
    level: str
    source: str
    group: str
    message: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "source": self.source,
            "group": self.group,
            "message": self.message,
        }


SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "apitoken",
    "authorization",
    "credential",
    "credentials",
    "private_key",
)
REDACTED = "***REDACTED***"

_REDACT_PATTERN = re.compile(
    r"\b(?P<keyword>" + "|".join(re.escape(k) for k in SENSITIVE_KEYWORDS) + r")\b"
    r'(?P<postquote>"?)'
    r"(?P<sep>\s*[:=]\s*)"
    r'(?P<prequote>"?)'
    r"(?P<value>[^\s\"',}]+)",
    re.IGNORECASE,
)


def _redact_match(match: re.Match[str]) -> str:
    return (
        match.group("keyword")
        + match.group("postquote")
        + match.group("sep")
        + match.group("prequote")
        + REDACTED
    )


def redact(text: str) -> str:
    return _REDACT_PATTERN.sub(_redact_match, text)


class SubscriptionRegistry:
    def __init__(self) -> None:
        self._subscriptions: dict[Any, int] = {}

    def register(self, conn: Any, min_level: str) -> None:
        self._subscriptions[conn] = level_value(min_level)

    def unregister(self, conn: Any) -> None:
        self._subscriptions.pop(conn, None)

    def has_subscribers(self) -> bool:
        return bool(self._subscriptions)

    def min_level_value(self) -> int | None:
        if not self._subscriptions:
            return None
        return min(self._subscriptions.values())

    def subscribers_for(self, level: str) -> list[Any]:
        target = level_value(level)
        return [
            conn
            for conn, min_level_val in self._subscriptions.items()
            if min_level_val <= target
        ]

    def all_conns(self) -> list[Any]:
        return list(self._subscriptions.keys())


FlushCallback = Callable[[Any, list[dict[str, Any]], int], None]


class LogBatcher:
    def __init__(
        self,
        flush_callback: FlushCallback,
        *,
        interval_ms: int = 200,
        max_batch: int = 100,
        buffer_limit: int = 1000,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flush_callback = flush_callback
        self._interval_seconds = interval_ms / 1000.0
        self._max_batch = max_batch
        self._buffer_limit = buffer_limit
        self._now = now
        self._buffers: dict[Any, list[dict[str, Any]]] = {}
        self._dropped: dict[Any, int] = {}
        self._anchors: dict[Any, float] = {}

    def enqueue(self, conn: Any, record: ClientLogRecord) -> None:
        if conn not in self._anchors:
            self._anchors[conn] = self._now()
        buffer = self._buffers.setdefault(conn, [])
        buffer.append(record.to_wire())

        overflow = len(buffer) - self._buffer_limit
        if overflow > 0:
            del buffer[:overflow]
            self._dropped[conn] = self._dropped.get(conn, 0) + overflow

        if level_value(record.level) >= LOG_LEVEL_VALUES["ERROR"]:
            self.flush(conn)
            return
        if len(buffer) >= self._max_batch:
            self.flush(conn)
            return

    def tick(self) -> None:
        current = self._now()
        for conn in list(self._buffers.keys()):
            anchor = self._anchors.get(conn, current)
            if current - anchor >= self._interval_seconds:
                self.flush(conn)

    def flush(self, conn: Any) -> None:
        buffer = self._buffers.get(conn, [])
        dropped = self._dropped.get(conn, 0)
        if not buffer and dropped == 0:
            return
        self._buffers[conn] = []
        self._dropped[conn] = 0
        self._anchors[conn] = self._now()
        self._flush_callback(conn, buffer, dropped)

    def flush_all(self) -> None:
        for conn in list(self._buffers.keys()):
            self.flush(conn)

    def drop_conn(self, conn: Any) -> None:
        self._buffers.pop(conn, None)
        self._dropped.pop(conn, None)
        self._anchors.pop(conn, None)


LOG_RECORDS_METHOD = "server/logRecords"
SUBSCRIBE_METHOD = "server/subscribeLogs"
UNSUBSCRIBE_METHOD = "server/unsubscribeLogs"


def build_log_notification(
    records: list[dict[str, Any]], dropped_count: int
) -> dict[str, Any]:
    params: dict[str, Any] = {"records": records}
    if dropped_count > 0:
        params["droppedCount"] = dropped_count
    return params
