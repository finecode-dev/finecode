import dataclasses

from finecode_extension_api import code_action

from fine_dep_graph_falkordb.falkordb_client_provider import FalkorDBClientProvider
from fine_dep_graph_falkordb.init_falkordb_action import (
    InitFalkorDBAction,
    InitFalkorDBRunContext,
    InitFalkorDBRunPayload,
    InitFalkorDBRunResult,
)


@dataclasses.dataclass
class InitFalkorDBHandlerConfig(code_action.ActionHandlerConfig):
    falkordb_host: str = "localhost"
    """FalkorDB server hostname."""
    falkordb_port: int = 6379
    """FalkorDB server port."""
    graph_name: str = "workspace_deps"
    """Name of the FalkorDB graph to use."""


class InitFalkorDBHandler(
    code_action.ActionHandler[
        InitFalkorDBAction,
        InitFalkorDBHandlerConfig,
    ]
):
    def __init__(
        self,
        config: InitFalkorDBHandlerConfig,
        falkordb_client_provider: FalkorDBClientProvider,
    ) -> None:
        self.config = config
        self.falkordb_client_provider = falkordb_client_provider

    async def run(
        self,
        payload: InitFalkorDBRunPayload,
        run_context: InitFalkorDBRunContext,
    ) -> InitFalkorDBRunResult:
        import falkordb

        try:
            client = falkordb.FalkorDB(
                host=self.config.falkordb_host,
                port=self.config.falkordb_port,
            )
        except Exception as e:
            raise code_action.ActionFailedException(
                f"Cannot connect to FalkorDB at {self.config.falkordb_host}:{self.config.falkordb_port}: {e}"
            ) from e
        self.falkordb_client_provider.set_client(client, self.config.graph_name)
        return InitFalkorDBRunResult(connected=True)
