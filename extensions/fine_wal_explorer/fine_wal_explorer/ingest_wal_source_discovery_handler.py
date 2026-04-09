import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.discover_wal_sources_action import (
    DiscoverWalSourcesAction,
    DiscoverWalSourcesRunPayload,
)
from finecode_extension_api.actions.observability.ingest_wal_to_store_action import (
    IngestWalToStoreAction,
    IngestWalToStoreRunContext,
    IngestWalToStoreRunPayload,
    IngestWalToStoreRunResult,
)
from finecode_extension_api.interfaces import iactionrunner


@dataclasses.dataclass
class IngestWalSourceDiscoveryHandlerConfig(code_action.ActionHandlerConfig): ...


class IngestWalSourceDiscoveryHandler(
    code_action.ActionHandler[
        IngestWalToStoreAction,
        IngestWalSourceDiscoveryHandlerConfig,
    ]
):
    """Discovery bridge: calls DiscoverWalSourcesAction to populate source_specs when not provided."""

    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
    ) -> None:
        self.action_runner = action_runner

    async def run(
        self,
        payload: IngestWalToStoreRunPayload,
        run_context: code_action.RunActionContext[IngestWalToStoreRunPayload],
    ) -> IngestWalToStoreRunResult:
        if not isinstance(run_context, IngestWalToStoreRunContext):
            raise code_action.ActionFailedException(
                "IngestWalSourceDiscoveryHandler requires IngestWalToStoreRunContext"
            )

        if run_context.source_specs is not None:
            return IngestWalToStoreRunResult()

        discover_action = self.action_runner.get_action_by_source(DiscoverWalSourcesAction)
        discover_result = await self.action_runner.run_action(
            action=discover_action,
            payload=DiscoverWalSourcesRunPayload(),
            meta=run_context.meta,
        )
        run_context.source_specs = discover_result.source_specs
        return IngestWalToStoreRunResult()
