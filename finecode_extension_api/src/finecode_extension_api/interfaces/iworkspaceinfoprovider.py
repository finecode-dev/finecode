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


class WorkspaceRootResponse(typing.TypedDict):
    """Reply of ``workspace/getWorkspaceRoot``.

    ``root`` is a posix path, or ``None``
    """

    root: str | None


class ProjectDeclaresActionResponse(typing.TypedDict):
    """Reply of ``workspace/projectDeclaresAction``."""

    declares: bool


def actionable_project_paths(projects: list[WorkspaceProject]) -> list[pathlib.Path]:
    """Return paths of projects that have valid FineCode config and can run actions."""
    return [p.path for p in projects if p.config_status == ProjectConfigStatus.VALID]


class IWorkspaceInfoProvider(service.Service, typing.Protocol):
    """Read-only access to workspace topology: project paths and related info."""

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        """Return all known workspace projects with their config status."""
        ...

    async def get_workspace_root(self) -> pathlib.Path | None:
        """Return the workspace root directory, or ``None`` when it is ambiguous.

        ``None`` means several workspace directories exist and none is marked
        by ``finecode-workspace.toml``; callers must refuse rather than pick one.
        """
        ...

    async def project_declares_action(
        self, path: pathlib.Path, action_source: str
    ) -> bool:
        """Whether the project at *path* declares the action *action_source*."""
        ...
