from __future__ import annotations

import dataclasses
import enum
import pathlib
import typing

from finecode_extension_api import service


class ProjectConfigStatus(enum.Enum):
    VALID = "valid"
    NO_CONFIG = "no_config"
    INVALID = "invalid"


@dataclasses.dataclass
class WorkspaceProject:
    path: pathlib.Path
    config_status: ProjectConfigStatus


def actionable_project_paths(projects: list[WorkspaceProject]) -> list[pathlib.Path]:
    """Return paths of projects that have valid FineCode config and can run actions."""
    return [p.path for p in projects if p.config_status == ProjectConfigStatus.VALID]


class IWorkspaceInfoProvider(service.Service, typing.Protocol):
    """Read-only access to workspace topology: project paths and related info."""

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        """Return all known workspace projects with their config status."""
        ...
