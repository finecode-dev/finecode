from pathlib import Path
from typing import Any, TypeAlias

from finecode_extension_api.interfaces import icache, ifilemanager, ilogger

CacheKeyType: TypeAlias = str


class InMemoryCache(icache.ICache):
    def __init__(
        self, file_manager: ifilemanager.IFileManager, logger: ilogger.ILogger
    ):
        self.file_manager = file_manager
        self.logger = logger

        self.cache_by_file: dict[Path, dict[CacheKeyType, tuple[str, Any]]] = {}

        # TODO: clear file cache when file changes

    async def save_file_cache(
        self, file_path: Path, file_version: str, key: CacheKeyType, value: Any
    ) -> None:
        current_file_version = await self.file_manager.get_file_version(file_path)

        if file_version != current_file_version:
            # `value` was created for older version of file, don't save it
            return None

        if file_path not in self.cache_by_file:
            # no cache for file, create
            self.cache_by_file[file_path] = {}

        self.cache_by_file[file_path][key] = (file_version, value)

    async def get_file_cache(self, file_path: Path, key: CacheKeyType) -> Any:
        try:
            file_cache = self.cache_by_file[file_path]
        except KeyError:
            self.logger.debug(f"No cache for file {file_path}, cache miss")
            raise icache.CacheMissException()

        if key not in file_cache:
            self.logger.debug(
                f"No cache with key {key} for file {file_path}, cache miss"
            )
            raise icache.CacheMissException()

        current_file_version = await self.file_manager.get_file_version(file_path)
        cached_file_version = file_cache[key][0]
        if cached_file_version != current_file_version:
            self.logger.debug(
                f"Cached value for file {file_path} is outdated, cache miss"
            )
            raise icache.CacheMissException()
        else:
            self.logger.debug(f"Use cached value for {file_path}, key {key}")
            cached_value = file_cache[key][1]
            return cached_value
