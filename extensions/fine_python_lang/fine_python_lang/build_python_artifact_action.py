# docs: docs/reference/actions.md
import dataclasses
import typing

from fine_src_artifacts.build_artifact_action import (
    BuildArtifactAction,
    BuildArtifactRunPayload,
    BuildArtifactRunResult,
)
from finecode_extension_api import code_action


@dataclasses.dataclass
class BuildPythonArtifactRunPayload(BuildArtifactRunPayload):
    distributions: list[typing.Literal["sdist", "wheel"]] | None = None
    """Distribution formats to build. ``None`` means the handler default (sdist, then wheel from it)."""


class BuildPythonArtifactRunContext(
    code_action.RunActionContext[BuildPythonArtifactRunPayload]
): ...


class BuildPythonArtifactAction(
    code_action.Action[
        BuildPythonArtifactRunPayload,
        BuildPythonArtifactRunContext,
        BuildArtifactRunResult,
    ]
):
    """Build the wheel and/or sdist distributions of a Python artifact."""

    DESCRIPTION = "Build the wheel and/or sdist distributions of a Python artifact."
    PAYLOAD_TYPE = BuildPythonArtifactRunPayload
    RUN_CONTEXT_TYPE = BuildPythonArtifactRunContext
    RESULT_TYPE = BuildArtifactRunResult
    LANGUAGE = "python"
    PARENT_ACTION = BuildArtifactAction
