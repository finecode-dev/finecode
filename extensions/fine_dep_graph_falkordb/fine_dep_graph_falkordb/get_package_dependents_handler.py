from fine_dep_graph.get_package_dependents_action import (
    GetPackageDependentsAction,
    GetPackageDependentsRunContext,
    GetPackageDependentsRunPayload,
    GetPackageDependentsRunResult,
)
from finecode_extension_api import code_action

from fine_dep_graph_falkordb.ifalkordb_client_provider import IFalkorDBClientProvider


class GetPackageDependentsHandler(
    code_action.ActionHandler[
        GetPackageDependentsAction,
        code_action.ActionHandlerConfig,
    ]
):
    def __init__(self, falkordb_client_provider: IFalkorDBClientProvider) -> None:
        self.falkordb_client_provider = falkordb_client_provider

    async def run(
        self,
        payload: GetPackageDependentsRunPayload,
        context: GetPackageDependentsRunContext,
    ) -> GetPackageDependentsRunResult:
        graph = self.falkordb_client_provider.get_graph()
        result = graph.query(
            "MATCH (dep)-[:DEPENDS_ON*]->(p:Package {name: $name}) RETURN dep.name",
            {"name": payload.package_name},
        )
        names = [row[0] for row in result.result_set]
        return GetPackageDependentsRunResult(dependent_names=names)
