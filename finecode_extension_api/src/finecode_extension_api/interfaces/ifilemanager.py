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

    async def get_file_version(self, file_path: Path) -> str:
        ...

    async def save_file(self, file_path: Path, file_content: str) -> None: ...

    async def create_dir(
        self, dir_path: Path, create_parents: bool = True, exist_ok: bool = True
    ) -> None: ...

    async def remove_dir(self, dir_path: Path) -> None: ...
