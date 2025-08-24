from typing import Any, Callable

from finecode_extension_api.interfaces import iprojectinfoprovider

project_raw_config_getter: Callable


class ProjectInfoProvider(iprojectinfoprovider.IProjectInfoProvider):
    async def get_project_raw_config(self) -> dict[str, Any]:
        return await project_raw_config_getter()
