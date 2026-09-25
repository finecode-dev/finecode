import dataclasses

from finecode_extension_api import code_action


@dataclasses.dataclass
class CollectProjectDependencyInfoRunPayload(code_action.RunActionPayload): ...


@dataclasses.dataclass
class CollectProjectDependencyInfoRunResult(code_action.RunActionResult):
    package_name: str = ""
    package_kind: str = ""
    """'core' | 'extension' | 'preset'. Set by PyprojectPackageInfoHandler."""
    pyproject_dependencies: list[str] = dataclasses.field(default_factory=list)
    """Raw names from project.dependencies, version specifiers stripped."""
    used_preset_sources: list[str] = dataclasses.field(default_factory=list)
    """Values of tool.finecode.presets[*].source."""

    def update(self, other: "CollectProjectDependencyInfoRunResult") -> None:
        if other.package_name:
            self.package_name = other.package_name
        if other.package_kind:
            self.package_kind = other.package_kind
        self.pyproject_dependencies.extend(other.pyproject_dependencies)
        self.used_preset_sources.extend(other.used_preset_sources)


class CollectProjectDependencyInfoRunContext(
    code_action.RunActionContext[CollectProjectDependencyInfoRunPayload]
): ...


class CollectProjectDependencyInfoAction(
    code_action.Action[
        CollectProjectDependencyInfoRunPayload,
        CollectProjectDependencyInfoRunContext,
        CollectProjectDependencyInfoRunResult,
    ]
):
    """Report this project's package identity, declared dependencies, and active presets.

    Each project runs this action once per workspace seed. Handlers run concurrently:
    PyprojectPackageInfoHandler writes package_name, package_kind, and
    pyproject_dependencies; FineCodePresetsInfoHandler writes used_preset_sources.
    Results are merged via update() after all handlers complete.
    """

    DESCRIPTION = (
        "Report this project's identity, declared dependencies, and preset sources."
    )
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
    PAYLOAD_TYPE = CollectProjectDependencyInfoRunPayload
    RUN_CONTEXT_TYPE = CollectProjectDependencyInfoRunContext
    RESULT_TYPE = CollectProjectDependencyInfoRunResult
