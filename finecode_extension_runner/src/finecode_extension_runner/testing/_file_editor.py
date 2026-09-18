from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import pathlib
import typing

from finecode_extension_api.interfaces import ifileeditor


class FileWrite(typing.NamedTuple):
    """One recorded whole-file write -- a `save_file` or a `create_file` call."""

    file_path: pathlib.Path
    content: str


class FileOperationKind(enum.StrEnum):
    """The structural file operations `InMemoryFileEditor` records."""

    CREATE = "create"
    DELETE = "delete"
    RENAME = "rename"


class FileOperation(typing.NamedTuple):
    """One recorded structural operation.

    `paths` holds the paths the operation touched, in call order: a single path
    for `CREATE` and `DELETE`, `(old_path, new_path)` for `RENAME`. It stays a
    tuple rather than named fields so the record is comparable to the plain
    `(kind, paths)` tuple this recorder used before it was typed.
    """

    kind: FileOperationKind
    paths: tuple[pathlib.Path, ...]


def _version_of(content: str) -> str:
    """Content-derived version, so `if_version` checks mean something in tests."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class _EmptyAsyncIterator:
    def __aiter__(self) -> _EmptyAsyncIterator:
        return self

    async def __anext__(self) -> typing.Never:
        raise StopAsyncIteration


class _InMemoryFileEditorSession(ifileeditor.IFileEditorProviderSession):
    def __init__(
        self,
        storage: dict[pathlib.Path, str],
        writes: list[FileWrite],
        changes: list[ifileeditor.FileChangeEvent],
        operations: list[FileOperation],
        author: ifileeditor.FileOperationAuthor,
        seed_from_disk: bool,
        locks: dict[pathlib.Path, asyncio.Lock],
        lock_owners: dict[pathlib.Path, asyncio.Task | None],
    ) -> None:
        self._storage = storage
        self._writes = writes
        self._changes = changes
        self._operations = operations
        self._author = author
        self._seed_from_disk = seed_from_disk
        self._locks = locks
        # Shared across sessions, like `locks`: the ER keys reentrancy on the
        # task alone, so a claim taken in one session is reentrant from another
        # session on the same task. A per-session registry would deadlock there.
        self._lock_owners = lock_owners

    def _exists(self, file_path: pathlib.Path) -> bool:
        key = file_path.resolve()
        return key in self._storage or key.exists()

    def _has_file_content(self, file_path: pathlib.Path) -> bool:
        key = file_path.resolve()
        if key in self._storage:
            return True
        if self._seed_from_disk and key.is_file():
            self._storage[key] = key.read_text(encoding="utf-8")
            return True
        return False

    def _content(self, file_path: pathlib.Path) -> str:
        key = file_path.resolve()
        if key not in self._storage:
            if self._seed_from_disk and key.is_file():
                self._storage[key] = key.read_text(encoding="utf-8")
            else:
                raise ifileeditor.FileNotFound()
        return self._storage[key]

    @contextlib.asynccontextmanager
    async def _claim(self, file_path: pathlib.Path) -> typing.AsyncIterator[None]:
        key = file_path.resolve()
        lock = self._locks.setdefault(key, asyncio.Lock())
        # Reentrant within one task, matching the ER's `FileClaim`. A double
        # that is stricter than the real editor fails tests the ER would pass;
        # one that is laxer passes tests the ER would deadlock on. Both are
        # worse than a double that claims exactly where the ER claims.
        current_task = asyncio.current_task()
        holder = self._lock_owners.get(key)
        if holder is not None and current_task is not None and holder is current_task:
            yield
            return

        async with lock:
            self._lock_owners[key] = current_task
            try:
                yield
            finally:
                del self._lock_owners[key]

    @contextlib.asynccontextmanager
    async def claim_file(
        self, file_path: pathlib.Path
    ) -> typing.AsyncIterator[ifileeditor.FileInfo | None]:
        async with self._claim(file_path):
            if self._has_file_content(file_path):
                content = self._content(file_path)
                yield ifileeditor.FileInfo(
                    content=content, version=_version_of(content)
                )
            else:
                yield None

    @contextlib.asynccontextmanager
    async def read_file(
        self, file_path: pathlib.Path
    ) -> typing.AsyncIterator[ifileeditor.FileInfo]:
        content = self._content(file_path)
        yield ifileeditor.FileInfo(content=content, version=_version_of(content))

    @contextlib.asynccontextmanager
    async def modify_file(
        self, file_path: pathlib.Path
    ) -> typing.AsyncIterator[ifileeditor.FileInfo]:
        async with self.claim_file(file_path) as file_info:
            if file_info is None:
                raise ifileeditor.FileNotFound()
            yield file_info

    async def read_file_version(self, file_path: pathlib.Path) -> str:
        return _version_of(self._content(file_path))

    async def file_exists(self, file_path: pathlib.Path) -> bool:
        return self._exists(file_path)

    async def create_file(
        self,
        file_path: pathlib.Path,
        file_content: str = "",
        *,
        overwrite: bool = False,
    ) -> None:
        key = file_path.resolve()
        async with self._claim(file_path):
            if self._exists(file_path) and not overwrite:
                raise ifileeditor.FileAlreadyExists(f"{file_path} already exists")
            self._storage[key] = file_content
            self._writes.append(FileWrite(key, file_content))
            self._operations.append(FileOperation(FileOperationKind.CREATE, (key,)))

    async def delete_file(
        self,
        file_path: pathlib.Path,
        *,
        if_version: str | None = None,
        missing_ok: bool = False,
        recursive: bool = False,
    ) -> None:
        if recursive and if_version is not None:
            raise ValueError(
                "a directory has no version; if_version must be None when recursive=True"
            )
        key = file_path.resolve()
        async with self._claim(file_path):
            if recursive:
                for storage_path in [
                    path
                    for path in self._storage
                    if path == key or path.is_relative_to(key)
                ]:
                    del self._storage[storage_path]
                self._operations.append(FileOperation(FileOperationKind.DELETE, (key,)))
                return
            if not self._exists(file_path):
                if missing_ok:
                    return
                raise ifileeditor.FileNotFound()
            if if_version is not None:
                current_version = _version_of(self._content(file_path))
                if current_version != if_version:
                    raise ifileeditor.FileVersionConflict(
                        file_path=key,
                        expected_version=if_version,
                        actual_version=current_version,
                    )
            self._storage.pop(key, None)
            self._operations.append(FileOperation(FileOperationKind.DELETE, (key,)))

    async def rename_file(
        self,
        old_path: pathlib.Path,
        new_path: pathlib.Path,
        *,
        overwrite: bool = False,
        if_version: str | None = None,
    ) -> None:
        old_key = old_path.resolve()
        new_key = new_path.resolve()
        async with contextlib.AsyncExitStack() as stack:
            for path in sorted([old_path, new_path], key=str):
                await stack.enter_async_context(self._claim(path))

            if not self._exists(old_path):
                raise ifileeditor.FileNotFound()
            if self._exists(new_path) and not overwrite:
                raise ifileeditor.FileAlreadyExists(f"{new_path} already exists")
            if if_version is not None:
                current_version = _version_of(self._content(old_path))
                if current_version != if_version:
                    raise ifileeditor.FileVersionConflict(
                        file_path=old_key,
                        expected_version=if_version,
                        actual_version=current_version,
                    )

            self._storage[new_key] = self._storage.pop(old_key)
            self._operations.append(
                FileOperation(FileOperationKind.RENAME, (old_key, new_key))
            )

    async def save_file(
        self,
        file_path: pathlib.Path,
        file_content: str,
        if_version: str | None = None,
    ) -> None:
        key = file_path.resolve()
        if if_version is not None:
            current_version = _version_of(self._content(file_path))
            if current_version != if_version:
                raise ifileeditor.FileVersionConflict(
                    file_path=key,
                    expected_version=if_version,
                    actual_version=current_version,
                )
        self._storage[key] = file_content
        self._writes.append(FileWrite(key, file_content))

    async def change_file(
        self, file_path: pathlib.Path, change: ifileeditor.FileChange
    ) -> None:
        key = file_path.resolve()
        current = self._storage.get(key, "")
        self._storage[key] = _apply_change(current, change)
        self._changes.append(
            ifileeditor.FileChangeEvent(
                file_path=key, author=self._author, change=change
            )
        )

    async def save_opened_file(self, file_path: pathlib.Path) -> None:
        pass

    async def open_file(self, file_path: pathlib.Path, content: str) -> None:
        self._storage[file_path.resolve()] = content

    async def close_file(self, file_path: pathlib.Path) -> None:
        pass

    @contextlib.asynccontextmanager
    async def subscribe_to_changes_of_opened_files(
        self,
    ) -> typing.AsyncIterator[ifileeditor.FileChangeEvent]:
        yield _EmptyAsyncIterator()  # type: ignore[misc]

    @contextlib.asynccontextmanager
    async def subscribe_to_all_events(
        self,
    ) -> typing.AsyncIterator[ifileeditor.FileEvent]:
        yield _EmptyAsyncIterator()  # type: ignore[misc]


def _apply_change(content: str, change: ifileeditor.FileChange) -> str:
    if isinstance(change, ifileeditor.FileChangeFull):
        return change.text
    # FileChangePartial — apply range-based edit
    lines = content.splitlines(keepends=True)
    start_line = change.range.start.line
    end_line = change.range.end.line
    start_char = change.range.start.character
    end_char = change.range.end.character
    while len(lines) <= end_line:
        lines.append("")
    prefix = lines[start_line][:start_char]
    suffix = lines[end_line][end_char:]
    return (
        "".join(lines[:start_line])
        + prefix
        + change.text
        + suffix
        + "".join(lines[end_line + 1 :])
    )


class InMemoryFileEditor(ifileeditor.IFileEditor):
    """In-memory IFileEditor for use in handler tests.

    Seed file contents with ``seed(path, text)``, then assert on
    ``contents(path)``, ``writes``, ``changes``, and ``operations`` after the
    handler runs.

    Pass ``seed_from_disk=True`` to fall back to disk for files not explicitly
    seeded; mutations always stay in memory.
    """

    def __init__(self, seed_from_disk: bool = False) -> None:
        self._storage: dict[pathlib.Path, str] = {}
        self._seed_from_disk = seed_from_disk
        self._locks: dict[pathlib.Path, asyncio.Lock] = {}
        self._lock_owners: dict[pathlib.Path, asyncio.Task | None] = {}
        self.writes: list[FileWrite] = []
        self.changes: list[ifileeditor.FileChangeEvent] = []
        self.operations: list[FileOperation] = []

    def seed(self, file_path: str | pathlib.Path, content: str) -> None:
        self._storage[pathlib.Path(file_path).resolve()] = content

    def contents(self, file_path: str | pathlib.Path) -> str:
        return self._storage.get(pathlib.Path(file_path).resolve(), "")

    @contextlib.asynccontextmanager
    async def session(
        self, author: ifileeditor.FileOperationAuthor
    ) -> typing.AsyncIterator[_InMemoryFileEditorSession]:
        yield _InMemoryFileEditorSession(
            storage=self._storage,
            writes=self.writes,
            changes=self.changes,
            operations=self.operations,
            author=author,
            seed_from_disk=self._seed_from_disk,
            locks=self._locks,
            lock_owners=self._lock_owners,
        )

    def get_opened_files(self) -> list[pathlib.Path]:
        return []
