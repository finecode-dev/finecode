"""Workspace Manager error hierarchy.

All WM-domain exceptions are defined here.  Raise these from WM code;
catch them at layer boundaries (services → API handlers, API handlers → LSP/MCP).

Hierarchy
---------
WmError
├── ProjectError
│   ├── FileNotInWorkspaceError
│   └── FileHasNoActionError
├── ConfigurationError
│   └── PresetPackageNotInstalledError
├── ActionError
│   ├── ActionNotFoundError
│   ├── ActionRunFailed
│   └── ActionNotResolvableError
├── InternalError
└── RunnerError
    └── StartingEnvironmentsFailed
"""


class WmError(Exception):
    """Base class for all Workspace Manager errors."""


# ---------------------------------------------------------------------------
# Project errors
# ---------------------------------------------------------------------------


class ProjectError(WmError):
    """Error related to project discovery or lifecycle."""


class FileNotInWorkspaceError(ProjectError):
    """A file path does not belong to any project in the workspace."""


class FileHasNoActionError(ProjectError):
    """The file belongs to projects in the workspace, but none of them defines
    the requested action."""


# ---------------------------------------------------------------------------
# Configuration errors
# ---------------------------------------------------------------------------


class ConfigurationError(WmError):
    """A project or workspace configuration is invalid or cannot be resolved."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PresetPackageNotInstalledError(ConfigurationError):
    """A required preset package is not installed in the execution environment."""


# ---------------------------------------------------------------------------
# Action errors
# ---------------------------------------------------------------------------


class ActionError(WmError):
    """Error related to action dispatch or execution."""


class ActionNotFoundError(ActionError):
    """The requested action is not defined in the project."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ActionRunFailed(ActionError):
    """Action execution failed in the Extension Runner."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ActionNotResolvableError(ActionError):
    """Action metadata could not be resolved from any of its configured handler environments.

    Raised when all handler envs for an action have been tried (including auto
    prepare-envs repair) and none could import the action class.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Runner errors
# ---------------------------------------------------------------------------


class InternalError(WmError):
    """An unexpected internal inconsistency in WM state or the WM–ER protocol.

    Raised when the WM encounters a condition that should never occur during
    normal operation (e.g. the ER asks about a project the WM doesn't know).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunnerError(WmError):
    """Error related to Extension Runner lifecycle."""


class StartingEnvironmentsFailed(RunnerError):
    """One or more execution environments failed to start."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
