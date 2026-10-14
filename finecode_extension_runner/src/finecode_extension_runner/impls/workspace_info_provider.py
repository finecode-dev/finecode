from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable
from typing import Any

from finecode_extension_api.interfaces import iworkspaceinfoprovider


class WorkspaceInfoProviderImpl(iworkspaceinfoprovider.IWorkspaceInfoProvider):
    def __init__(
        self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]
    ) -> None:
        self._send = send_request_to_wm

    async def init(self) -> None:
        pass

    async def get_workspace_projects(
        self,
    ) -> list[iworkspaceinfoprovider.WorkspaceProject]:
        raw = await self._send("workspace/getProjectPaths", {})
        return [
            iworkspaceinfoprovider.WorkspaceProject(
                path=pathlib.Path(p["path"]),
                config_status=iworkspaceinfoprovider.ProjectConfigStatus(
                    p["configStatus"]
                ),
            )
            for p in raw["projects"]
        ]

    async def get_workspace_root(self) -> pathlib.Path | None:
        raw: iworkspaceinfoprovider.WorkspaceRootResponse = await self._send(
            "workspace/getWorkspaceRoot", {}
        )
        root = raw["root"]
        return pathlib.Path(root) if root is not None else None

    async def project_declares_action(
        self, path: pathlib.Path, action_source: str
    ) -> bool:
        raw: iworkspaceinfoprovider.ProjectDeclaresActionResponse = await self._send(
            "workspace/projectDeclaresAction",
            {"path": str(path), "actionSource": action_source},
        )
        return raw["declares"]
