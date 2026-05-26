# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri

DEFAULT_DOCS_SERVER_PORT = 8000


@dataclasses.dataclass
class ServeDocsRunPayload(code_action.RunActionPayload):
    docs_source_dir: ResourceUri | None = None
    """``file://`` URI of the documentation source directory.
    None = infer from project tool config (e.g. mkdocs.yml, docs/conf.py)."""
    host: str = "127.0.0.1"
    """Host address the dev server binds to."""
    port: int = DEFAULT_DOCS_SERVER_PORT
    """Port the dev server listens on."""


class ServeDocsRunContext(code_action.RunActionContext[ServeDocsRunPayload]):
    ...


@dataclasses.dataclass
class ServeDocsRunResult(code_action.RunActionResult):
    base_url: str | None = None
    """URL at which the documentation is being served, e.g. ``http://127.0.0.1:8000/``.
    Populated in the first partial result, before the server blocks."""
    bound_host: str | None = None
    bound_port: int | None = None

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ServeDocsRunResult):
            return
        if other.base_url is not None:
            self.base_url = other.base_url
        if other.bound_host is not None:
            self.bound_host = other.bound_host
        if other.bound_port is not None:
            self.bound_port = other.bound_port

    def to_text(self) -> str | textstyler.StyledText:
        if self.base_url:
            return f"Documentation server running at {self.base_url}"
        return "Documentation server started."


class ServeDocsAction(
    code_action.Action[
        ServeDocsRunPayload,
        ServeDocsRunContext,
        ServeDocsRunResult,
    ]
):
    """Start a local documentation development server that watches sources and live-reloads.

    Runs until interrupted. The handler yields a partial result with base_url and
    bound_port as soon as the server is ready to accept connections, then blocks
    until cancelled.

    When docs_source_dir is None, the handler infers the source location from the
    project's tool configuration file (e.g. mkdocs.yml, docs/conf.py, book.toml).
    Tool-specific options (open browser, watch extra dirs, strict mode) belong in
    handler configuration.
    """

    DESCRIPTION = "Start a local documentation development server that watches sources and live-reloads."
    PAYLOAD_TYPE = ServeDocsRunPayload
    RUN_CONTEXT_TYPE = ServeDocsRunContext
    RESULT_TYPE = ServeDocsRunResult
