import dataclasses

from finecode_extension_api import code_action


@dataclasses.dataclass
class DetectWorkspaceDependencyCyclesRunPayload(code_action.RunActionPayload): ...


@dataclasses.dataclass
class DetectWorkspaceDependencyCyclesRunResult(code_action.RunActionResult):
    cycles: list[list[str]] = dataclasses.field(default_factory=list)
    """Each inner list is one cycle as an ordered list of package names."""

    def update(self, other: "DetectWorkspaceDependencyCyclesRunResult") -> None:
        self.cycles.extend(other.cycles)


class DetectWorkspaceDependencyCyclesRunContext(
    code_action.RunActionContext[DetectWorkspaceDependencyCyclesRunPayload]
): ...


class DetectWorkspaceDependencyCyclesAction(
    code_action.Action[
        DetectWorkspaceDependencyCyclesRunPayload,
        DetectWorkspaceDependencyCyclesRunContext,
        DetectWorkspaceDependencyCyclesRunResult,
    ]
):
    DESCRIPTION = "Detect dependency cycles across all workspace packages."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = DetectWorkspaceDependencyCyclesRunPayload
    RUN_CONTEXT_TYPE = DetectWorkspaceDependencyCyclesRunContext
    RESULT_TYPE = DetectWorkspaceDependencyCyclesRunResult
