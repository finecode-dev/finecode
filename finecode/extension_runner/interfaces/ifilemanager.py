from pathlib import Path
from typing import Protocol


class IFileManager(Protocol):
    def get_content(self, file_path: Path) -> str:
        ...

    def get_file_version(self, file_path: Path) -> str:
        ...
