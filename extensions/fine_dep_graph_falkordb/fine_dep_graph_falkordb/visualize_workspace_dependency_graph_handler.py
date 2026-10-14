import re

from fine_dep_graph.visualize_workspace_dependency_graph_action import (
    VisualizeWorkspaceDependencyGraphAction,
    VisualizeWorkspaceDependencyGraphRunContext,
    VisualizeWorkspaceDependencyGraphRunPayload,
    VisualizeWorkspaceDependencyGraphRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_dep_graph_falkordb.ifalkordb_client_provider import (
    FalkorDBNotInitializedError,
    IFalkorDBClientProvider,
)


def _generate_mermaid(
    nodes: list[tuple[str, str]],
    depends_on_edges: list[tuple[str, str]],
    uses_preset_edges: list[tuple[str, str]],
) -> str:
    lines = ["graph TD"]
    id_map: dict[str, str] = {}
    for i, (name, kind) in enumerate(nodes):
        safe_id = f"N{i}"
        id_map[name] = safe_id
        lines.append(f'    {safe_id}["{name}\\n({kind})"]')
    for src, dst in depends_on_edges:
        if src in id_map and dst in id_map:
            lines.append(f"    {id_map[src]} -->|DEPENDS_ON| {id_map[dst]}")
    for src, dst in uses_preset_edges:
        if src in id_map and dst in id_map:
            lines.append(f"    {id_map[src]} -.->|USES_PRESET| {id_map[dst]}")
    return "\n".join(lines)


def _generate_dot(
    nodes: list[tuple[str, str]],
    depends_on_edges: list[tuple[str, str]],
    uses_preset_edges: list[tuple[str, str]],
) -> str:
    def safe_id(name: str) -> str:

        return re.sub(r"[^A-Za-z0-9_]", "_", name)

    lines = ["digraph workspace_deps {"]
    for name, kind in nodes:
        lines.append(f'    {safe_id(name)} [label="{name}\\n({kind})"];')
    for src, dst in depends_on_edges:
        lines.append(f'    {safe_id(src)} -> {safe_id(dst)} [label="DEPENDS_ON"];')
    for src, dst in uses_preset_edges:
        lines.append(
            f'    {safe_id(src)} -> {safe_id(dst)} [label="USES_PRESET" style="dashed"];'
        )
    lines.append("}")
    return "\n".join(lines)


class VisualizeWorkspaceDependencyGraphHandler(
    code_action.ActionHandler[
        VisualizeWorkspaceDependencyGraphAction,
        code_action.ActionHandlerConfig,
    ]
):
    def __init__(self, falkordb_client_provider: IFalkorDBClientProvider) -> None:
        self.falkordb_client_provider = falkordb_client_provider

    async def run(
        self,
        payload: VisualizeWorkspaceDependencyGraphRunPayload,
        run_context: VisualizeWorkspaceDependencyGraphRunContext,
    ) -> VisualizeWorkspaceDependencyGraphRunResult:
        try:
            graph = self.falkordb_client_provider.get_graph()
        except FalkorDBNotInitializedError:
            raise code_action.ActionFailedException(
                "FalkorDB not initialized. Run init_falkordb first."
            )

        nodes_result = graph.query("MATCH (p:Package) RETURN p.name, p.kind")
        nodes = [(row[0], row[1]) for row in nodes_result.result_set]

        deps_result = graph.query(
            "MATCH (a:Package)-[:DEPENDS_ON]->(b:Package) RETURN a.name, b.name"
        )
        depends_on_edges = [(row[0], row[1]) for row in deps_result.result_set]

        presets_result = graph.query(
            "MATCH (a:Package)-[:USES_PRESET]->(b:Package) RETURN a.name, b.name"
        )
        uses_preset_edges = [(row[0], row[1]) for row in presets_result.result_set]

        if payload.format == "mermaid":
            diagram = _generate_mermaid(nodes, depends_on_edges, uses_preset_edges)
        else:
            diagram = _generate_dot(nodes, depends_on_edges, uses_preset_edges)

        output_path = None
        if payload.output_path is not None:
            path = resource_uri_to_path(payload.output_path)
            path.write_text(diagram)
            output_path = payload.output_path

        return VisualizeWorkspaceDependencyGraphRunResult(
            diagram=diagram,
            output_path=output_path,
        )
