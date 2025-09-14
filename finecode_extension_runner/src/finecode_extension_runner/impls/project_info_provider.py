import pathlib
from typing import Any, Awaitable, Callable

from finecode_extension_api.interfaces import iprojectinfoprovider


class InvalidProjectConfig(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class ProjectInfoProvider(iprojectinfoprovider.IProjectInfoProvider):
    def __init__(
        self,
        project_def_path_getter: Callable[[], pathlib.Path],
        project_raw_config_getter: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> None:
        self.project_def_path_getter = project_def_path_getter
        self.project_raw_config_getter = project_raw_config_getter

    def get_current_project_dir_path(self) -> pathlib.Path:
        project_def_path = self.project_def_path_getter()
        return project_def_path.parent

    def get_current_project_def_path(self) -> pathlib.Path:
        return self.project_def_path_getter()

    async def get_current_project_package_name(self) -> str:
        project_raw_config = await self.get_current_project_raw_config()
        raw_name = project_raw_config.get("project", {}).get("name", None)
        if raw_name is None:
            raise InvalidProjectConfig("project.name not found in project config")

        return raw_name.replace("-", "_")

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        return await self.project_raw_config_getter(str(project_def_path))

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        current_project_path = self.get_current_project_def_path()
        return await self.get_project_raw_config(project_def_path=current_project_path)
