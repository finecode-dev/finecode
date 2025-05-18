from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Coroutine

from pygls.io_ import run_async

from finecode.pygls_client_utils import JsonRPCClient


class CustomJsonRpcClient(JsonRPCClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.server_exit_callback: Callable[[], Coroutine] | None = None

    async def start_io(self, cmd: str, *args, **kwargs):
        """Start the given server and communicate with it over stdio."""
        logger = logging.getLogger(__name__)
        logger.debug("Starting server process: %s", " ".join([cmd, *args, str(kwargs)]))
        # difference with original version: use `create_subprocess_shell` instead of
        # `create_subprocess_exec` to be able to start detached process
        full_cmd = shlex.join([cmd, *args])
        server = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )

        # Keep mypy happy
        if server.stdout is None:
            raise RuntimeError("Server process is missing a stdout stream")

        # Keep mypy happy
        if server.stdin is None:
            raise RuntimeError("Server process is missing a stdin stream")

        self.protocol.set_writer(server.stdin)
        connection = asyncio.create_task(
            run_async(
                stop_event=self._stop_event,
                reader=server.stdout,
                protocol=self.protocol,
                logger=logger,
                error_handler=self.report_server_error,
            )
        )
        notify_exit = asyncio.create_task(self._server_exit())

        self._server = server
        self._async_tasks.extend([connection, notify_exit])

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
    client: CustomJsonRpcClient
    keep_running_request_task: asyncio.Task | None = None

    @property
    def process_id(self) -> int:
        if self.client._server is not None:
            return self.client._server.pid
        else:
            return 0
