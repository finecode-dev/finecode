import asyncio
import collections.abc
import typing

from finecode_extension_api import code_action
from loguru import logger

from finecode_extension_runner import coverage_sink
from finecode_extension_runner._converter import converter as _converter


class PartialResultSender:
    # send partial results not more often than every `wait_time_ms` ms. Only the
    # last one can be sent immediately.
    def __init__(self, sender: collections.abc.Callable, wait_time_ms: int) -> None:
        self.sender = sender
        self.wait_time_ms = wait_time_ms

        self.scheduled_task: asyncio.Task | None = None
        self.results_scheduled_to_send_by_token: dict[
            str | int, code_action.RunActionResult
        ] = {}
        self.formats_by_token: dict[str | int, list[str]] = {}

    async def schedule_sending(
        self,
        token: int | str,
        value: code_action.RunActionResult,
        result_formats: list[str] | None = None,
    ) -> None:
        logger.trace(
            f"PartialResultSender: schedule_sending for token={token}, value_type={type(value).__name__}"
        )
        # Streamed side: fold the run's sink into this partial before it is
        # serialized. A streamed partial is sent mid-run, so the end-of-run
        # fold alone would never reach it — a bridge that sends a fresh result
        # (inspect_code's per-project blocks) would silently drop the miss.
        coverage_sink.fold_into(value)
        if token not in self.results_scheduled_to_send_by_token:
            # The accumulator retains the same object the handler passed, so
            # storing it here as well would merge every later send into one
            # object twice. Copy, with the same round-trip the async-generator
            # path performs — after the fold, so the fold is not normalized
            # away by the round-trip.
            self.results_scheduled_to_send_by_token[token] = typing.cast(
                code_action.RunActionResult,
                _converter.structure(_converter.unstructure(value), type(value)),
            )
        else:
            self.results_scheduled_to_send_by_token[token].update(value)
        if result_formats is not None:
            self.formats_by_token[token] = result_formats

        if self.scheduled_task is None:
            self.scheduled_task = asyncio.create_task(self._wait_and_send())

    async def send_all_immediately(self) -> None:
        logger.trace(
            f"PartialResultSender: send_all_immediately, pending_tokens={list(self.results_scheduled_to_send_by_token.keys())}"
        )
        if self.scheduled_task is not None:
            self.scheduled_task.cancel()
            self.scheduled_task = None

        self._send_all()

    async def _wait_and_send(self) -> None:
        await asyncio.sleep(self.wait_time_ms / 1000)
        self._send_all()
        self.scheduled_task = None

    def _send_all(self) -> None:
        count = 0
        while True:
            try:
                token, value = self.results_scheduled_to_send_by_token.popitem()
            except KeyError:
                break

            count += 1
            formats = self.formats_by_token.pop(token, None)
            logger.trace(f"PartialResultSender: _send_all sending token={token}")
            self.sender(token, value, formats)
        logger.trace(f"PartialResultSender: _send_all done, sent {count} results")
