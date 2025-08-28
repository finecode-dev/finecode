import pathlib
from typing import Any, Protocol


class IProjectInfoProvider(Protocol):
    def get_current_project_def_path(self) -> pathlib.Path: ...

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]: ...

    async def get_current_project_raw_config(self) -> dict[str, Any]: ...
