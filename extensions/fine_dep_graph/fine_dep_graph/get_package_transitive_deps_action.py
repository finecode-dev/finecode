import dataclasses

from finecode_extension_api import code_action


@dataclasses.dataclass
class GetPackageTransitiveDepsRunPayload(code_action.RunActionPayload):
    package_name: str = ""
    """Name of the package whose transitive dependencies to return."""


@dataclasses.dataclass
class GetPackageTransitiveDepsRunResult(code_action.RunActionResult):
    dependency_names: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: "GetPackageTransitiveDepsRunResult") -> None:
        self.dependency_names.extend(other.dependency_names)


class GetPackageTransitiveDepsRunContext(
    code_action.RunActionContext[GetPackageTransitiveDepsRunPayload]
): ...


class GetPackageTransitiveDepsAction(
    code_action.Action[
        GetPackageTransitiveDepsRunPayload,
        GetPackageTransitiveDepsRunContext,
        GetPackageTransitiveDepsRunResult,
    ]
):
    DESCRIPTION = "Return all packages that a given package depends on, transitively."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = GetPackageTransitiveDepsRunPayload
    RUN_CONTEXT_TYPE = GetPackageTransitiveDepsRunContext
    RESULT_TYPE = GetPackageTransitiveDepsRunResult
