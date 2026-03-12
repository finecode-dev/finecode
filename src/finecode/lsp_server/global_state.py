import asyncio

from finecode.wm_client import ApiClient

server_initialized = asyncio.Event()
wm_client: ApiClient | None = None
partial_result_tokens: dict[str | int, tuple[str, str]] = {}
wm_log_level: str = "INFO"
