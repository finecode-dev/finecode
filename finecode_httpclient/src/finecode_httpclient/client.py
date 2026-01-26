from types import TracebackType
from typing import Any, Self

import httpx

from finecode_extension_api.interfaces import ihttpclient, ilogger


class HttpResponse(ihttpclient.IHttpResponse):
    """Wrapper for httpx.Response that implements IHttpResponse protocol."""

    def __init__(self, response: httpx.Response):
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._response.headers)

    @property
    def content(self) -> bytes:
        return self._response.content

    @property
    def text(self) -> str:
        return self._response.text

    def json(self) -> Any:
        return self._response.json()

    def raise_for_status(self) -> None:
        self._response.raise_for_status()


class HttpSession(ihttpclient.IHttpSession):
    """HTTP session implementation using httpx.AsyncClient."""

    def __init__(self, logger: ilogger.ILogger):
        self.logger = logger
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        """Async context manager entry. Creates and initializes the httpx client."""
        self.logger.debug("HTTP session opened")
        self._client = httpx.AsyncClient()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit. Automatically closes the client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self.logger.debug("HTTP session closed")

    def _ensure_client(self) -> httpx.AsyncClient:
        """Ensure the client is initialized."""
        if self._client is None:
            raise RuntimeError(
                "HTTP session not initialized. Use 'async with session:' context manager."
            )
        return self._client

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ihttpclient.IHttpResponse:
        self.logger.debug(f"HTTP GET: {url}")
        client = self._ensure_client()
        response = await client.get(
            url, headers=headers, params=params, timeout=timeout
        )
        return HttpResponse(response)

    async def post(
        self,
        url: str,
        data: bytes | str | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ihttpclient.IHttpResponse:
        self.logger.debug(f"HTTP POST: {url}")
        client = self._ensure_client()
        response = await client.post(
            url, data=data, json=json, headers=headers, timeout=timeout
        )
        return HttpResponse(response)

    async def put(
        self,
        url: str,
        data: bytes | str | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ihttpclient.IHttpResponse:
        self.logger.debug(f"HTTP PUT: {url}")
        client = self._ensure_client()
        response = await client.put(
            url, data=data, json=json, headers=headers, timeout=timeout
        )
        return HttpResponse(response)

    async def delete(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ihttpclient.IHttpResponse:
        self.logger.debug(f"HTTP DELETE: {url}")
        client = self._ensure_client()
        response = await client.delete(url, headers=headers, timeout=timeout)
        return HttpResponse(response)

    async def head(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ihttpclient.IHttpResponse:
        self.logger.debug(f"HTTP HEAD: {url}")
        client = self._ensure_client()
        response = await client.head(url, headers=headers, timeout=timeout)
        return HttpResponse(response)

    async def request(
        self,
        method: str,
        url: str,
        data: bytes | str | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ihttpclient.IHttpResponse:
        self.logger.debug(f"HTTP {method.upper()}: {url}")
        client = self._ensure_client()
        response = await client.request(
            method,
            url,
            data=data,
            json=json,
            headers=headers,
            params=params,
            timeout=timeout,
        )
        return HttpResponse(response)


class HttpClient(ihttpclient.IHttpClient):
    """HTTP client factory that creates sessions."""

    def __init__(self, logger: ilogger.ILogger):
        self.logger = logger

    def session(self) -> ihttpclient.IHttpSession:
        """Create a new HTTP session that should be used as a context manager."""
        return HttpSession(self.logger)
