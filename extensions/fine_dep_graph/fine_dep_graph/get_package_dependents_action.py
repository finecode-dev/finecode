import dataclasses

from finecode_extension_api import code_action


@dataclasses.dataclass
class GetPackageDependentsRunPayload(code_action.RunActionPayload):
    package_name: str = ""
    """Name of the package whose transitive dependents to return."""


@dataclasses.dataclass
class GetPackageDependentsRunResult(code_action.RunActionResult):
    dependent_names: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: "GetPackageDependentsRunResult") -> None:
        self.dependent_names.extend(other.dependent_names)


class GetPackageDependentsRunContext(
    code_action.RunActionContext[GetPackageDependentsRunPayload]
): ...


class GetPackageDependentsAction(
    code_action.Action[
        GetPackageDependentsRunPayload,
        GetPackageDependentsRunContext,
        GetPackageDependentsRunResult,
    ]
):
    DESCRIPTION = "Return all packages that depend on a given package, transitively."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = GetPackageDependentsRunPayload
    RUN_CONTEXT_TYPE = GetPackageDependentsRunContext
    RESULT_TYPE = GetPackageDependentsRunResult
