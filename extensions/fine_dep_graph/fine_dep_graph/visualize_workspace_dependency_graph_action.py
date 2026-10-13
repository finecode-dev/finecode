import dataclasses
from typing import Literal

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class VisualizeWorkspaceDependencyGraphRunPayload(code_action.RunActionPayload):
    format: Literal["mermaid", "dot"] = "mermaid"
    """Output format: 'mermaid' (default) or 'dot'."""
    output_path: ResourceUri | None = None
    """None = return diagram as string only, do not write to disk."""


@dataclasses.dataclass
class VisualizeWorkspaceDependencyGraphRunResult(code_action.RunActionResult):
    diagram: str = ""
    output_path: ResourceUri | None = None

    def update(self, other: "VisualizeWorkspaceDependencyGraphRunResult") -> None:
        if other.diagram:
            self.diagram = other.diagram
        if other.output_path is not None:
            self.output_path = other.output_path


class VisualizeWorkspaceDependencyGraphRunContext(
    code_action.RunActionContext[VisualizeWorkspaceDependencyGraphRunPayload]
): ...


class VisualizeWorkspaceDependencyGraphAction(
    code_action.Action[
        VisualizeWorkspaceDependencyGraphRunPayload,
        VisualizeWorkspaceDependencyGraphRunContext,
        VisualizeWorkspaceDependencyGraphRunResult,
    ]
):
    DESCRIPTION = (
        "Generate a Mermaid or DOT diagram from the current dependency graph state."
    )
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = VisualizeWorkspaceDependencyGraphRunPayload
    RUN_CONTEXT_TYPE = VisualizeWorkspaceDependencyGraphRunContext
    RESULT_TYPE = VisualizeWorkspaceDependencyGraphRunResult
