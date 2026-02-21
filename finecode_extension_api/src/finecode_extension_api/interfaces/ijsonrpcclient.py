import collections.abc
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self


class IJsonRpcSession(Protocol):
    """An active JSON-RPC connection session.

    Use as an async context manager: ``__aenter__`` starts the subprocess,
    ``__aexit__`` stops it.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    # -- Async API (for use from async extension handlers) -----------------

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send a JSON-RPC request and wait for the response.

        Args:
            method: The JSON-RPC method name.
            params: Optional parameters for the request.
            timeout: Optional timeout in seconds.

        Returns:
            The ``result`` field from the JSON-RPC response.
        """
        ...

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        ...

    # -- Sync API (blocks caller thread, IO thread resolves) ---------------

    def send_request_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send a JSON-RPC request synchronously.

        Blocks the calling thread until the IO thread receives the response.
        """
        ...

    def send_notification_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC notification synchronously."""
        ...

    # -- Server-initiated messages -----------------------------------------

    def on_notification(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[None]
        ],
    ) -> None:
        """Register a handler for incoming notifications from the server.

        Args:
            method: The notification method name to handle.
            handler: Async callable that receives the notification params.
        """
        ...

    def on_request(
        self,
        method: str,
        handler: collections.abc.Callable[
            [dict[str, Any] | None], collections.abc.Awaitable[Any]
        ],
    ) -> None:
        """Register a handler for incoming requests from the server.

        Args:
            method: The request method name.
            handler: Async callable that receives params and returns the result.
        """
        ...


class IJsonRpcClient(Protocol):
    """Factory for creating JSON-RPC sessions."""

    def session(
        self,
        cmd: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        readable_id: str = "",
    ) -> IJsonRpcSession:
        """Create a new JSON-RPC session that launches a subprocess.

        Usage::

            async with json_rpc_client.session("some-server --stdio") as session:
                result = await session.send_request("method", {"key": "value"})

        Args:
            cmd: Shell command to start the JSON-RPC server process.
            cwd: Working directory for the subprocess.
            env: Environment variables for the subprocess.
            readable_id: Human-readable identifier for logging.

        Returns:
            An async context manager yielding IJsonRpcSession.
        """
        ...
