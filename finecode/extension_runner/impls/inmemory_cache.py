from pathlib import Path
from typing import Any

from finecode.extension_runner.interfaces import icache, ifilemanager


class InMemoryCache(icache.ICache):
    def __init__(self, file_manager: ifilemanager.IFileManager):
        self.file_manager = file_manager

        self.cache_by_file: dict[Path, dict[str, Any]] = {}

        # TODO: clear file cache when file changes

    def save_file_cache(self, file_version: str, file_path: Path, key: str, value: Any) -> None:
        if file_path not in self.cache_by_file:
            self.cache_by_file[file_path] = {}

        current_file_version = self.file_manager.get_file_version(file_path)
        if file_version != current_file_version:
            # file changed, clean its cache
            self.cache_by_file[file_path] = {}

        self.cache_by_file[file_path][key] = value

    def get_file_cache(self, file_path: Path, key: str) -> Any:
        try:
            return self.cache_by_file[file_path][key]
        except KeyError:
            raise icache.CacheMissException()

    def file_changed_since_state(self, file_path: Path, last_state: str) -> bool: ...

    def get_file_state(self, file_path: Path) -> str: ...
