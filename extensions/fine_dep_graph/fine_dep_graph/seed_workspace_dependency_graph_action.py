import dataclasses

from finecode_extension_api import code_action


@dataclasses.dataclass
class SeedWorkspaceDependencyGraphRunPayload(code_action.RunActionPayload):
    clear_existing: bool = True
    """Wipe all existing nodes and edges before seeding."""


@dataclasses.dataclass
class SeedWorkspaceDependencyGraphRunResult(code_action.RunActionResult):
    packages_indexed: int = 0
    depends_on_edges: int = 0
    uses_preset_edges: int = 0
    warnings: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: "SeedWorkspaceDependencyGraphRunResult") -> None:
        self.packages_indexed += other.packages_indexed
        self.depends_on_edges += other.depends_on_edges
        self.uses_preset_edges += other.uses_preset_edges
        self.warnings.extend(other.warnings)


class SeedWorkspaceDependencyGraphRunContext(
    code_action.RunActionContext[SeedWorkspaceDependencyGraphRunPayload]
): ...


class SeedWorkspaceDependencyGraphAction(
    code_action.Action[
        SeedWorkspaceDependencyGraphRunPayload,
        SeedWorkspaceDependencyGraphRunContext,
        SeedWorkspaceDependencyGraphRunResult,
    ]
):
    """Seed the FalkorDB dependency graph from current workspace state.

    Fans out collect_project_dependency_info to every project, then writes Package
    nodes and DEPENDS_ON / USES_PRESET edges for dependencies that resolve to local
    workspace packages. Only intra-workspace edges are written; third-party
    dependencies are not represented in the graph.

    When clear_existing is True (default), all nodes and edges are deleted before
    the new data is written. Set it to False to incrementally add or update entries
    without wiping the graph first.
    """

    DESCRIPTION = "Seed the FalkorDB dependency graph from the current workspace state."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = SeedWorkspaceDependencyGraphRunPayload
    RUN_CONTEXT_TYPE = SeedWorkspaceDependencyGraphRunContext
    RESULT_TYPE = SeedWorkspaceDependencyGraphRunResult
