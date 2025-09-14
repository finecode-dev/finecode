import enum
import pathlib

import tomlkit

from finecode_extension_api.interfaces import ifilemanager, ipypackagelayoutinfoprovider
from finecode_extension_api import service


class PyPackageLayoutInfoProvider(ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider, service.Service):
    def __init__(self, file_manager: ifilemanager.IFileManager) -> None:
        self.file_manager = file_manager
        # TODO: cache package name by file version?

    async def _get_package_name(self, package_dir_path: pathlib.Path) -> str:
        package_def_file = package_dir_path / 'pyproject.toml'
        if not package_def_file.exists():
            raise NotImplementedError("Only python packages with pyproject.toml config file are supported")

        package_def_file_content = await self.file_manager.get_content(file_path=package_def_file)
        # TODO: handle errors
        package_def_dict = tomlkit.loads(package_def_file_content)
        package_raw_name = package_def_dict.get('project', {}).get('name', None)
        if package_raw_name is None:
            raise ValueError(f"package.name not found in {package_def_file}")

        return package_raw_name.replace('-', '_')

    async def get_package_layout(self, package_dir_path: pathlib.Path) -> ipypackagelayoutinfoprovider.PyPackageLayout:
        if (package_dir_path / 'src').exists():
            return ipypackagelayoutinfoprovider.PyPackageLayout.SRC
        else:
            package_name = await self._get_package_name(package_dir_path=package_dir_path)
            if (package_dir_path / package_name).exists():
                return ipypackagelayoutinfoprovider.PyPackageLayout.FLAT
            else:
                return ipypackagelayoutinfoprovider.PyPackageLayout.CUSTOM

    async def get_package_src_root_dir_path(self, package_dir_path: str) -> pathlib.Path:
        package_layout = await self.get_package_layout(package_dir_path=package_dir_path)
        package_name = await self._get_package_name(package_dir_path=package_dir_path)
        if package_layout == ipypackagelayoutinfoprovider.PyPackageLayout.SRC:
            return package_dir_path / 'src' / package_name
        elif package_layout == ipypackagelayoutinfoprovider.PyPackageLayout.FLAT:
            return package_dir_path / package_name
        else:
            raise NotImplementedError(f"Custom python package layout in {package_dir_path} is not supported")
