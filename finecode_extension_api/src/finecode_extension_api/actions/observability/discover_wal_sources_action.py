import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.actions.observability.ingest_wal_to_store_action import (
    WalSourceSpec,
)


@dataclasses.dataclass
class DiscoverWalSourcesRunPayload(code_action.RunActionPayload):
    pass


class DiscoverWalSourcesRunContext(
    code_action.RunActionContext[DiscoverWalSourcesRunPayload]
):
    pass


@dataclasses.dataclass
class DiscoverWalSourcesRunResult(code_action.RunActionResult):
    source_specs: list[WalSourceSpec]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, DiscoverWalSourcesRunResult):
            return
        self.source_specs += other.source_specs

    def to_text(self) -> str | textstyler.StyledText:
        if len(self.source_specs) == 0:
            return "No WAL sources discovered"
        return "\n".join(item.source_id for item in self.source_specs)


class DiscoverWalSourcesAction(
    code_action.Action[
        DiscoverWalSourcesRunPayload,
        DiscoverWalSourcesRunContext,
        DiscoverWalSourcesRunResult,
    ]
):
    """Discover WAL sources."""

    PAYLOAD_TYPE = DiscoverWalSourcesRunPayload
    RUN_CONTEXT_TYPE = DiscoverWalSourcesRunContext
    RESULT_TYPE = DiscoverWalSourcesRunResult
