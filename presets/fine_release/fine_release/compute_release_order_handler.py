from __future__ import annotations

import asyncio
import dataclasses

from fine_dep_graph.detect_workspace_dependency_cycles_action import (
    DetectWorkspaceDependencyCyclesAction,
    DetectWorkspaceDependencyCyclesRunPayload,
)
from fine_dep_graph.get_package_transitive_deps_action import (
    GetPackageTransitiveDepsAction,
    GetPackageTransitiveDepsRunPayload,
)
from fine_dep_graph.seed_workspace_dependency_graph_action import (
    SeedWorkspaceDependencyGraphAction,
    SeedWorkspaceDependencyGraphRunPayload,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iworkspaceactionrunner

from fine_release.release_workspace_packages_action import (
    ReleaseWorkspacePackagesAction,
    ReleaseWorkspacePackagesRunContext,
    ReleaseWorkspacePackagesRunPayload,
    ReleaseWorkspacePackagesRunResult,
    _Candidate,
)


@dataclasses.dataclass
class ComputeReleaseOrderHandlerConfig(code_action.ActionHandlerConfig): ...


class ComputeReleaseOrderHandler(
    code_action.ActionHandler[
        ReleaseWorkspacePackagesAction,
        ComputeReleaseOrderHandlerConfig,
    ]
):
    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner

    async def run(
        self,
        payload: ReleaseWorkspacePackagesRunPayload,
        run_context: ReleaseWorkspacePackagesRunContext,
    ) -> ReleaseWorkspacePackagesRunResult:
        state = run_context.state
        candidate_names = set(state.candidates_by_name.keys())

        await self.workspace_action_runner.run_action_in_projects(
            action_type=SeedWorkspaceDependencyGraphAction,
            payload=SeedWorkspaceDependencyGraphRunPayload(),
            meta=run_context.meta,
            project_paths=None,
            concurrently=True,
        )

        cycles_results = await self.workspace_action_runner.run_action_in_projects(
            action_type=DetectWorkspaceDependencyCyclesAction,
            payload=DetectWorkspaceDependencyCyclesRunPayload(),
            meta=run_context.meta,
            project_paths=None,
            concurrently=True,
        )
        for cycles_result in cycles_results.values():
            for cycle in cycles_result.cycles:
                if len(candidate_names & set(cycle)) >= 2:
                    raise code_action.ActionFailedException(
                        f"Dependency cycle detected among release candidates: {cycle}"
                    )

        async def _get_transitive_deps(
            name: str, candidate: _Candidate
        ) -> tuple[str, set[str]]:
            transitive_deps_result = (
                await self.workspace_action_runner.run_action_in_projects(
                    action_type=GetPackageTransitiveDepsAction,
                    payload=GetPackageTransitiveDepsRunPayload(package_name=name),
                    meta=run_context.meta,
                    project_paths=[candidate.project_path],
                    concurrently=True,
                )
            )
            dependency_names = transitive_deps_result[
                candidate.project_path
            ].dependency_names
            return name, candidate_names & set(dependency_names)

        dependencies_by_name: dict[str, set[str]] = dict(
            await asyncio.gather(
                *(
                    _get_transitive_deps(name, candidate)
                    for name, candidate in state.candidates_by_name.items()
                )
            )
        )

        ordered: list[str] = []
        remaining = set(candidate_names)
        while remaining:
            ordered_set = set(ordered)
            ready = sorted(
                name for name in remaining if dependencies_by_name[name] <= ordered_set
            )
            if not ready:
                raise code_action.ActionFailedException(
                    "Could not compute a release order for remaining candidates: "
                    f"{sorted(remaining)}"
                )
            ordered.append(ready[0])
            remaining.remove(ready[0])

        state.ordered_names = ordered
        state.dependencies_by_name = dependencies_by_name

        return ReleaseWorkspacePackagesRunResult(dry_run=payload.dry_run)
