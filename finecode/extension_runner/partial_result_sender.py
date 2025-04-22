import asyncio
import collections.abc

from finecode_extension_api import code_action


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

    async def schedule_sending(
        self, token: int | str, value: code_action.RunActionResult
    ) -> None:
        if token not in self.results_scheduled_to_send_by_token:
            self.results_scheduled_to_send_by_token[token] = value
        else:
            self.results_scheduled_to_send_by_token[token].update(value)

        if self.scheduled_task is None:
            self.scheduled_task = asyncio.create_task(self._wait_and_send())

    async def send_all_immediately(self) -> None:
        if self.scheduled_task is not None:
            self.scheduled_task.cancel()
            self.scheduled_task = None

        self._send_all()

    async def _wait_and_send(self) -> None:
        await asyncio.sleep(self.wait_time_ms / 1000)
        self._send_all()
        self.scheduled_task = None

    def _send_all(self) -> None:
        while True:
            try:
                token, value = self.results_scheduled_to_send_by_token.popitem()
            except KeyError:
                break

            self.sender(token, value)
