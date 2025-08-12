from typing import Any, Protocol


class IProjectInfoProvider(Protocol):
    async def get_project_raw_config(self) -> dict[str, Any]: ...
