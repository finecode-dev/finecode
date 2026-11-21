"""Rate-limited partial snapshots for agent task handlers."""

from __future__ import annotations

import asyncio

from finecode_extension_api.code_action import PartialResultSender
from finecode_extension_api.interfaces import ilogger

from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskRunResult,
)

__all__ = ["SnapshotStream"]


class SnapshotStream:
    """Send changed running snapshots and one unthrottled terminal result."""

    def __init__(
        self,
        sender: PartialResultSender,
        *,
        min_interval_sec: float | None,
        logger: ilogger.ILogger,
    ) -> None:
        self._sender = sender
        self._logger = logger
        self._min_interval_sec = min_interval_sec
        self._last_text: str | None = None
        self._last_sent_at: float | None = None

    async def update(self, text: str, *, message_end: bool = False) -> None:
        """Send a changed text snapshot when its flush condition is met."""
        if text == self._last_text:
            return
        now = asyncio.get_running_loop().time()
        if (
            not message_end
            and self._min_interval_sec is not None
            and self._last_sent_at is not None
            and now - self._last_sent_at < self._min_interval_sec
        ):
            return
        snapshot = RunAgentTaskRunResult(
            status=AgentRunStatus.RUNNING,
            output=text,
        )
        try:
            await self._sender.send(snapshot)
        except Exception as exception:
            self._logger.error(f"Failed to send agent task snapshot: {exception!r}")
            return
        self._last_text = text
        self._last_sent_at = asyncio.get_running_loop().time()

    async def finish(self, result: RunAgentTaskRunResult) -> RunAgentTaskRunResult:
        """Send the terminal result without rate limiting and return it."""
        try:
            await self._sender.send(result)
        except Exception as exception:
            self._logger.error(
                f"Failed to send terminal agent task result: {exception!r}"
            )
        return result
