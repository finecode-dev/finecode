import collections.abc
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self


class ILspSession(Protocol):
    """An active LSP session with a language server.

    Use as an async context manager:

    - ``__aenter__`` starts the process, sends ``initialize`` request,
      sends ``initialized`` notification.
    - ``__aexit__`` sends ``shutdown`` request, sends ``exit`` notification,
      stops the process.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    # -- Async API ---------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send an LSP request and return the result."""
        ...

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send an LSP notification."""
        ...

    # -- Sync API ----------------------------------------------------------

    def send_request_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send an LSP request synchronously (blocks caller thread)."""
        ...

    def send_notification_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send an LSP notification synchronously."""
        ...

    # -- Server-initiated messages -----------------------------------------

    def on_notification(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[None]
        ],
    ) -> None:
        """Register handler for server notifications."""
        ...

    def on_request(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[Any]
        ],
    ) -> None:
        """Register handler for server-to-client requests."""
        ...

    # -- Server info -------------------------------------------------------

    @property
    def server_capabilities(self) -> dict[str, Any]:
        """Capabilities returned by the server in the initialize response."""
        ...

    @property
    def server_info(self) -> dict[str, Any] | None:
        """Server info returned in the initialize response, if any."""
        ...


class ILspClient(Protocol):
    """Factory for creating LSP sessions with language servers."""

    def session(
        self,
        cmd: str,
        root_uri: str,
        workspace_folders: list[dict[str, str]] | None = None,
        initialization_options: dict[str, Any] | None = None,
        client_capabilities: dict[str, Any] | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        readable_id: str = "",
    ) -> ILspSession:
        """Create a new LSP session that launches a language server.

        The session automatically performs the LSP initialization handshake.

        Usage::

            async with lsp_client.session(
                cmd="pyright-langserver --stdio",
                root_uri="file:///path/to/project",
            ) as session:
                result = await session.send_request(
                    "textDocument/completion",
                    {"textDocument": {"uri": "file:///file.py"}, "position": {"line": 0, "character": 0}},
                )

        Args:
            cmd: Shell command to start the language server.
            root_uri: The root URI of the workspace.
            workspace_folders: Optional workspace folders (each with 'uri' and 'name' keys).
            initialization_options: Optional server-specific initialization options.
            client_capabilities: Optional client capabilities override.
            cwd: Working directory for the subprocess.
            env: Environment variables for the subprocess.
            readable_id: Human-readable identifier for logging.

        Returns:
            An async context manager yielding ILspSession.
        """
        ...
