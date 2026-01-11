import pathlib

import tomlkit
import tomlkit.exceptions

from finecode_extension_api.interfaces import (
    ifileeditor,
    ipypackagelayoutinfoprovider,
    icache,
)
from finecode_extension_api import service


class ConfigParseError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class PyPackageLayoutInfoProvider(
    ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider, service.Service
):
    PACKAGE_NAME_CACHE_KEY = "PyPackageLayoutInfoProviderPackageName"
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
        id="PyPackageLayoutInfoProvider"
    )

    def __init__(
        self, file_editor: ifileeditor.IFileEditor, cache: icache.ICache
    ) -> None:
        self.file_editor = file_editor
        self.cache = cache

    async def _get_package_name(self, package_dir_path: pathlib.Path) -> str:
        # raises ConfigParseError
        package_def_file = package_dir_path / "pyproject.toml"
        if not package_def_file.exists():
            raise NotImplementedError(
                "Only python packages with pyproject.toml config file are supported"
            )

        try:
            cached_package_name = await self.cache.get_file_cache(
                file_path=package_def_file, key=self.PACKAGE_NAME_CACHE_KEY
            )
            return cached_package_name
        except icache.CacheMissException:
            ...

        async with self.file_editor.session(
            author=self.FILE_OPERATION_AUTHOR
        ) as session:
            async with session.read_file(file_path=package_def_file) as file_info:
                package_def_file_content: str = file_info.content
                package_def_file_version: str = file_info.version

        try:
            package_def_dict = tomlkit.loads(package_def_file_content)
        except tomlkit.exceptions.ParseError as exception:
            raise ConfigParseError(
                f"Failed to parse package config {package_def_file}: toml parsing failed at {exception.line}:{exception.col}"
            ) from exception

        package_raw_name = package_def_dict.get("project", {}).get("name", None)
        if package_raw_name is None:
            raise ValueError(f"package.name not found in {package_def_file}")

        package_name = package_raw_name.replace("-", "_")
        await self.cache.save_file_cache(
            file_path=package_def_file,
            file_version=package_def_file_version,
            key=self.PACKAGE_NAME_CACHE_KEY,
            value=package_name,
        )
        return package_name

    async def get_package_layout(
        self, package_dir_path: pathlib.Path
    ) -> ipypackagelayoutinfoprovider.PyPackageLayout:
        if (package_dir_path / "src").exists():
            return ipypackagelayoutinfoprovider.PyPackageLayout.SRC
        else:
            try:
                package_name = await self._get_package_name(
                    package_dir_path=package_dir_path
                )
            except ConfigParseError as exception:
                raise ipypackagelayoutinfoprovider.FailedToGetPackageLayout(
                    exception.message
                ) from exception

            if (package_dir_path / package_name).exists():
                return ipypackagelayoutinfoprovider.PyPackageLayout.FLAT
            else:
                return ipypackagelayoutinfoprovider.PyPackageLayout.CUSTOM

    async def get_package_src_root_dir_path(
        self, package_dir_path: str
    ) -> pathlib.Path:
        try:
            package_layout = await self.get_package_layout(
                package_dir_path=package_dir_path
            )
        except ipypackagelayoutinfoprovider.FailedToGetPackageLayout as exception:
            raise ipypackagelayoutinfoprovider.FailedToGetPackageSrcRootDirPath(
                exception.message
            )

        try:
            package_name = await self._get_package_name(
                package_dir_path=package_dir_path
            )
        except ConfigParseError as exception:
            raise ipypackagelayoutinfoprovider.FailedToGetPackageSrcRootDirPath(
                exception.message
            )

        if package_layout == ipypackagelayoutinfoprovider.PyPackageLayout.SRC:
            return package_dir_path / "src" / package_name
        elif package_layout == ipypackagelayoutinfoprovider.PyPackageLayout.FLAT:
            return package_dir_path / package_name
        else:
            raise NotImplementedError(
                f"Custom python package layout in {package_dir_path} is not supported"
            )
