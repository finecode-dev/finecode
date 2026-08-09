from __future__ import annotations

import asyncio
import contextlib
import hashlib
import pathlib
import typing

from finecode_extension_api.interfaces import ifileeditor


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
        writes: list[tuple[pathlib.Path, str]],
        changes: list[ifileeditor.FileChangeEvent],
        author: ifileeditor.FileOperationAuthor,
        seed_from_disk: bool,
        locks: dict[pathlib.Path, asyncio.Lock],
    ) -> None:
        self._storage = storage
        self._writes = writes
        self._changes = changes
        self._author = author
        self._seed_from_disk = seed_from_disk
        self._locks = locks

    def _content(self, file_path: pathlib.Path) -> str:
        key = file_path.resolve()
        if key not in self._storage:
            if self._seed_from_disk and key.exists():
                self._storage[key] = key.read_text(encoding="utf-8")
            else:
                self._storage[key] = ""
        return self._storage[key]

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
        key = file_path.resolve()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            content = self._content(file_path)
            yield ifileeditor.FileInfo(content=content, version=_version_of(content))

    async def read_file_version(self, file_path: pathlib.Path) -> str:
        return _version_of(self._content(file_path))

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
        self._writes.append((key, file_content))

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
    ``contents(path)``, ``writes``, and ``changes`` after the handler runs.

    Pass ``seed_from_disk=True`` to fall back to disk for files not explicitly
    seeded; mutations always stay in memory.
    """

    def __init__(self, seed_from_disk: bool = False) -> None:
        self._storage: dict[pathlib.Path, str] = {}
        self._seed_from_disk = seed_from_disk
        self._locks: dict[pathlib.Path, asyncio.Lock] = {}
        self.writes: list[tuple[pathlib.Path, str]] = []
        self.changes: list[ifileeditor.FileChangeEvent] = []

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
            author=author,
            seed_from_disk=self._seed_from_disk,
            locks=self._locks,
        )

    def get_opened_files(self) -> list[pathlib.Path]:
        return []
