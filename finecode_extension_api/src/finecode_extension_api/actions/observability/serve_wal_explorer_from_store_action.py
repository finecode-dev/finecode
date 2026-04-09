# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri

DEFAULT_WAL_EXPLORER_PORT = 8765


@dataclasses.dataclass
class ServeWalExplorerFromStoreRunPayload(code_action.RunActionPayload):
    store_uri: ResourceUri | None = None
    host: str = "127.0.0.1"
    port: int = DEFAULT_WAL_EXPLORER_PORT
    read_only: bool = True


class ServeWalExplorerFromStoreRunContext(
    code_action.RunActionContext[ServeWalExplorerFromStoreRunPayload]
):
    pass


@dataclasses.dataclass
class ServeWalExplorerFromStoreRunResult(code_action.RunActionResult):
    schema_version: int
    base_url: str
    bound_host: str
    bound_port: int
    store_uri: ResourceUri
    warnings: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        return

    def to_text(self) -> str | textstyler.StyledText:
        lines = [f"WAL Explorer serving at {self.base_url}"]
        lines.append(f"  store: {self.store_uri}")
        if self.warnings:
            for warning in self.warnings:
                lines.append(f"  warning: {warning}")
        return "\n".join(lines)


class ServeWalExplorerFromStoreAction(
    code_action.Action[
        ServeWalExplorerFromStoreRunPayload,
        ServeWalExplorerFromStoreRunContext,
        ServeWalExplorerFromStoreRunResult,
    ]
):
    """Start a HTTP API over the WAL store and serve until interrupted."""

    PAYLOAD_TYPE = ServeWalExplorerFromStoreRunPayload
    RUN_CONTEXT_TYPE = ServeWalExplorerFromStoreRunContext
    RESULT_TYPE = ServeWalExplorerFromStoreRunResult
