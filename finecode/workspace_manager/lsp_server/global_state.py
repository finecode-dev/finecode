import asyncio
import collections.abc
from typing import Any

from finecode.workspace_manager import context

ws_context = context.WorkspaceContext([])
server_initialized = asyncio.Event()
progress_reporter: collections.abc.Callable[[str | int, Any], None] | None = None
