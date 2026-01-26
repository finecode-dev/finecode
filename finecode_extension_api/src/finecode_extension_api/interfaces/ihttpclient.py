from types import TracebackType
from typing import Any, Protocol, Self


class IHttpResponse(Protocol):
    """Protocol for HTTP response objects."""

    @property
    def status_code(self) -> int:
        """HTTP status code (e.g., 200, 404)."""
        ...

    @property
    def headers(self) -> dict[str, str]:
        """Response headers."""
        ...

    @property
    def content(self) -> bytes:
        """Raw response content as bytes."""
        ...

    @property
    def text(self) -> str:
        """Response content as text."""
        ...

    def json(self) -> Any:
        """Parse response content as JSON."""
        ...

    def raise_for_status(self) -> None:
        """Raise an exception if the response status indicates an error."""
        ...


class IHttpSession(Protocol):
    """Protocol for HTTP session that manages a connection and can be used as a context manager."""

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit. Automatically closes the session."""
        ...

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> IHttpResponse:
        """
        Send an HTTP GET request.

        Args:
            url: The URL to request
            headers: Optional request headers
            params: Optional query parameters
            timeout: Optional timeout in seconds

        Returns:
            HTTP response object
        """
        ...

    async def post(
        self,
        url: str,
        data: bytes | str | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> IHttpResponse:
        """
        Send an HTTP POST request.

        Args:
            url: The URL to request
            data: Request body data
            json: JSON data to send (automatically sets Content-Type)
            headers: Optional request headers
            timeout: Optional timeout in seconds

        Returns:
            HTTP response object
        """
        ...

    async def put(
        self,
        url: str,
        data: bytes | str | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> IHttpResponse:
        """
        Send an HTTP PUT request.

        Args:
            url: The URL to request
            data: Request body data
            json: JSON data to send (automatically sets Content-Type)
            headers: Optional request headers
            timeout: Optional timeout in seconds

        Returns:
            HTTP response object
        """
        ...

    async def delete(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> IHttpResponse:
        """
        Send an HTTP DELETE request.

        Args:
            url: The URL to request
            headers: Optional request headers
            timeout: Optional timeout in seconds

        Returns:
            HTTP response object
        """
        ...

    async def head(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> IHttpResponse:
        """
        Send an HTTP HEAD request.

        Args:
            url: The URL to request
            headers: Optional request headers
            timeout: Optional timeout in seconds

        Returns:
            HTTP response object
        """
        ...

    async def request(
        self,
        method: str,
        url: str,
        data: bytes | str | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> IHttpResponse:
        """
        Send an HTTP request with any method.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            url: The URL to request
            data: Request body data
            json: JSON data to send (automatically sets Content-Type)
            headers: Optional request headers
            params: Optional query parameters
            timeout: Optional timeout in seconds

        Returns:
            HTTP response object
        """
        ...


class IHttpClient(Protocol):
    """Protocol for HTTP client factory that creates sessions."""

    def session(self) -> IHttpSession:
        """
        Create a new HTTP session.

        Returns:
            A new HTTP session that should be used as a context manager
        """
        ...
