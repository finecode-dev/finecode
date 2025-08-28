import asyncio
import collections.abc
import contextlib


class IterableSubscribe[T]:
    def __init__(self) -> None:
        self.iterators: list[IterableSubscribeIterator] = []

    def publish(self, item: T) -> None:
        for iterator in self.iterators:
            iterator.append(item)

    @contextlib.contextmanager
    def iterator(self):
        iterator = IterableSubscribeIterator()
        self.iterators.append(iterator)
        try:
            yield iterator
        finally:
            self.iterators.remove(iterator)


class IterableSubscribeIterator[T](collections.abc.AsyncIterator[T]):
    def __init__(self) -> None:
        self.values: list[T] = []
        self.change_event: asyncio.Event = asyncio.Event()

    def __aiter__(self):
        return self

    def append(self, el: T) -> None:
        self.values.append(el)
        self.change_event.set()

    async def __anext__(self) -> T:
        if len(self.values) == 0:
            await self.change_event.wait()

        next_el = self.values.pop(0)
        if len(self.values) == 0:
            self.change_event.clear()
        return next_el
