import asyncio

from finecode.api_client import ApiClient

server_initialized = asyncio.Event()
api_client: ApiClient | None = None
partial_result_tokens: dict[str | int, tuple[str, str]] = {}
