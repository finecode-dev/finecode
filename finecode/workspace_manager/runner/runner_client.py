# TODO: autocheck, that runner.client.protocol is accessed only here
# TODO: autocheck, that lsprotocol is imported only here
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types

import finecode.workspace_manager.domain as domain

if TYPE_CHECKING:
    from finecode.workspace_manager.runner.runner_info import \
        ExtensionRunnerInfo


async def initialize(
    runner: ExtensionRunnerInfo, client_process_id, client_name: str, client_version: str
):
    try:
        await asyncio.wait_for(
            runner.client.protocol.send_request_async(
                method=types.INITIALIZE,
                params=types.InitializeParams(
                    process_id=client_process_id,
                    capabilities=types.ClientCapabilities(),
                    client_info=types.ClientInfo(name=client_name, version=client_version),
                    trace=types.TraceValue.Verbose,
                ),
            ),
            10,
        )
    except TimeoutError:
        raise Exception()  # TODO


async def notify_initialized(runner: ExtensionRunnerInfo) -> None:
    try:
        runner.client.protocol.notify(method=types.INITIALIZED, params=types.InitializedParams())
    except TimeoutError:
        raise Exception()  # TODO


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
        result = await asyncio.wait_for(
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


async def update_config(
    runner: ExtensionRunnerInfo, actions: dict[str, Any], actions_configs: dict[str, Any]
) -> None:
    # lsp client requests have no timeout, add own one
    try:
        await asyncio.wait_for(
            runner.client.protocol.send_request_async(
                method=types.WORKSPACE_EXECUTE_COMMAND,
                params=types.ExecuteCommandParams(
                    command="finecodeRunner/updateConfig",
                    arguments=[
                        runner.working_dir_path.as_posix(),
                        runner.working_dir_path.stem,
                        actions,
                        actions_configs,
                    ],
                ),
            ),
            10,
        )
    except TimeoutError:
        logger.error(f"Failed to update config of runner {runner.working_dir_path}")
        raise Exception()  # TODO


async def notify_updated_action(runner: ExtensionRunnerInfo, action: dict[str, Any]) -> None:
    # lsp client requests have no timeout, add own one
    try:
        await asyncio.wait_for(
            runner.client.protocol.notify(method="actionsNodes/changed", params={}),
            10,
        )
    except TimeoutError:
        logger.error(f"Failed to update config of runner {runner.working_dir_path}")
        raise Exception()  # TODO
