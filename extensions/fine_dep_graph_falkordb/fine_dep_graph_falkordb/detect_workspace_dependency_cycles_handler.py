from fine_dep_graph.detect_workspace_dependency_cycles_action import (
    DetectWorkspaceDependencyCyclesAction,
    DetectWorkspaceDependencyCyclesRunContext,
    DetectWorkspaceDependencyCyclesRunPayload,
    DetectWorkspaceDependencyCyclesRunResult,
)
from finecode_extension_api import code_action

from fine_dep_graph_falkordb.ifalkordb_client_provider import IFalkorDBClientProvider


class DetectWorkspaceDependencyCyclesHandler(
    code_action.ActionHandler[
        DetectWorkspaceDependencyCyclesAction,
        code_action.ActionHandlerConfig,
    ]
):
    def __init__(self, falkordb_client_provider: IFalkorDBClientProvider) -> None:
        self.falkordb_client_provider = falkordb_client_provider

    async def run(
        self,
        payload: DetectWorkspaceDependencyCyclesRunPayload,
        context: DetectWorkspaceDependencyCyclesRunContext,
    ) -> DetectWorkspaceDependencyCyclesRunResult:
        graph = self.falkordb_client_provider.get_graph()
        result = graph.query(
            "MATCH path = (p:Package)-[:DEPENDS_ON*]->(p) RETURN nodes(path)"
        )
        cycles: list[list[str]] = []
        for row in result.result_set:
            nodes = row[0]
            cycle = [node.properties.get("name", "") for node in nodes]
            cycles.append(cycle)
        return DetectWorkspaceDependencyCyclesRunResult(cycles=cycles)
