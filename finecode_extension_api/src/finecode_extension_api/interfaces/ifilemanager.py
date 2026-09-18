from pathlib import Path
from typing import Protocol


class FileNotFound(FileNotFoundError):
    """Raised when a read or write names a path that does not exist.

    Subclasses the stdlib ``FileNotFoundError`` so a caller that already
    catches that type keeps working."""


class FileAlreadyExists(FileExistsError):
    """Raised when an operation refuses to overwrite an existing path.

    Subclasses the stdlib ``FileExistsError`` so a caller that already catches
    that type keeps working."""


class DeleteFileError(Exception):
    """Raised when ``delete_file`` cannot remove the named path.

    Callers should catch this instead of an implementation's underlying
    exception type, which varies by storage backend (local filesystem,
    remote, ...)."""


class RenameFileError(Exception):
    """Raised when ``rename_file`` cannot move ``old_path`` to ``new_path``.

    Callers should catch this instead of an implementation's underlying
    exception type, which varies by storage backend (local filesystem,
    remote, ...)."""


class IFileManager(Protocol):
    """Service for file system access: list files, create/read/write/delete files and
    directories.

    Its main purpose is to abstract file storage(local, remote, file system etc).
    Additional functionalities such as management of opened files etc are not part of
    this service.
    """

    async def get_content(self, file_path: Path) -> str:
        """Return the content of ``file_path``.

        Raises:
            FileNotFound: ``file_path`` does not exist.
        """
        ...

    async def get_file_version(self, file_path: Path) -> str:
        """Return the content-derived version of ``file_path``.

        Raises:
            FileNotFound: ``file_path`` does not exist.
        """
        ...

    async def file_exists(self, file_path: Path) -> bool:
        """Return whether there is a file at ``file_path``.

        Advisory: the answer may be stale the instant it returns, so it must
        never guard a write. A write guards itself, with ``if_version`` or with
        ``overwrite`` / ``missing_ok``.
        """
        ...

    async def delete_file(self, file_path: Path, *, missing_ok: bool = False) -> None:
        """Delete the file at ``file_path``.

        Raises:
            FileNotFound: ``file_path`` does not exist and ``missing_ok`` is False.
            DeleteFileError: ``file_path`` could not be removed (e.g. it is a
                directory -- ``remove_dir`` is the operation for directories).
        """
        ...

    async def rename_file(
        self, old_path: Path, new_path: Path, *, overwrite: bool = False
    ) -> None:
        """Move ``old_path`` to ``new_path``.

        Raises:
            FileNotFound: ``old_path`` does not exist.
            FileAlreadyExists: ``new_path`` exists and ``overwrite`` is False.
            RenameFileError: the rename could not be completed.
        """
        ...

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
