import pathlib
from typing import Any, Protocol


class IProjectInfoProvider(Protocol):
    def get_current_project_dir_path(self) -> pathlib.Path: ...

    def get_current_project_def_path(self) -> pathlib.Path: ...

    async def get_current_project_package_name(self) -> str: ...

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        """Return the resolved config dict for the project at *project_def_path*.

        "Raw" means untyped -- a plain dict shaped like the TOML -- not unprocessed.
        The WM resolves the config before serving it, so what arrives here is **not**
        what the definition file says on disk:

        - every preset's config is merged in, so a table may be here that the
          project's own file never declares;
        - a `finecode-user.toml` is merged in at its own priority;
        - handler and service dependencies are merged into `[dependency-groups]`;
        - **interpreter matrices are already expanded** (ADR-0047): an env declaring
          `interpreters` is gone from `[tool.finecode.env]` by the time a handler sees
          it, replaced by one concrete child per interpreter named
          `<base>@<impl>-<version>`, each carrying a singular `interpreter` identity
          instead of the `interpreters` list.

        The expansion point matters for anything reading the env table: a matrix env
        is only ever observable through its children here. Read the project's own file
        directly when you need what the file itself declares.

        Raises:
            ProjectInfoUnavailableError: config could not be retrieved.
        """
        ...

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        """Return the resolved config dict for the current project.

        Same resolution as `get_project_raw_config` -- presets merged and interpreter
        matrices already expanded. See there for what that changes.

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

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        """Return the workspace's extra selection, keyed by canonical package name.

        This is the validated, canonicalized reading of the gitignored
        ``finecode-workspace-user.toml``, as the WM used it to rewrite
        dependency specs. Empty when no selection file exists.

        Raises:
            ProjectInfoUnavailableError: selection could not be retrieved.
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
