from typing import Callable
from typing import Any

from finecode_extension_api.interfaces import iprojectinfoprovider


project_raw_config_getter: Callable


class ProjectInfoProvider(iprojectinfoprovider.IProjectInfoProvider):
    def __init__(
        self,
    ) -> None:
        ...

    async def get_project_raw_config(
        self
    ) -> dict[str, Any]:
        return await project_raw_config_getter()
