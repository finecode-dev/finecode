import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import janus
from loguru import logger
from lsprotocol import types

import finecode.workspace_manager.domain as domain
from finecode.workspace_manager.create_lsp_client import JsonRPCClient


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


async def run_action(
    runner: ExtensionRunnerInfo,
    action: domain.Action,
    apply_on: list[Path] | None,
    apply_on_text: str,
) -> dict[str, Any]:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()
    assert runner.client is not None

    try:
        result = await runner.client.protocol.send_request_async(
            types.WORKSPACE_EXECUTE_COMMAND,
            types.ExecuteCommandParams(
                command="actions/run",
                arguments=[
                    action.name,
                    {
                        "apply_on": (
                            [path.as_posix() for path in apply_on] if apply_on is not None else []
                        ),
                        "apply_on_text": apply_on_text,
                    },
                ],
            ),
        )
        logger.debug(f"Action result: {result}")
        return {"result": json.loads(result.result)}
    except Exception as e:
        logger.exception(e)
        return {}


async def reload_action(runner: ExtensionRunnerInfo, action_name: str) -> None:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()
    assert runner.client is not None

    try:
        result = asyncio.wait_for(
            runner.client.protocol.send_request_async(
                types.WORKSPACE_EXECUTE_COMMAND,
                types.ExecuteCommandParams(
                    command="actions/reload",
                    arguments=[
                        action_name,
                    ],
                ),
            ),
            5,
        )
        logger.debug(f"Action reload result: {result}")
        return {}
    except Exception as e:
        logger.exception(e)
        return {}


async def resolve_package_path(runner: ExtensionRunnerInfo, package_name: str) -> None:
    # resolving package path is used directly after initialization of runner to get full config,
    # which is then registered in runner. In this time runner is not available for any other actions,
    # so `runner.started_event` stays not set and should not be checked here.
    assert runner.client is not None

    try:
        result = await asyncio.wait_for(
            runner.client.protocol.send_request_async(
                types.WORKSPACE_EXECUTE_COMMAND,
                types.ExecuteCommandParams(
                    command="packages/resolvePath",
                    arguments=[
                        package_name,
                    ],
                ),
            ),
            5,
        )
        logger.debug(f"Package path resolve result: {result}")
        return {"packagePath": result.packagePath}
    except Exception as e:
        logger.exception(e)
        return {}
