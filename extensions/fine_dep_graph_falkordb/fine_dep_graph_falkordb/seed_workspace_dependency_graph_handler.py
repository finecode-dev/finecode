import pathlib
import re
import tomllib

from fine_dep_graph.collect_project_dependency_info_action import (
    CollectProjectDependencyInfoAction,
    CollectProjectDependencyInfoRunPayload,
    CollectProjectDependencyInfoRunResult,
)
from fine_dep_graph.seed_workspace_dependency_graph_action import (
    SeedWorkspaceDependencyGraphAction,
    SeedWorkspaceDependencyGraphRunContext,
    SeedWorkspaceDependencyGraphRunPayload,
    SeedWorkspaceDependencyGraphRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)

from fine_dep_graph_falkordb.ifalkordb_client_provider import (
    FalkorDBNotInitializedError,
    IFalkorDBClientProvider,
)

_NORMALIZE_RE = re.compile(r"[-_.]+")


def _normalize_name(name: str) -> str:
    """PEP 503 canonical name: lowercase with runs of [-_.] replaced by a single dash."""
    return _NORMALIZE_RE.sub("-", name).lower()


def _infer_package_kind(project_path: pathlib.Path) -> str:
    lower_parts = [p.lower() for p in project_path.parts]
    if "extensions" in lower_parts:
        return "extension"
    if "presets" in lower_parts:
        return "preset"
    return "core"


def _read_package_name(project_path: pathlib.Path) -> str:
    pyproject_path = project_path / "pyproject.toml"
    if not pyproject_path.exists():
        return ""
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    return data.get("project", {}).get("name", "")


class SeedWorkspaceDependencyGraphHandler(
    code_action.ActionHandler[
        SeedWorkspaceDependencyGraphAction,
        code_action.ActionHandlerConfig,
    ]
):
    def __init__(
        self,
        falkordb_client_provider: IFalkorDBClientProvider,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
    ) -> None:
        self.falkordb_client_provider = falkordb_client_provider
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider

    async def run(
        self,
        payload: SeedWorkspaceDependencyGraphRunPayload,
        run_context: SeedWorkspaceDependencyGraphRunContext,
    ) -> SeedWorkspaceDependencyGraphRunResult:
        projects = await self.workspace_info_provider.get_workspace_projects()
        project_paths = actionable_project_paths(projects)

        results: dict[
            object, CollectProjectDependencyInfoRunResult
        ] = await self.workspace_action_runner.run_action_in_projects(
            action_type=CollectProjectDependencyInfoAction,
            payload=CollectProjectDependencyInfoRunPayload(),
            meta=run_context.meta,
            project_paths=project_paths,
            concurrently=True,
        )

        try:
            graph = self.falkordb_client_provider.get_graph()
        except FalkorDBNotInitializedError:
            raise code_action.ActionFailedException(
                "FalkorDB not initialized. Run init_falkordb first."
            )

        if payload.clear_existing:
            graph.query("MATCH (n) DETACH DELETE n")

        # Build local_packages from ALL workspace projects by reading pyproject.toml
        # directly, so packages without [tool.finecode] are still recognized as graph
        # nodes and can be targets of DEPENDS_ON / USES_PRESET edges.
        local_packages: dict[str, str] = {}
        for project in projects:
            package_name = _read_package_name(project.path)
            if package_name:
                local_packages[_normalize_name(package_name)] = package_name
                graph.query(
                    "MERGE (:Package {name: $name, kind: $kind})",
                    {"name": package_name, "kind": _infer_package_kind(project.path)},
                )

        depends_on_edges = 0
        uses_preset_edges = 0
        warnings: list[str] = []

        for project_path, result in results.items():
            if not result.package_name:
                warnings.append(
                    f"Project at {project_path} has no package name — skipped."
                )
                continue

            pyproject_source = str(project_path / "pyproject.toml")

            for dep_name in result.pyproject_dependencies:
                target = local_packages.get(_normalize_name(dep_name))
                if target is not None:
                    graph.query(
                        "MATCH (a:Package {name: $src}), (b:Package {name: $dst}) "
                        "CREATE (a)-[:DEPENDS_ON {source_file: $src_file}]->(b)",
                        {
                            "src": result.package_name,
                            "dst": target,
                            "src_file": pyproject_source,
                        },
                    )
                    depends_on_edges += 1

            for preset_source in result.used_preset_sources:
                target = local_packages.get(_normalize_name(preset_source))
                if target is not None:
                    graph.query(
                        "MATCH (a:Package {name: $src}), (b:Package {name: $dst}) "
                        "CREATE (a)-[:USES_PRESET {source_file: $src_file}]->(b)",
                        {
                            "src": result.package_name,
                            "dst": target,
                            "src_file": pyproject_source,
                        },
                    )
                    uses_preset_edges += 1

        return SeedWorkspaceDependencyGraphRunResult(
            packages_indexed=len(local_packages),
            depends_on_edges=depends_on_edges,
            uses_preset_edges=uses_preset_edges,
            warnings=warnings,
        )
