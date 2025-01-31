# TODO: pass not the whole runner, but only runner.client
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


class BaseRunnerRequestException(Exception): ...


class NoResponse(BaseRunnerRequestException): ...


class ResponseTimeout(BaseRunnerRequestException): ...


async def send_request(
    runner: ExtensionRunnerInfo, method: str, params: Any | None, timeout: int | None = 10
) -> Any | None:
    try:
        response = await asyncio.wait_for(
            runner.client.protocol.send_request_async(
                method=method,
                params=params,
            ),
            timeout,
        )
        logger.debug(f"Got response on {method} from {runner.working_dir_path}")
        return response
    except RuntimeError as error:
        logger.error(f"Extension runner crashed: {error}")
        stdout, stderr = await runner.client._server.communicate()

        logger.debug(f"[Runner exited with {runner.client._server.returncode}]")
        if stdout:
            logger.debug(f"[stdout]\n{stdout.decode()}")
        if stderr:
            logger.debug(f"[stderr]\n{stderr.decode()}")

        raise NoResponse(
            f"Extension runner {runner.working_dir_path} crashed, no response on {method}"
        )
    except TimeoutError:
        raise ResponseTimeout(
            f"Timeout {timeout}s for response on {method} to runner {runner.working_dir_path}"
        )


async def initialize(
    runner: ExtensionRunnerInfo, client_process_id, client_name: str, client_version: str
) -> None:
    await send_request(
        runner=runner,
        method=types.INITIALIZE,
        params=types.InitializeParams(
            process_id=client_process_id,
            capabilities=types.ClientCapabilities(),
            client_info=types.ClientInfo(name=client_name, version=client_version),
            trace=types.TraceValue.Verbose,
        ),
    )


async def notify_initialized(runner: ExtensionRunnerInfo) -> None:
    runner.client.protocol.notify(method=types.INITIALIZED, params=types.InitializedParams())


async def run_action(
    runner: ExtensionRunnerInfo,
    action: domain.Action,
    apply_on: list[Path] | None,
    apply_on_text: str,
) -> dict[str, Any]:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()
    assert runner.client is not None

    response = await send_request(
        runner=runner,
        method=types.WORKSPACE_EXECUTE_COMMAND,
        params=types.ExecuteCommandParams(
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
    return {"result": json.loads(response.result)}


async def reload_action(runner: ExtensionRunnerInfo, action_name: str) -> None:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()
    assert runner.client is not None

    await send_request(
        runner=runner,
        method=types.WORKSPACE_EXECUTE_COMMAND,
        params=types.ExecuteCommandParams(
            command="actions/reload",
            arguments=[
                action_name,
            ],
        ),
    )


async def resolve_package_path(runner: ExtensionRunnerInfo, package_name: str) -> None:
    # resolving package path is used directly after initialization of runner to get full config,
    # which is then registered in runner. In this time runner is not available for any other actions,
    # so `runner.started_event` stays not set and should not be checked here.
    assert runner.client is not None

    response = await send_request(
        runner=runner,
        method=types.WORKSPACE_EXECUTE_COMMAND,
        params=types.ExecuteCommandParams(
            command="packages/resolvePath",
            arguments=[
                package_name,
            ],
        ),
    )
    return {"packagePath": response.packagePath}


async def update_config(
    runner: ExtensionRunnerInfo, actions: dict[str, Any], actions_configs: dict[str, Any]
) -> None:
    await send_request(
        runner=runner,
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
    )
