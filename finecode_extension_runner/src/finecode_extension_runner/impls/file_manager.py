import asyncio
import hashlib
import os
import shutil
import stat
from pathlib import Path

from finecode_extension_api.interfaces import ifilemanager, ilogger


class FileManager(ifilemanager.IFileManager):
    def __init__(
        self,
        logger: ilogger.ILogger,
    ) -> None:
        self.logger = logger

    async def get_content(self, file_path: Path) -> str:
        file_content = self.read_content_file_from_fs(file_path=file_path)

        return file_content

    async def get_file_version(self, file_path: Path) -> str:
        file_version: str = ""
        file_version = self.get_hash_of_file_from_fs(file_path=file_path)

        # 12 chars is enough to distinguish. The whole value is 64 chars length and
        # is not really needed in logs
        file_version_readable = f"{file_version[:12]}..."
        self.logger.debug(f"Version of {file_path}: {file_version_readable}")
        return file_version

    async def save_file(self, file_path: Path, file_content: str) -> None:
        self.logger.debug(f"Save file {file_path}")
        with open(file_path, "w") as f:
            f.write(file_content)

    async def create_dir(
        self, dir_path: Path, create_parents: bool = True, exist_ok: bool = True
    ):
        # currently only local file system is supported
        dir_path.mkdir(parents=create_parents, exist_ok=exist_ok)

    async def remove_dir(self, dir_path: Path, *, tolerant: bool = False) -> None:
        if not tolerant:
            try:
                await asyncio.to_thread(shutil.rmtree, dir_path)
            except OSError as exception:
                raise ifilemanager.RemoveDirError(str(exception)) from exception
            return

        try:
            await asyncio.to_thread(self._remove_dir_tolerant, dir_path)
        except OSError as exception:
            raise ifilemanager.RemoveDirError(str(exception)) from exception

    def _remove_dir_tolerant(self, dir_path: Path) -> None:
        if dir_path.is_symlink() or (dir_path.exists() and not dir_path.is_dir()):
            dir_path.unlink(missing_ok=True)
            return

        if not dir_path.exists():
            return

        try:
            shutil.rmtree(dir_path)
        except OSError:
            # Fall back to force-chmodding the tree only once the plain
            # removal actually fails — the common case (nothing read-only)
            # then costs nothing beyond `rmtree`'s own walk.
            self._make_tree_writable(dir_path)
            shutil.rmtree(dir_path)

    def _make_tree_writable(self, root: Path) -> None:
        """Best-effort: give the owner write access to everything under *root*.

        `os.walk` is top-down, so each directory is made traversable before the
        walk descends into it — a directory with its permissions stripped would
        otherwise be skipped silently and left behind.
        """
        self._chmod_writable(root)
        for dir_path, dir_names, file_names in os.walk(root):
            for name in (*dir_names, *file_names):
                self._chmod_writable(Path(dir_path) / name)

    def _chmod_writable(self, path: Path) -> None:
        if path.is_symlink():
            # chmod follows symlinks; the link itself needs no permissions to
            # be unlinked, and its target may be outside the tree.
            return
        try:
            mode = path.stat().st_mode
            extra = stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0)
            path.chmod(mode | extra)
        except OSError:
            # Best effort only — let rmtree report the real, actionable failure.
            pass

    # helper methods
    def read_content_file_from_fs(self, file_path: Path) -> str:
        # don't use this method directly, use `get_content` instead
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        self.logger.debug(f"Read file: {file_path}")
        with open(file_path) as f:
            file_content = f.read()

        return file_content

    def get_hash_of_file_from_fs(self, file_path: Path) -> str:
        # don't use this method directly, use `get_file_version` instead
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        with open(file_path, "rb") as f:
            file_version = hashlib.file_digest(f, "sha256").hexdigest()

        return file_version
