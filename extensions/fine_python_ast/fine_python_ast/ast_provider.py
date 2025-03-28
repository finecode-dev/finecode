import ast
from pathlib import Path

from fine_python_ast import iast_provider

from finecode_extension_api.interfaces import icache, ifilemanager, ilogger


class PythonSingleAstProvider(iast_provider.IPythonSingleAstProvider):
    CACHE_KEY = "PythonSingleAstProvider"

    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
        cache: icache.ICache,
        logger: ilogger.ILogger,
    ):
        self.cache = cache
        self.file_manager = file_manager
        self.logger = logger

    async def get_file_ast(self, file_path: Path) -> ast.Module:
        try:
            cached_value = await self.cache.get_file_cache(
                file_path=file_path, key=self.CACHE_KEY
            )
            if not isinstance(cached_value, ast.Module):
                raise icache.CacheMissException()
            return cached_value
        except icache.CacheMissException:
            ...

        file_content: str = await self.file_manager.get_content(file_path)
        file_version: str = await self.file_manager.get_file_version(file_path)

        try:
            ast_instance = ast.parse(file_content)
        except SyntaxError as error:
            raise error

        await self.cache.save_file_cache(
            file_path=file_path,
            file_version=file_version,
            key=self.CACHE_KEY,
            value=ast_instance,
        )
        return ast_instance

    def get_ast_revision(self, file_ast: ast.Module) -> str:
        return str(id(file_ast))
