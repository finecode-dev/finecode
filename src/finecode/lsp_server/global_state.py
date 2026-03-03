import asyncio
import collections.abc
from typing import Any

from finecode.api_server import context
from finecode.api_client import ApiClient

ws_context = context.WorkspaceContext([])
server_initialized = asyncio.Event()
progress_reporter: collections.abc.Callable[[str | int, Any], None] | None = None
api_client: ApiClient | None = None
