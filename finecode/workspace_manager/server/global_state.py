import asyncio
import collections.abc
from typing import Any

from finecode.workspace_manager import context
from finecode.workspace_manager.utils import iterable_subscribe

ws_context = context.WorkspaceContext([])
server_initialized = asyncio.Event()
progress_reporter: collections.abc.Callable[[str | int, Any], None] | None = None
partial_results: iterable_subscribe.IterableSubscribe = (
    iterable_subscribe.IterableSubscribe()
)
