from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import janus

if TYPE_CHECKING:
    from finecode.pygls_client_utils import JsonRPCClient


@dataclass
class ExtensionRunnerInfo:
    working_dir_path: Path
    output_queue: janus.Queue
    # NOTE: initialized doesn't mean the runner is running, check its status
    initialized_event: asyncio.Event
    client: JsonRPCClient | None = None
    keep_running_request_task: asyncio.Task | None = None

    @property
    def process_id(self) -> int:
        if self.client is not None and self.client._server is not None:
            return self.client._server.pid
        else:
            return 0
