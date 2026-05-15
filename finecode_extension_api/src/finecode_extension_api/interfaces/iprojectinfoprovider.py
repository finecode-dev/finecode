import pathlib
from typing import Any, Protocol


class IProjectInfoProvider(Protocol):
    def get_current_project_dir_path(self) -> pathlib.Path: ...

    def get_current_project_def_path(self) -> pathlib.Path: ...

    async def get_current_project_package_name(self) -> str: ...

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        """Return the raw TOML config dict for the project at *project_def_path*.

        Raises:
            ProjectInfoUnavailableError: config could not be retrieved.
        """
        ...

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        """Return the raw TOML config dict for the current project.

        Raises:
            ProjectInfoUnavailableError: config could not be retrieved.
        """
        ...

    def get_current_project_raw_config_version(self) -> int: ...

    async def get_workspace_editable_packages(self) -> dict[str, pathlib.Path]:
        """Return editable packages in the workspace, keyed by package name.

        Raises:
            ProjectInfoUnavailableError: packages could not be retrieved.
        """
        ...


class ProjectInfoUnavailableError(Exception):
    """Raised when project information could not be retrieved."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class InvalidProjectConfig(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
