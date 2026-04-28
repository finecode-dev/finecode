from __future__ import annotations

import pathlib
from typing import Any, Awaitable, Callable

from finecode_extension_api.interfaces import iworkspaceinfoprovider


class WorkspaceInfoProviderImpl(iworkspaceinfoprovider.IWorkspaceInfoProvider):
    def __init__(self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]) -> None:
        self._send = send_request_to_wm

    async def get_workspace_project_paths(self) -> list[pathlib.Path]:
        raw = await self._send("workspace/getProjectPaths", {})
        return [pathlib.Path(p) for p in raw["projectPaths"]]
