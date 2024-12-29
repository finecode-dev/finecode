import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import janus
from loguru import logger
from lsprotocol import types

import finecode.workspace_manager.domain as domain
from finecode.workspace_manager.runner_lsp_client import JsonRPCClient


@dataclass
class ExtensionRunnerInfo:
    working_dir_path: Path
    output_queue: janus.Queue
    started_event: asyncio.Event
    client: JsonRPCClient | None = None
    keep_running_request_task: asyncio.Task | None = None
    
    @property
    def process_id(self) -> int:
        if self.client is not None and self.client._server is not None:
            return self.client._server.pid
        else:
            return 0


async def run_action_in_runner(
    runner: ExtensionRunnerInfo, action: domain.Action, apply_on: list[Path] | None, apply_on_text: str
) -> dict[str, Any]:
    if not runner.started_event.is_set():
        await runner.started_event.wait()
    assert runner.client is not None

    try:
        result = await runner.client.protocol.send_request_async(
            types.WORKSPACE_EXECUTE_COMMAND,
            types.ExecuteCommandParams(
                command='actions/run',
                arguments=[
                    action.name,
                    {
                        "apply_on": [path.as_posix() for path in apply_on] if apply_on is not None else [],
                        "apply_on_text": apply_on_text
                    }
                ]))
        logger.debug(f"Action result: {result}")
        return {"result": json.loads(result.result) }
    except Exception as e:
        logger.exception(e)
        return {}


async def reload_action_in_runner(runner: ExtensionRunnerInfo, action_name: str) -> None:
    if not runner.started_event.is_set():
        await runner.started_event.wait()
    assert runner.client is not None
    
    try:
        result = await runner.client.protocol.send_request_async(
            types.WORKSPACE_EXECUTE_COMMAND,
            types.ExecuteCommandParams(
                command='actions/reload',
                arguments=[
                    action_name,
                ]))
        logger.debug(f"Action reload result: {result}")
        return {}
    except Exception as e:
        logger.exception(e)
        return {}