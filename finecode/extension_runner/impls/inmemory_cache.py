from pathlib import Path
from typing import Any

from finecode.extension_runner.interfaces import icache, ifilemanager, ilogger


class InMemoryCache(icache.ICache):
    def __init__(self, file_manager: ifilemanager.IFileManager, logger: ilogger.ILogger):
        self.file_manager = file_manager
        self.logger = logger

        self.cache_by_file: dict[Path, tuple[str, dict[str, Any]]] = {}

        # TODO: clear file cache when file changes

    async def save_file_cache(self, file_path: Path, file_version: str, key: str, value: Any) -> None:
        current_file_version = await self.file_manager.get_file_version(file_path)
        
        if file_version != current_file_version:
            # `value` was created for older version of file, don't save it
            return None

        if file_path not in self.cache_by_file:
            # no cache for file, create
            self.cache_by_file[file_path] = (current_file_version, {})
        elif self.cache_by_file[file_path][0] != current_file_version:
            # cached value is outdated, clean it
            self.cache_by_file[file_path] = (current_file_version, {})

        self.cache_by_file[file_path][1][key] = value

    async def get_file_cache(self, file_path: Path, key: str) -> Any:
        try:
            file_cache = self.cache_by_file[file_path]
        except KeyError:
            self.logger.debug(f'No cache for file {file_path}, cache miss')
            raise icache.CacheMissException()

        current_file_version = await self.file_manager.get_file_version(file_path)
        cached_file_version = file_cache[0]
        if cached_file_version != current_file_version:
            self.logger.debug(f'Cached value for file {file_path} is outdated, cache miss')
            raise icache.CacheMissException()
        else:
            try:
                cached_value = file_cache[1][key]
            except KeyError:
                self.logger.debug(f'Cached value for file {file_path} doesn\'t contain key {key}, cache miss')
                raise icache.CacheMissException()

            self.logger.debug(f"Use cached value for {file_path}, key {key}")
            return cached_value

    def file_changed_since_state(self, file_path: Path, last_state: str) -> bool: ...

    def get_file_state(self, file_path: Path) -> str: ...
