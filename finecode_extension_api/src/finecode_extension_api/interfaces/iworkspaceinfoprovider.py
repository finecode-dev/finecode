from __future__ import annotations

import pathlib
import typing

from finecode_extension_api import service


class IWorkspaceInfoProvider(service.Service, typing.Protocol):
    """Read-only access to workspace topology: project paths and related info."""

    async def get_workspace_project_paths(self) -> list[pathlib.Path]:
        """Return the directory paths of all known workspace projects."""
        ...
