# import time
from pathlib import Path

import mypy.build as mypy_build
import mypy.modulefinder as modulefinder
import mypy.nodes as mypy_nodes
import mypy.options as mypy_options
from fine_python_mypy import iast_provider

from finecode_extension_api.interfaces import icache, ifilemanager, ilogger


class MypySingleAstProvider(iast_provider.IMypySingleAstProvider):
    CACHE_KEY = "MypySingleAstProvider"

    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
        cache: icache.ICache,
        logger: ilogger.ILogger,
    ):
        self.cache = cache
        self.file_manager = file_manager
        self.logger = logger

    async def get_file_ast(self, file_path: Path) -> mypy_nodes.MypyFile:
        try:
            cached_value = await self.cache.get_file_cache(
                file_path=file_path, key=self.CACHE_KEY
            )
            if not isinstance(cached_value, mypy_nodes.MypyFile):
                raise icache.CacheMissException()
            return cached_value
        except icache.CacheMissException:
            ...

        file_text: str = await self.file_manager.get_content(file_path)
        file_version: str = await self.file_manager.get_file_version(file_path)
        base_dir = self.get_file_package_parent_dir_path(file_path)
        module_program_path = self.get_file_program_path(
            file_path=file_path, root_package_parent_dir_path=base_dir
        )
        self.logger.debug(f"{base_dir} {module_program_path}")

        sources = [
            modulefinder.BuildSource(
                path=file_path.as_posix(),
                module=module_program_path,
                text=file_text,
                base_dir=base_dir.as_posix(),
            )
        ]
        options = mypy_options.Options()
        # options.semantic_analysis_only = True
        options.incremental = False
        options.use_fine_grained_cache = False
        options.fine_grained_incremental = False
        options.fast_exit = False
        options.raise_exceptions = False
        options.show_traceback = True
        options.follow_imports = "skip"
        # start_time = time.time_ns()

        try:
            result = mypy_build.build(sources=sources, options=options)
        except Exception as e:
            raise e
        # end_time = time.time_ns()

        # logger.info(f"Duration: {(end_time - start_time) / 1_000_000_000}s")
        # logger.info(f'{result.files}')
        # logger.info(f'{result.errors}')

        try:
            mypy_single_ast = result.files[module_program_path]
        except KeyError:
            raise Exception()  # TODO

        await self.cache.save_file_cache(
            file_path=file_path,
            file_version=file_version,
            key=self.CACHE_KEY,
            value=mypy_single_ast,
        )
        return mypy_single_ast

    def get_ast_revision(self, file_ast: mypy_nodes.MypyFile) -> str:
        return str(id(file_ast))

    def get_file_package_parent_dir_path(self, file_path: Path) -> Path:
        current_dir_path: Path = file_path.parent
        # go to parent package by package until there is no package anymore
        init_py_exists: bool = (current_dir_path / "__init__.py").exists()
        package_found: bool = init_py_exists

        while init_py_exists:
            current_dir_path = current_dir_path.parent
            init_py_exists = (current_dir_path / "__init__.py").exists()

        if package_found is True:
            return current_dir_path.parent
        else:
            return current_dir_path

    def get_file_program_path(
        self, file_path: Path, root_package_parent_dir_path: Path
    ) -> str:
        rel_path = file_path.relative_to(root_package_parent_dir_path)
        rel_path_wo_suffix = rel_path.with_suffix("")
        program_path = ".".join(rel_path_wo_suffix.parts)
        return program_path
