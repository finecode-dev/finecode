import hashlib
import shutil
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
        with open(file_path, "w") as f:
            f.write(file_content)

    async def create_dir(
        self, dir_path: Path, create_parents: bool = True, exist_ok: bool = True
    ):
        # currently only local file system is supported
        dir_path.mkdir(parents=create_parents, exist_ok=exist_ok)

    async def remove_dir(self, dir_path: Path) -> None:
        shutil.rmtree(dir_path)

    # helper methods
    def read_content_file_from_fs(self, file_path: Path) -> str:
        # don't use this method directly, use `get_content` instead
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        self.logger.debug(f"Read file: {file_path}")
        with open(file_path, "r") as f:
            file_content = f.read()

        return file_content

    def get_hash_of_file_from_fs(self, file_path: Path) -> str:
        # don't use this method directly, use `get_file_version` instead
        # TODO: handle errors: file doesn't exist, cannot be opened etc
        with open(file_path, "rb") as f:
            file_version = hashlib.file_digest(f, "sha256").hexdigest()

        return file_version
