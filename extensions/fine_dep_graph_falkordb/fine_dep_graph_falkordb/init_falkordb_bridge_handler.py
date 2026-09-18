from fine_dep_graph.seed_workspace_dependency_graph_action import (
    SeedWorkspaceDependencyGraphAction,
    SeedWorkspaceDependencyGraphRunContext,
    SeedWorkspaceDependencyGraphRunPayload,
    SeedWorkspaceDependencyGraphRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.interfaces.iprojectactionrunner import ActionRef

from fine_dep_graph_falkordb.init_falkordb_action import (
    InitFalkorDBAction,
    InitFalkorDBRunPayload,
)


class InitFalkorDBBridgeHandler(
    code_action.ActionHandler[
        SeedWorkspaceDependencyGraphAction,
        code_action.ActionHandlerConfig,
    ]
):
    """Run init_falkordb before seeding so FalkorDB is ready.

    Register this handler before SeedWorkspaceDependencyGraphHandler. Disable it
    in project config if you call init_falkordb manually with custom settings.
    """

    def __init__(
        self,
        project_action_runner: iprojectactionrunner.IProjectActionRunner,
    ) -> None:
        self.project_action_runner = project_action_runner

    async def run(
        self,
        payload: SeedWorkspaceDependencyGraphRunPayload,
        run_context: SeedWorkspaceDependencyGraphRunContext,
    ) -> SeedWorkspaceDependencyGraphRunResult:
        try:
            await self.project_action_runner.run_action(
                ActionRef.from_type(InitFalkorDBAction),
                InitFalkorDBRunPayload(),
                meta=run_context.meta,
            )
        except iprojectactionrunner.ActionRunFailed as e:
            raise code_action.ActionFailedException(
                f"Failed to initialize FalkorDB: {e.message}"
            ) from e

        return SeedWorkspaceDependencyGraphRunResult()
