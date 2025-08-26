import pathlib
from typing import Any, Awaitable, Callable

from finecode_extension_api.interfaces import iprojectinfoprovider


class ProjectInfoProvider(iprojectinfoprovider.IProjectInfoProvider):
    def __init__(
        self,
        project_def_path_getter: Callable[[], pathlib.Path],
        project_raw_config_getter: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> None:
        self.project_def_path_getter = project_def_path_getter
        self.project_raw_config_getter = project_raw_config_getter

    def get_current_project_def_path(self) -> pathlib.Path:
        return self.project_def_path_getter()

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        return await self.project_raw_config_getter(str(project_def_path))

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        current_project_path = self.get_current_project_def_path()
        return await self.get_project_raw_config(project_def_path=current_project_path)
