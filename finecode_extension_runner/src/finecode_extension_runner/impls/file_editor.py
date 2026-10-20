import asyncio
import collections.abc
import contextlib
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
class FileClaim:
    """A held right to modify one file.

    `claimed_by` exists for session-scoped cleanup — ending a session releases
    the claims it still holds. It is deliberately *not* what exclusion is keyed
    on: one session can span many independent concurrent operations, so session
    identity would conflate operations that must serialize with operations that
    must not. Exclusion is keyed by path, via `lock`.

    `owner_task` and `depth` make the claim **reentrant within one task**, the
    way `threading.RLock` is reentrant within one thread. A task that already
    holds a path may claim it again -- it takes a depth instead of a second
    acquisition, and the lock is released when the outermost claim exits.

    Reentrancy costs no exclusion. A task runs one coroutine stack at a time,
    so an inner claim can only be reached from inside the outer claim's own
    call: the two are nested, never concurrent, and serializing them is not
    something a caller could observe. It is what lets a writer claim a batch of
    paths and then call the write methods, which claim the paths they touch --
    without it that self-nesting awaits a lock the task itself holds and never
    returns.

    The one case reentrancy does not cover is a *different* task that the
    holder is waiting on -- a subtask spawned inside the claim. That still
    deadlocks, exactly as a plain lock would, because task identity is the only
    thing distinguishing "nested" from "concurrent" and asyncio does not track
    task parentage. Do not claim a held path from a subtask.
    """

    claimed_by: "FileEditorSession"
    lock: asyncio.Lock
    owner_task: asyncio.Task | None = None
    depth: int = 1


class BaseSubscription: ...


class SubscriptionToFileChanges(BaseSubscription):
    def __init__(self) -> None:
        self.event_queue: asyncio.Queue[ifileeditor.FileChangeEvent] = asyncio.Queue()


class SubscriptionToAllEvents(BaseSubscription):
    def __init__(self) -> None:
        self.event_queue: asyncio.Queue[ifileeditor.FileEvent] = asyncio.Queue()


