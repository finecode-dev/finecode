import asyncio
import contextlib
import collections.abc
import dataclasses
import pathlib
from typing import TypeVar

from finecode_extension_api.interfaces import ifileeditor, ifilemanager, ilogger


T = TypeVar("T")

class QueueIterator:
    def __init__(self, queue: asyncio.Queue[T]):
        self._queue = queue
    
    def __aiter__(self):
        return self
    
    async def __anext__(self):
        item = await self._queue.get()
        if item is None:  # Sentinel
            raise StopAsyncIteration
        return item


class MultiQueueIterator(collections.abc.AsyncIterator[T]):
    """Merges multiple asyncio queues into a single async iterator.

    Supports dynamic addition and removal of queues during iteration.
    """

    def __init__(self, queues: list[asyncio.Queue[T]]) -> None:
        self._queues: list[asyncio.Queue[T]] = queues
        self._queues_changed_event: asyncio.Event = asyncio.Event()
        self._shutdown_event: asyncio.Event = asyncio.Event()

    def shutdown(self) -> None:
        """Shutdown the iterator, causing it to raise StopAsyncIteration."""
        self._shutdown_event.set()

    def add_queue(self, queue: asyncio.Queue[T]) -> None:
        """Add a queue to be merged."""
        self._queues.append(queue)
        self._queues_changed_event.set()

    def remove_queue(self, queue: asyncio.Queue[T]) -> None:
        """Remove a queue from being merged."""
        if queue in self._queues:
            self._queues.remove(queue)
            self._queues_changed_event.set()

    def __aiter__(self) -> "MultiQueueIterator[T]":
        return self

    async def __anext__(self) -> T:
        while True:
            if not self._queues:
                raise StopAsyncIteration

            # Clear the event before starting wait
            self._queues_changed_event.clear()

            # Create get tasks for all queues
            tasks = {asyncio.create_task(queue.get()): queue for queue in self._queues}

            # Also wait for the queues changed event and shutdown event
            queues_changed_task = asyncio.create_task(self._queues_changed_event.wait())
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            # Wait for either a queue to have an item, queues to change, or shutdown
            all_tasks = set(tasks.keys()) | {queues_changed_task, shutdown_task}

            try:
                done, pending = await asyncio.wait(
                    all_tasks, return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel all pending tasks
                for task in pending:
                    task.cancel()

                # If shutdown, stop iteration
                if shutdown_task in done:
                    raise StopAsyncIteration

                # If queues changed, restart the loop
                if queues_changed_task in done:
                    continue

                # Get the result from the completed task
                completed_task = done.pop()
                result = await completed_task

                return result
            except asyncio.CancelledError:
                # Cancel all tasks on cancellation
                for task in all_tasks:
                    if not task.done():
                        task.cancel()
                raise
            finally:
                # Make sure control tasks are cancelled if they're still pending
                if not queues_changed_task.done():
                    queues_changed_task.cancel()
                if not shutdown_task.done():
                    shutdown_task.cancel()

    async def aclose(self) -> None:
        """Close the iterator and cleanup resources."""
        self.shutdown()


@dataclasses.dataclass
class OpenedFileInfo:
    content: str
    version: str
    opened_by: list[ifileeditor.IFileEditorSession]


@dataclasses.dataclass
class BlockedFileInfo:
    blocked_by: "FileEditorSession"
    unblock_event: asyncio.Event


class BaseSubscription: ...


class SubscriptionToFileChanges(BaseSubscription):
    def __init__(self) -> None:
        self.event_queue: asyncio.Queue[ifileeditor.FileChangeEvent] = asyncio.Queue()


class SubscriptionToAllEvents(BaseSubscription):
    def __init__(self) -> None:
        self.event_queue: asyncio.Queue[ifileeditor.FileEvent] = asyncio.Queue()


class FileEditorSession(ifileeditor.IFileEditorSession):
    def __init__(
        self,
        logger: ilogger.ILogger,
        author: ifileeditor.FileOperationAuthor,
        file_manager: ifilemanager.IFileManager,
        opened_files: dict[pathlib.Path, OpenedFileInfo],
        blocked_files: dict[pathlib.Path, BlockedFileInfo],
        file_change_subscriptions: dict[
            pathlib.Path,
            dict[
                ifileeditor.IFileEditorSession,
                SubscriptionToFileChanges,
            ],
        ],
        all_events_subscriptions: dict[
            ifileeditor.IFileEditorSession,
            SubscriptionToAllEvents,
        ],
    ) -> None:
        self.logger = logger
        self.author = author
        self._file_manager = file_manager
        self._opened_files = opened_files
        self._blocked_files = blocked_files
        self._file_change_subscriptions = file_change_subscriptions
        self._all_events_subscriptions = all_events_subscriptions

        self._opened_file_subscription: (
            MultiQueueIterator[ifileeditor.FileChangeEvent] | None
        ) = None

    @property
    def _subscribed_to_opened_files(self) -> bool:
        return self._opened_file_subscription is not None

    def close(self) -> None:
        """Close the session and cleanup all resources."""
        # Shutdown active subscription first
        if self._opened_file_subscription is not None:
            self._opened_file_subscription.shutdown()

            # Clean up subscriptions
            files_to_unsubscribe: list[pathlib.Path] = []
            for file_path, sessions_dict in self._file_change_subscriptions.items():
                if self in sessions_dict:
                    files_to_unsubscribe.append(file_path)

            for file_path in files_to_unsubscribe:
                self._unsubscribe_from_file_changes(file_path=file_path)

            self._opened_file_subscription = None

        # Close all files opened by this session
        files_to_close: list[pathlib.Path] = []
        for file_path, opened_file_info in self._opened_files.items():
            if self in opened_file_info.opened_by:
                files_to_close.append(file_path)

        for file_path in files_to_close:
            try:
                opened_file_info = self._opened_files[file_path]
                opened_file_info.opened_by.remove(self)

                # Remove file from opened_files if no sessions have it open
                if len(opened_file_info.opened_by) == 0:
                    del self._opened_files[file_path]
            except (KeyError, ValueError):
                # File was already removed or session not in list
                pass

        # Unblock files blocked by this session
        files_to_unblock: list[pathlib.Path] = []
        for file_path, blocked_file_info in self._blocked_files.items():
            if blocked_file_info.blocked_by == self:
                files_to_unblock.append(file_path)

        for file_path in files_to_unblock:
            try:
                blocked_file_info = self._blocked_files.pop(file_path)
                blocked_file_info.unblock_event.set()
            except KeyError:
                # File was already unblocked
                pass

    async def change_file(
        self, file_path: pathlib.Path, change: ifileeditor.FileChange
    ) -> None:
        self.logger.trace(f"Change file {file_path}")
        if file_path in self._opened_files:
            opened_file_info = self._opened_files[file_path]
            file_content = opened_file_info.content
            new_file_content = FileEditorSession.apply_change_to_file_content(
                change=change, file_content=file_content
            )
            self.logger.info(str(change))
            self.logger.info(f"||{file_content}||{new_file_content}||")
            self._update_opened_file_info(
                file_path=file_path, new_file_content=new_file_content
            )
            self.logger.trace(f"File {file_path} is opened, updated its content")
        else:
            file_content = await self._file_manager.get_content(file_path=file_path)
            new_file_content = FileEditorSession.apply_change_to_file_content(
                change=change, file_content=file_content
            )
            await self._file_manager.save_file(
                file_path=file_path, file_content=new_file_content
            )
            self.logger.trace(
                f"File {file_path} is not opened, saved it in file system"
            )

        # notify subscribers
        if file_path in self._file_change_subscriptions or len(self._all_events_subscriptions) > 0:
            self._notify_subscribers_about_file_change(
                file_path=file_path, change=change
            )

    @staticmethod
    def apply_change_to_file_content(
        change: ifileeditor.FileChange, file_content: str
    ) -> str:
        if isinstance(change, ifileeditor.FileChangeFull):
            return change.text
        else:
            # Split file content into lines
            lines = file_content.splitlines(keepends=True)

            # Get start and end positions
            start_line = change.range.start.line
            start_char = change.range.start.character
            end_line = change.range.end.line
            end_char = change.range.end.character

            # Validate range
            if start_line < 0 or end_line < 0:
                raise ValueError("Invalid range: negative line numbers not allowed")

            if end_line < start_line or (
                end_line == start_line and end_char < start_char
            ):
                raise ValueError("Invalid range: end position is before start position")

            # For bounds checking: line indices beyond file length should be treated as
            # appending to the end. LSP spec allows this for insertions at end of file,
            # make it also here the same.
            # However, if both start and end are beyond bounds, it's likely an error.
            if start_line > len(lines):
                raise ValueError(
                    f"Invalid range: start line {start_line} is beyond file length {len(lines)}"
                )

            # Build the new content
            # Part before the change
            before_parts: list[str] = []
            for i in range(start_line):
                before_parts.append(lines[i])
            if start_line < len(lines):
                before_parts.append(lines[start_line][:start_char])
            before = "".join(before_parts)

            # Part after the change
            after_parts: list[str] = []
            if end_line < len(lines):
                after_parts.append(lines[end_line][end_char:])
                for i in range(end_line + 1, len(lines)):
                    after_parts.append(lines[i])
            after = "".join(after_parts)

            new_file_content = before + change.text + after
            return new_file_content

    @contextlib.asynccontextmanager
    async def subscribe_to_changes_of_opened_files(
        self,
    ) -> collections.abc.AsyncIterator[ifileeditor.FileChangeEvent]:
        if self._subscribed_to_opened_files is True:
            raise ValueError("This session is already subscribed to opened files")

        change_queues: list[asyncio.Queue[ifileeditor.FileChangeEvent]] = []
        for file_path, opened_file_info in self._opened_files.items():
            if self in opened_file_info.opened_by:
                change_queue = self._subscribe_to_file_changes(file_path=file_path)
                change_queues.append(change_queue)

        self._opened_file_subscription = MultiQueueIterator(queues=change_queues)

        try:
            yield self._opened_file_subscription
        finally:
            # Unsubscribe from all files
            files_to_unsubscribe: list[pathlib.Path] = []
            for file_path, sessions_dict in self._file_change_subscriptions.items():
                if self in sessions_dict:
                    files_to_unsubscribe.append(file_path)

            for file_path in files_to_unsubscribe:
                self._unsubscribe_from_file_changes(file_path=file_path)

            self._opened_file_subscription.shutdown()
            self._opened_file_subscription = None

    def _subscribe_to_file_changes(
        self, file_path: pathlib.Path
    ) -> asyncio.Queue[ifileeditor.FileChangeEvent]:
        if file_path not in self._file_change_subscriptions:
            self._file_change_subscriptions[file_path] = {}

        new_subscription = SubscriptionToFileChanges()
        self._file_change_subscriptions[file_path][self] = new_subscription

        return new_subscription.event_queue

    def _unsubscribe_from_file_changes(
        self, file_path: pathlib.Path
    ) -> asyncio.Queue[ifileeditor.FileChangeEvent]:
        subscription = self._file_change_subscriptions[file_path][self]

        del self._file_change_subscriptions[file_path][self]

        if len(self._file_change_subscriptions[file_path]) == 0:
            del self._file_change_subscriptions[file_path]

        return subscription.event_queue

    def _notify_subscribers_about_file_change(
        self, file_path: pathlib.Path, change: ifileeditor.FileChange
    ) -> None:
        file_change_event = ifileeditor.FileChangeEvent(
            file_path=file_path, author=self.author, change=change
        )
        for subscription in self._file_change_subscriptions[file_path].values():
            subscription.event_queue.put_nowait(file_change_event)
        
        for subscription in self._all_events_subscriptions.values():
            subscription.event_queue.put_nowait(file_change_event)

    async def open_file(self, file_path: pathlib.Path) -> None:
        if file_path in self._opened_files:
            # file is already opened by one of the sessions, just add current session to
            # the `opened_by` list
            opened_file_info = self._opened_files[file_path]
            if self in opened_file_info.opened_by:
                raise ifileeditor.FileAlreadyOpenError(
                    f"{file_path} is already opened in this session"
                )

            opened_file_info.opened_by.append(self)
        else:
            initial_file_content = await self._file_manager.get_content(
                file_path=file_path
            )
            initial_file_version = await self._file_manager.get_file_version(
                file_path=file_path
            )
            new_opened_file_info = OpenedFileInfo(
                content=initial_file_content,
                version=initial_file_version,
                opened_by=[self],
            )
            self._opened_files[file_path] = new_opened_file_info

        if self._subscribed_to_opened_files:
            change_queue = self._subscribe_to_file_changes(file_path=file_path)
            assert self._opened_file_subscription is not None
            self._opened_file_subscription.add_queue(change_queue)

        if len(self._all_events_subscriptions) > 0:
            file_open_event = ifileeditor.FileOpenEvent(file_path=file_path)
            for subscription in self._all_events_subscriptions.values():
                subscription.event_queue.put_nowait(file_open_event)

    async def save_opened_file(self, file_path: pathlib.Path) -> None:
        if file_path not in self._opened_files:
            raise ValueError(f"{file_path} is not opened")
        opened_file_info = self._opened_files[file_path]

        if self not in opened_file_info.opened_by:
            raise ValueError(f"{file_path} is not opened in this session")

        file_content = opened_file_info.content
        await self._file_manager.save_file(
            file_path=file_path, file_content=file_content
        )

    async def close_file(self, file_path: pathlib.Path) -> None:
        if self._subscribed_to_opened_files:
            change_queue = self._unsubscribe_from_file_changes(file_path=file_path)
            assert self._opened_file_subscription is not None
            self._opened_file_subscription.remove_queue(change_queue)

        try:
            opened_file_info = self._opened_files[file_path]
            try:
                opened_file_info.opened_by.remove(self)
            except ValueError as exception:
                raise ValueError(
                    f"{file_path} is not opened in this session"
                ) from exception

            if len(opened_file_info.opened_by) == 0:
                del self._opened_files[file_path]
        except KeyError as exception:
            raise ValueError(f"{file_path} is not opened") from exception

        if len(self._all_events_subscriptions) > 0:
            file_close_event = ifileeditor.FileOpenEvent(file_path=file_path)
            for subscription in self._all_events_subscriptions.values():
                subscription.event_queue.put_nowait(file_close_event)

    def _update_opened_file_info(
        self, file_path: pathlib.Path, new_file_content: str
    ) -> None:
        # this method expects `file_path` is opened
        opened_file_info = self._opened_files[file_path]
        opened_file_info.content = new_file_content
        new_version = hash(new_file_content)  # or just increase?
        opened_file_info.version = str(new_version)

    @contextlib.asynccontextmanager
    async def subscribe_to_all_events(
        self,
    ) -> collections.abc.AsyncIterator[ifileeditor.FileEvent]:
        new_subscription = SubscriptionToAllEvents()
        self._all_events_subscriptions[self] = new_subscription
        iterator = QueueIterator(queue=new_subscription.event_queue)
        
        try:
            yield iterator
        finally:
            del self._all_events_subscriptions[self]
            await iterator._queue.put(None)

    @contextlib.asynccontextmanager
    async def read_file(
        self, file_path: pathlib.Path, block: bool = False
    ) -> collections.abc.AsyncIterator[ifileeditor.FileInfo]:
        if file_path in self._blocked_files:
            blocked_file_info = self._blocked_files[file_path]
            if blocked_file_info.blocked_by == self:
                raise ValueError(
                    f"{file_path} is blocked by this session, cannot read it"
                )

            unblock_event = blocked_file_info.unblock_event
            await unblock_event.wait()

        if block:
            blocked_file_info = BlockedFileInfo(
                blocked_by=self, unblock_event=asyncio.Event()
            )
            self._blocked_files[file_path] = blocked_file_info
        try:
            if file_path in self._opened_files:
                opened_file_info = self._opened_files[file_path]
                file_content = opened_file_info.content
                file_version = opened_file_info.version
            else:
                file_content = await self._file_manager.get_content(file_path=file_path)
                file_version = await self._file_manager.get_file_version(
                    file_path=file_path
                )
            file_info = ifileeditor.FileInfo(content=file_content, version=file_version)
            yield file_info
        finally:
            if block:
                blocked_file_info = self._blocked_files.pop(file_path)
                blocked_file_info.unblock_event.set()

    async def read_file_version(self, file_path: pathlib.Path) -> str:
        if file_path in self._blocked_files:
            blocked_file_info = self._blocked_files[file_path]
            unblock_event = blocked_file_info.unblock_event
            await unblock_event.wait()

        if file_path in self._opened_files:
            opened_file_info = self._opened_files[file_path]
            file_version = opened_file_info.version
        else:
            file_version = await self._file_manager.get_file_version(
                file_path=file_path
            )
        return file_version

    async def save_file(self, file_path: pathlib.Path, file_content: str) -> None:
        await self._file_manager.save_file(
            file_path=file_path, file_content=file_content
        )

        if file_path in self._opened_files:
            self._update_opened_file_info(
                file_path=file_path, new_file_content=file_content
            )

        if file_path in self._file_change_subscriptions or len(self._all_events_subscriptions) > 0:
            file_change = ifileeditor.FileChangeFull(text=file_content)
            self._notify_subscribers_about_file_change(
                file_path=file_path, change=file_change
            )


class FileEditor(ifileeditor.IFileEditor):
    def __init__(
        self, logger: ilogger.ILogger, file_manager: ifilemanager.IFileManager
    ) -> None:
        self.logger = logger
        self.file_manager = file_manager

        self._opened_files: dict[pathlib.Path, OpenedFileInfo] = {}
        self._blocked_files: dict[pathlib.Path, BlockedFileInfo] = {}
        self._sessions: list[FileEditorSession] = []
        self._author_by_session: dict[
            ifileeditor.IFileEditorSession, ifileeditor.FileOperationAuthor
        ] = {}
        self._file_change_subscriptions: dict[
            pathlib.Path,
            dict[
                ifileeditor.IFileEditorSession,
                SubscriptionToFileChanges,
            ],
        ] = {}
        self._all_events_subscriptions: dict[
            ifileeditor.IFileEditorSession,
            SubscriptionToAllEvents,
        ] = {}

    @contextlib.asynccontextmanager
    async def session(
        self, author: ifileeditor.FileOperationAuthor
    ) -> collections.abc.AsyncIterator[ifileeditor.IFileEditorSession]:
        new_session = FileEditorSession(
            logger=self.logger,
            author=author,
            file_manager=self.file_manager,
            opened_files=self._opened_files,
            blocked_files=self._blocked_files,
            file_change_subscriptions=self._file_change_subscriptions,
            all_events_subscriptions=self._all_events_subscriptions,
        )
        self._sessions.append(new_session)
        self._author_by_session[new_session] = author
        try:
            yield new_session
        finally:
            new_session.close()
            self._sessions.remove(new_session)
            del self._author_by_session[new_session]

    def get_opened_files(self) -> list[pathlib.Path]:
        return list(self._opened_files.keys())
