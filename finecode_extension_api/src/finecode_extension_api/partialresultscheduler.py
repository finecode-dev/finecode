import collections.abc
from typing import Any


class PartialResultScheduler:
    def __init__(self) -> None:
        self.coroutines_by_key: dict[Any, list[collections.abc.Coroutine]] = {}

    def schedule(
        self, partial_result_key: Any, coroutine: collections.abc.Coroutine
    ) -> None:
        if partial_result_key not in self.coroutines_by_key:
            self.coroutines_by_key[partial_result_key] = []

        self.coroutines_by_key[partial_result_key].append(coroutine)
