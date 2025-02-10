from pathlib import Path
from typing import Protocol


class IFileManager(Protocol):
    async def get_content(self, file_path: Path) -> str:
        ...

    async def get_file_version(self, file_path: Path) -> str:
        ...