class FileEditorSession(ifileeditor.IFileEditorProviderSession):
    def __init__(
        self,
        logger: ilogger.ILogger,
        author: ifileeditor.FileOperationAuthor,
        file_manager: ifilemanager.IFileManager,
        opened_files: dict[pathlib.Path, OpenedFileInfo],
        file_claims: dict[pathlib.Path, FileClaim],
        file_locks: dict[pathlib.Path, asyncio.Lock],
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
        self._file_claims = file_claims
        self._file_locks = file_locks
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

        # Release modification claims still held by this session
        files_to_release = [
            file_path
            for file_path, claim in self._file_claims.items()
            if claim.claimed_by is self
        ]
        for file_path in files_to_release:
            self._release_claim(file_path, force=True)

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
            content_changed = new_file_content != file_content
            self._update_opened_file_info(
                file_path=file_path, new_file_content=new_file_content
            )
            self.logger.trace(f"File {file_path} is opened, updated its content")
        else:
            file_content = await self._file_manager.get_content(file_path=file_path)
            new_file_content = FileEditorSession.apply_change_to_file_content(
                change=change, file_content=file_content
            )
            content_changed = new_file_content != file_content
            await self._file_manager.save_file(
                file_path=file_path, file_content=new_file_content
            )
            self.logger.trace(
                f"File {file_path} is not opened, saved it in file system"
            )

        # Notify subscribers, unless the change is a no-op (e.g. an IDE resyncing
        # an already-open document after reconnecting, re-sending content it
        # already sent). Broadcasting a no-op as a real FileChangeEvent looks like
        # an edit to consumers that forward it to external servers (e.g. the
        # pyrefly LSP bridge), which then cancel any in-flight request for the
        # document even though nothing actually changed.
        if content_changed and (
            file_path in self._file_change_subscriptions
            or len(self._all_events_subscriptions) > 0
        ):
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

    def _notify_subscribers(self, event: ifileeditor.FileEvent) -> None:
        # Per-path subscriptions are for FileChangeEvent only; the other event
        # kinds have no per-path queue and reach the all-events subscribers.
        if isinstance(event, ifileeditor.FileChangeEvent):
            for subscription in self._file_change_subscriptions.get(
                event.file_path, {}
            ).values():
                subscription.event_queue.put_nowait(event)

        for subscription in self._all_events_subscriptions.values():
            subscription.event_queue.put_nowait(event)

    def _notify_subscribers_about_file_change(
        self, file_path: pathlib.Path, change: ifileeditor.FileChange
    ) -> None:
        self._notify_subscribers(
            ifileeditor.FileChangeEvent(
                file_path=file_path, author=self.author, change=change
            )
        )

    async def open_file(self, file_path: pathlib.Path, content: str) -> None:
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
            new_opened_file_info = OpenedFileInfo(
                content=content,
                version=str(hash(content)),
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
            file_close_event = ifileeditor.FileCloseEvent(
                file_path=file_path, author=self.author
            )
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

    async def _current_file_info(self, file_path: pathlib.Path) -> ifileeditor.FileInfo:
        if file_path in self._opened_files:
            opened_file_info = self._opened_files[file_path]
            file_content = opened_file_info.content
            file_version = opened_file_info.version
        else:
            file_content = await self._file_manager.get_content(file_path=file_path)
            file_version = await self._file_manager.get_file_version(
                file_path=file_path
            )
        return ifileeditor.FileInfo(content=file_content, version=file_version)

    def _release_claim(self, file_path: pathlib.Path, *, force: bool = False) -> None:
        """Give up one level of the claim on `file_path`.

        The lock is released only when the outermost claim exits. `force` drops
        the whole claim whatever its depth, for session teardown: a session that
        ends with claims still held is unwinding, and leaving a lock acquired
        would strand every other writer of that path.
        """
        claim = self._file_claims.get(file_path)
        if claim is None:
            return

        if not force:
            claim.depth -= 1
            if claim.depth > 0:
                return

        self._file_claims.pop(file_path, None)
        if claim.lock.locked():
            claim.lock.release()

    @contextlib.asynccontextmanager
    async def _claim(
        self, file_path: pathlib.Path
    ) -> collections.abc.AsyncIterator[None]:
        """Hold the exclusive right to write `file_path` for the duration.

        Reentrant within one task -- see `FileClaim`. Every write path that
        needs exclusion goes through here, so there is one place where the
        reentrancy rule is stated and one place it can be got wrong.
        """
        lock = self._file_locks.get(file_path)
        if lock is None:
            lock = asyncio.Lock()
            # Kept in the registry after release so that a later modifier of the
            # same path queues on the same lock object.
            self._file_locks[file_path] = lock

        current_task = asyncio.current_task()
        held = self._file_claims.get(file_path)
        # `current_task is None` means we are not running inside a task at all
        # and have no identity to match on; treat that as "not the holder" so
        # the fallback is blocking rather than a wrongly-shared claim.
        if (
            held is not None
            and current_task is not None
            and held.owner_task is current_task
        ):
            held.depth += 1
            self.logger.trace(f"Reclaimed {file_path} (depth {held.depth})")
        else:
            await lock.acquire()
            self._file_claims[file_path] = FileClaim(
                claimed_by=self, lock=lock, owner_task=current_task
            )
            self.logger.trace(f"Claimed {file_path} for modification")

        try:
            yield
        finally:
            self._release_claim(file_path)

    @contextlib.asynccontextmanager
    async def read_file(
        self, file_path: pathlib.Path
    ) -> collections.abc.AsyncIterator[ifileeditor.FileInfo]:
        # Reads never consult `_file_claims`. A modification is published in one
        # commit, so whatever is readable here is always a consistent snapshot,
        # and a read nested inside an in-flight modification cannot deadlock on
        # a claim only its own caller could release. See ADR-0071.
        yield await self._current_file_info(file_path)

    @contextlib.asynccontextmanager
    async def claim_file(
        self, file_path: pathlib.Path
    ) -> collections.abc.AsyncIterator[ifileeditor.FileInfo | None]:
        async with self._claim(file_path):
            if await self.file_exists(file_path):
                yield await self._current_file_info(file_path)
            else:
                yield None

    @contextlib.asynccontextmanager
    async def modify_file(
        self, file_path: pathlib.Path
    ) -> collections.abc.AsyncIterator[ifileeditor.FileInfo]:
        async with self.claim_file(file_path) as file_info:
            if file_info is None:
                raise ifileeditor.FileNotFound()
            yield file_info

    async def file_exists(self, file_path: pathlib.Path) -> bool:
        if file_path in self._opened_files:
            return True
        return await self._file_manager.file_exists(file_path)

    async def create_file(
        self,
        file_path: pathlib.Path,
        file_content: str = "",
        *,
        overwrite: bool = False,
    ) -> None:
        self.logger.debug(f"Create file {file_path}")
        async with self._claim(file_path):
            if await self.file_exists(file_path) and not overwrite:
                raise ifileeditor.FileAlreadyExists(f"{file_path} already exists")
            await self._file_manager.save_file(
                file_path=file_path, file_content=file_content
            )
            if file_path in self._opened_files:
                self._update_opened_file_info(
                    file_path=file_path, new_file_content=file_content
                )
            self._notify_subscribers(
                ifileeditor.FileCreateEvent(file_path=file_path, author=self.author)
            )

    async def delete_file(
        self,
        file_path: pathlib.Path,
        *,
        if_version: str | None = None,
        missing_ok: bool = False,
        recursive: bool = False,
    ) -> None:
        """Delete `file_path`, and drop any opened-file tracking for it.

        A path tracked in ``_opened_files`` may be deleted out from under the
        sessions that have it open; a later ``close_file`` for it then raises
        ``ValueError`` because the file no longer exists. That is accepted, not
        a bug: the ``FileDeleteEvent`` emitted here is how a session finds out
        in time to avoid asking to close a deleted file.

        `recursive=True` deletes a directory tree through `remove_dir` and
        drops every opened file beneath it. A directory has no version, so
        `if_version` must be None when `recursive` is set.
        """
        if recursive and if_version is not None:
            raise ValueError(
                "a directory has no version; if_version must be None when recursive=True"
            )

        self.logger.debug(f"Delete file {file_path}")
        async with self._claim(file_path):
            if recursive:
                await self._file_manager.remove_dir(file_path, tolerant=False)
                for opened_path in [
                    path
                    for path in self._opened_files
                    if path == file_path or path.is_relative_to(file_path)
                ]:
                    del self._opened_files[opened_path]
                self._notify_subscribers(
                    ifileeditor.FileDeleteEvent(file_path=file_path, author=self.author)
                )
                return

            if not await self.file_exists(file_path):
                if missing_ok:
                    return
                raise ifileeditor.FileNotFound()
            if if_version is not None:
                current_version = await self.read_file_version(file_path)
                if current_version != if_version:
                    raise ifileeditor.FileVersionConflict(
                        file_path=file_path,
                        expected_version=if_version,
                        actual_version=current_version,
                    )
            # `file_exists` counts a path the editor holds open, which may have
            # no filesystem counterpart (an IDE tab outliving its file). Deleting
            # it is dropping the buffer; `missing_ok` keeps storage from
            # objecting to what is already not there.
            await self._file_manager.delete_file(file_path, missing_ok=True)
            self._opened_files.pop(file_path, None)
            self._notify_subscribers(
                ifileeditor.FileDeleteEvent(file_path=file_path, author=self.author)
            )

    async def rename_file(
        self,
        old_path: pathlib.Path,
        new_path: pathlib.Path,
        *,
        overwrite: bool = False,
        if_version: str | None = None,
    ) -> None:
        self.logger.debug(f"Rename file {old_path} to {new_path}")
        # Claim both paths in sorted order so two concurrent renames of the
        # same pair cannot deadlock by acquiring them in opposite orders.
        async with contextlib.AsyncExitStack() as stack:
            for path in sorted([old_path, new_path], key=str):
                await stack.enter_async_context(self._claim(path))

            if not await self.file_exists(old_path):
                raise ifileeditor.FileNotFound()
            if await self.file_exists(new_path) and not overwrite:
                raise ifileeditor.FileAlreadyExists(f"{new_path} already exists")
            if if_version is not None:
                current_version = await self.read_file_version(old_path)
                if current_version != if_version:
                    raise ifileeditor.FileVersionConflict(
                        file_path=old_path,
                        expected_version=if_version,
                        actual_version=current_version,
                    )

            # A path the editor holds open may have no filesystem counterpart,
            # in which case the tracked buffer is the whole of the file and
            # moving it is the whole of the rename.
            if await self._file_manager.file_exists(old_path):
                await self._file_manager.rename_file(
                    old_path, new_path, overwrite=overwrite
                )
            opened_info = self._opened_files.pop(old_path, None)
            if opened_info is not None:
                self._opened_files[new_path] = opened_info
            self._notify_subscribers(
                ifileeditor.FileRenameEvent(
                    old_path=old_path,
                    new_path=new_path,
                    author=self.author,
                )
            )

    async def read_file_version(self, file_path: pathlib.Path) -> str:
        # Non-blocking, for the same reason as `read_file` — this path is reached
        # indirectly through file caches, so a wait here would reintroduce the
        # deadlock that removing the read barrier eliminates.
        if file_path in self._opened_files:
            opened_file_info = self._opened_files[file_path]
            file_version = opened_file_info.version
        else:
            file_version = await self._file_manager.get_file_version(
                file_path=file_path
            )
        return file_version

    async def save_file(
        self,
        file_path: pathlib.Path,
        file_content: str,
        if_version: str | None = None,
    ) -> None:
        self.logger.debug(f"Save file {file_path}")

        if if_version is not None:
            current_version = await self.read_file_version(file_path)
            if current_version != if_version:
                raise ifileeditor.FileVersionConflict(
                    file_path=file_path,
                    expected_version=if_version,
                    actual_version=current_version,
                )

        # Only opened files have cached content cheap enough to diff against; for
        # files that aren't open, treat the write as a real change (matches prior
        # behavior) rather than reading from disk just to check.
        previous_content = (
            self._opened_files[file_path].content
            if file_path in self._opened_files
            else None
        )

        await self._file_manager.save_file(
            file_path=file_path, file_content=file_content
        )

        if file_path in self._opened_files:
            self._update_opened_file_info(
                file_path=file_path, new_file_content=file_content
            )

        content_changed = previous_content is None or previous_content != file_content
        if content_changed and (
            file_path in self._file_change_subscriptions
            or len(self._all_events_subscriptions) > 0
        ):
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
        self._file_claims: dict[pathlib.Path, FileClaim] = {}
        self._file_locks: dict[pathlib.Path, asyncio.Lock] = {}
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
            file_claims=self._file_claims,
            file_locks=self._file_locks,
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
