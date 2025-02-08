from pathlib import Path
from typing import Any, Protocol


class ICache(Protocol):
    def save_file_cache(self, file_path: Path, file_version: str, key: str, value: Any) -> None:
        ...

    def get_file_cache(self, file_path: Path, key: str) -> Any:
        ...


class CacheMissException(Exception):
    pass
