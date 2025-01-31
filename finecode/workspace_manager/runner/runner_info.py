from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Coroutine

from finecode.pygls_client_utils import JsonRPCClient


class CustomJsonRpcClient(JsonRPCClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_exit_callback: Coroutine | None = None

    async def server_exit(self, server):
        result = await super().server_exit(server)
        if self.server_exit_callback is not None:
            await self.server_exit_callback()
        return result


@dataclass
class ExtensionRunnerInfo:
    working_dir_path: Path
    # NOTE: initialized doesn't mean the runner is running, check its status
    initialized_event: asyncio.Event
    client: CustomJsonRpcClient | None = None
    keep_running_request_task: asyncio.Task | None = None

    @property
    def process_id(self) -> int:
        if self.client is not None and self.client._server is not None:
            return self.client._server.pid
        else:
            return 0
