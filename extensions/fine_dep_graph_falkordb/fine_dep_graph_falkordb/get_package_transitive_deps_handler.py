from fine_dep_graph.get_package_transitive_deps_action import (
    GetPackageTransitiveDepsAction,
    GetPackageTransitiveDepsRunContext,
    GetPackageTransitiveDepsRunPayload,
    GetPackageTransitiveDepsRunResult,
)
from finecode_extension_api import code_action

from fine_dep_graph_falkordb.ifalkordb_client_provider import IFalkorDBClientProvider


class GetPackageTransitiveDepsHandler(
    code_action.ActionHandler[
        GetPackageTransitiveDepsAction,
        code_action.ActionHandlerConfig,
    ]
):
    def __init__(self, falkordb_client_provider: IFalkorDBClientProvider) -> None:
        self.falkordb_client_provider = falkordb_client_provider

    async def run(
        self,
        payload: GetPackageTransitiveDepsRunPayload,
        context: GetPackageTransitiveDepsRunContext,
    ) -> GetPackageTransitiveDepsRunResult:
        graph = self.falkordb_client_provider.get_graph()
        result = graph.query(
            "MATCH (p:Package {name: $name})-[:DEPENDS_ON*]->(dep) RETURN dep.name",
            {"name": payload.package_name},
        )
        names = [row[0] for row in result.result_set]
        return GetPackageTransitiveDepsRunResult(dependency_names=names)
