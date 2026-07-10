# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from fine_src_artifacts.lock_dependencies_action import (
    LockDependenciesAction,
    LockDependenciesRunPayload,
    LockDependenciesRunResult,
)


@dataclasses.dataclass
class LockPythonDependenciesRunPayload(LockDependenciesRunPayload):
    target_python_version: str | None = None
    """Python version to target, e.g. '3.11'. Defaults to the running interpreter version."""
    target_platform: str | None = None
    """Wheel platform tag to target, e.g. 'linux_x86_64'. Defaults to the current platform."""


class LockPythonDependenciesRunContext(
    code_action.RunActionContext[LockPythonDependenciesRunPayload]
): ...


class LockPythonDependenciesAction(
    code_action.Action[
        LockPythonDependenciesRunPayload,
        LockPythonDependenciesRunContext,
        LockDependenciesRunResult,
    ]
):
    """Generate a pip-compatible lock file for a Python artifact's dependencies."""

    DESCRIPTION = "Generate a pip-compatible lock file for a Python artifact's dependencies."
    PAYLOAD_TYPE = LockPythonDependenciesRunPayload
    RUN_CONTEXT_TYPE = LockPythonDependenciesRunContext
    RESULT_TYPE = LockDependenciesRunResult
    LANGUAGE = "python"
    PARENT_ACTION = LockDependenciesAction
