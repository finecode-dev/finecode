import asyncio

import finecode.workspace_manager.context as context

ws_context = context.WorkspaceContext([])
server_initialized = asyncio.Event()
