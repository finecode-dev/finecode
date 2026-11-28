"""Tests for rate-limited, replaceable agent task snapshots."""

from __future__ import annotations

import asyncio
import typing
import unittest.mock

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger
from finecode_extension_runner.testing import run_on_virtual_time

from fine_agent.run_agent_task_action import AgentRunStatus, RunAgentTaskRunResult
from fine_agent.snapshot_stream import SnapshotStream

_logger = typing.cast(ilogger.ILogger, unittest.mock.MagicMock())


class _RecordingSender:
    def __init__(self) -> None:
        self.results: list[RunAgentTaskRunResult] = []

    async def send(self, result: code_action.RunActionResult) -> None:
        self.results.append(typing.cast(RunAgentTaskRunResult, result))


def test_update_sends_changed_snapshots_on_interval_or_message_end() -> None:
    sender = _RecordingSender()
    stream = SnapshotStream(sender, min_interval_sec=2.0, logger=_logger)

    async def scenario() -> None:
        await stream.update("first")
        await stream.update("second")
        await asyncio.sleep(2.0)
        await stream.update("second")
        await stream.update("third", message_end=True)
        await stream.update("third", message_end=True)

    _ = run_on_virtual_time(scenario())

    assert [result.output for result in sender.results] == ["first", "second", "third"]
    assert all(result.status is AgentRunStatus.RUNNING for result in sender.results)
    assert all(
        result.usage is None and result.session is None for result in sender.results
    )


def test_update_inside_interval_waits_for_later_update() -> None:
    sender = _RecordingSender()
    stream = SnapshotStream(sender, min_interval_sec=2.0, logger=_logger)

    async def scenario() -> None:
        await stream.update("first")
        await stream.update("shorter")
        await asyncio.sleep(2.0)
        await stream.update("shorter")

    _ = run_on_virtual_time(scenario())

    assert [result.output for result in sender.results] == ["first", "shorter"]


def test_finish_sends_terminal_result_without_rate_limit() -> None:
    sender = _RecordingSender()
    stream = SnapshotStream(sender, min_interval_sec=10.0, logger=_logger)
    terminal = RunAgentTaskRunResult(
        status=AgentRunStatus.FAILED,
        output="",
        error="timed out",
    )

    async def scenario() -> RunAgentTaskRunResult:
        await stream.update("partial")
        return await stream.finish(terminal)

    result = run_on_virtual_time(scenario())

    assert result is terminal
    assert sender.results == [
        RunAgentTaskRunResult(status=AgentRunStatus.RUNNING, output="partial"),
        terminal,
    ]


def test_sender_failure_is_swallowed_but_cancellation_propagates() -> None:
    class FailingSender:
        def __init__(self, failure: Exception | asyncio.CancelledError) -> None:
            self.failure = failure

        async def send(self, result: code_action.RunActionResult) -> None:
            _ = result
            raise self.failure

    async def scenario() -> None:
        failed = SnapshotStream(
            FailingSender(RuntimeError("offline")),
            min_interval_sec=None,
            logger=_logger,
        )
        await failed.update("snapshot")

        cancelled = SnapshotStream(
            FailingSender(asyncio.CancelledError()),
            min_interval_sec=None,
            logger=_logger,
        )
        with pytest.raises(asyncio.CancelledError):
            await cancelled.update("snapshot")

    _logger.reset_mock()
    _ = run_on_virtual_time(scenario())
    cast_logger = typing.cast(unittest.mock.MagicMock, _logger)
    cast_logger.error.assert_called_once()
