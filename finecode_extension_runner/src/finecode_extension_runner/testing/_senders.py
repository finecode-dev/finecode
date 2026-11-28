from __future__ import annotations

from finecode_extension_api import code_action


class CollectingPartialResultSender(code_action.PartialResultSender):
    """PartialResultSender that accumulates every sent result for test assertions.

    ``sender.events`` holds each ``RunActionResult`` passed to ``send()``.
    ``Session.run_action`` wires this sender as the run's partial-result
    observer, so every object a handler passes to
    ``run_context.partial_result_sender.send()`` lands here in order — the
    identity copy a test asserts on, not an accumulated merge.
    """

    def __init__(self) -> None:
        self.events: list[code_action.RunActionResult] = []

    async def send(self, result: code_action.RunActionResult) -> None:
        self.events.append(result)


class CollectingProgressSender(code_action.ProgressSender):
    """ProgressSender that accumulates begin/report/end calls for test assertions.

    ``sender.events`` holds dicts with key ``type`` (``"begin"``, ``"report"``,
    ``"end"``) plus the fields passed at each call.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def begin(
        self,
        title: str,
        message: str | None = None,
        percentage: int | None = None,
        cancellable: bool = False,
        total: int | None = None,
    ) -> None:
        self.events.append(
            {
                "type": "begin",
                "title": title,
                "message": message,
                "percentage": percentage,
                "cancellable": cancellable,
                "total": total,
            }
        )

    async def report(
        self,
        message: str | None = None,
        percentage: int | None = None,
    ) -> None:
        self.events.append(
            {"type": "report", "message": message, "percentage": percentage}
        )

    async def end(self, message: str | None = None) -> None:
        self.events.append({"type": "end", "message": message})
