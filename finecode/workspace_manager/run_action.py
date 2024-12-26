import asyncio
import concurrent.futures as futures
import threading
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
    process_id: int
    output_queue: janus.Queue
    stop_event: threading.Event
    started_event: asyncio.Event
    process_future: futures.Future | None
    port: int | None = None
    client: JsonRPCClient | None = None
    keep_running_request_task: asyncio.Task | None = None


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
        return result
    except Exception as e:
        logger.exception(e)
        return {}
