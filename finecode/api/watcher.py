from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
import multiprocessing as mp
from pathlib import Path
import time
from typing import Generator, Generic, TypeVar
from threading import Timer

from loguru import logger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import finecode.api.async_queue as async_queue


@dataclass
class ChangeEvent:
    path: Path
    kind: ChangeKind
    # used for MOVE and RENAME events
    new_path: Path | None = None

    def __eq__(self, other: ChangeEvent) -> bool:
        return (
            self.path == other.path
            and self.kind == other.kind
            and self.new_path == other.new_path
        )


class ChangeKind(Enum):
    NEW = auto()
    MODIFY = auto()
    MOVE = auto()
    RENAME = auto()
    DELETE = auto()


class QueueingEventHandler(FileSystemEventHandler):
    # TODO: implement rename event
    def __init__(
        self,
        event_queue: async_queue.AsyncQueue[ChangeEvent],
        enable_rename_event: bool = False,
    ):
        super().__init__()
        self.event_queue = event_queue
        self.enable_rename_event = enable_rename_event
        self._timer = Timer(0.1, self._timer_end)
        self._timer_is_running = False
        self._event_buffer: list[ChangeEvent] = []

    def on_moved(self, event):
        super().on_moved(event)

        what = "directory" if event.is_directory else "file"
        print(f"Moved {what}: from {event.src_path} to {event.dest_path}")
        self.queue_event(
            ChangeEvent(
                path=Path(event.src_path),
                new_path=Path(event.dest_path),
                kind=ChangeKind.MOVE,
            )
        )

    def on_created(self, event):
        super().on_created(event)

        what = "directory" if event.is_directory else "file"
        print(f"Created {what}: {event.src_path}")
        self.queue_event(
            ChangeEvent(path=Path(event.src_path), kind=ChangeKind.NEW)
        )

    def on_deleted(self, event):
        super().on_deleted(event)

        what = "directory" if event.is_directory else "file"
        print(f"Deleted {what}: {event.src_path}")
        self.queue_event(
            ChangeEvent(path=Path(event.src_path), kind=ChangeKind.DELETE)
        )

    def on_modified(self, event):
        super().on_modified(event)

        self.queue_event(
            ChangeEvent(path=Path(event.src_path), kind=ChangeKind.MODIFY)
        )

    def queue_event(self, event: ChangeEvent) -> None:
        self._event_buffer.append(event)
        if not self._timer_is_running:
            self._timer.start()
            self._timer_is_running = True

    def _timer_end(self) -> None:
        # 1. If file is watched and we change it, we get modified file and modified parent
        #    directory events. Directory itself cannot be modified(only renamed, but it is
        #    rename event), so we can safely remove these events. 'modified directory' seems to be
        #    always the next event after 'modified file', even if multiple files were modified
        #    (experimentally found on linux).
        last_modified_file_parent: Path | None = None
        events_to_raise = self._event_buffer.copy()
        for event in self._event_buffer:
            if event.path.is_file():
                last_modified_file_parent = event.path.parent
            elif event.path.is_dir():
                if event.path == last_modified_file_parent:
                    try:
                        events_to_raise.remove(event)
                    except ValueError:
                        ...

        for event in events_to_raise:
            self.event_queue.put(event)

        # restart timer
        self._timer_is_running = False
        self._timer = Timer(0.1, self._timer_end)


QueueElementType = TypeVar("QueueElementType")


class AsyncQueueIterator(Generic[QueueElementType]):
    def __init__(self, queue: async_queue.AsyncQueue[QueueElementType]):
        self.queue = queue

    def __aiter__(self):
        return self

    async def __anext__(self) -> QueueElementType:
        result = await self.queue.get_async()
        return result


def run_observer(event_queue: async_queue.AsyncQueue, dir_path: Path) -> None:
    observer = Observer()
    event_handler = QueueingEventHandler(event_queue=event_queue)
    observer.schedule(event_handler, dir_path, recursive=True)
    observer.start()
    logger.trace(f"Start watcher on {dir_path}")
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()


@contextmanager
def watch_workspace_dir(
    dir_path: Path,
) -> Generator[AsyncQueueIterator[ChangeEvent], None, None]:
    # NOTE: watcher is not in all possible cases reliable, especially when there are a lot of
    # changes on Windows. Always provide possibility to refresh information manually if possible.
    event_queue = async_queue.create_async_process_queue()
    event_queue_iterator = AsyncQueueIterator(event_queue)

    try:
        p = mp.Process(target=run_observer, args=(event_queue, dir_path))
        p.start()
        yield event_queue_iterator
    finally:
        # TODO: send sig
        p.join()
