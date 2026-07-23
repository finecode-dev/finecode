from pathlib import Path
from typing import Protocol


class IFileManager(Protocol):
    """Service for file system access: list files, create/read/write/delete files and
    directories.

    Its main purpose is to abstract file storage(local, remote, file system etc).
    Additional functionalities such as management of opened files etc are not part of
    this service.
    """

    async def get_content(self, file_path: Path) -> str: ...

    async def get_file_version(self, file_path: Path) -> str: ...

    async def save_file(self, file_path: Path, file_content: str) -> None: ...

    async def create_dir(
        self, dir_path: Path, create_parents: bool = True, exist_ok: bool = True
    ) -> None: ...

    async def remove_dir(self, dir_path: Path, *, tolerant: bool = False) -> None:
        """Remove a directory tree.

        By default, raises `RemoveDirError` if `dir_path` does not exist, is
        not a directory, or contains entries that cannot be removed (e.g.
        read-only files).

        Pass `tolerant=True` to instead remove whatever can be removed and
        succeed in the common case: a missing path, a dangling symlink, or a
        plain file at `dir_path` is treated as already gone, and read-only
        entries are made writable before removal. Even then, `RemoveDirError`
        is still raised if removal fails for a reason tolerance does not
        cover (e.g. a permission denied by the OS beyond read-only bits).

        `tolerant=True` is intended for directories FineCode itself manages
        (virtualenvs, caches, generated output), where a read-only entry is
        an artifact of the tooling rather than a deliberate protection. Do
        not use it to remove arbitrary user-supplied paths: it strips
        read-only bits, which there may signal intent to keep the data.
        """
        ...


class RemoveDirError(Exception):
    """Raised by `remove_dir` when the target cannot be removed, tolerant or
    not. Callers should catch this instead of an implementation's underlying
    exception type, which varies by storage backend (local filesystem,
    remote, ...)."""
