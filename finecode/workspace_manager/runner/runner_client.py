# TODO: pass not the whole runner, but only runner.client
# TODO: autocheck, that runner.client.protocol is accessed only here
# TODO: autocheck, that lsprotocol is imported only here
from __future__ import annotations

import asyncio
import asyncio.subprocess
import enum
import json
import typing
from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types
from pygls import exceptions as pygls_exceptions

import finecode.workspace_manager.domain as domain

if TYPE_CHECKING:
    from finecode.workspace_manager.runner.runner_info import ExtensionRunnerInfo


class BaseRunnerRequestException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class NoResponse(BaseRunnerRequestException): ...


class ResponseTimeout(BaseRunnerRequestException): ...


async def log_process_log_streams(process: asyncio.subprocess.Process) -> None:
    stdout, stderr = await process.communicate()

    logger.debug(f"[Runner exited with {process.returncode}]")
    if stdout:
        logger.debug(f"[stdout]\n{stdout.decode()}")
    if stderr:
        logger.debug(f"[stderr]\n{stderr.decode()}")


async def send_request(
    runner: ExtensionRunnerInfo,
    method: str,
    params: Any | None,
    timeout: int | None = 10,
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
        await log_process_log_streams(process=runner.client._server)
        raise NoResponse(
            f"Extension runner {runner.working_dir_path} crashed,"
            f" no response on {method}"
        )
    except TimeoutError:
        # can this happen?
        # if runner.client._server.returncode != None:
        #     await log_process_log_streams(process=runner.client._server)
        raise ResponseTimeout(
            f"Timeout {timeout}s for response on {method} to"
            f" runner {runner.working_dir_path}"
        )
    except pygls_exceptions.JsonRpcInternalError as error:
        logger.error(f"JsonRpcInternalError: {error.message}")
        raise NoResponse(
            f"Extension runner {runner.working_dir_path} returned no response,"
            " check it logs"
        )


def send_request_sync(
    runner: ExtensionRunnerInfo,
    method: str,
    params: Any | None,
    timeout: int | None = 10,
) -> Any | None:
    try:
        response_future = runner.client.protocol.send_request(
            method=method,
            params=params,
        )
        response = response_future.result(timeout)
        logger.debug(f"Got response on {method} from {runner.working_dir_path}")
        return response
    except RuntimeError as error:
        logger.error(f"Extension runner crashed? {error}")
        raise NoResponse(
            f"Extension runner {runner.working_dir_path} crashed,"
            f" no response on {method}"
        )
    except TimeoutError:
        if runner.client._server.returncode is not None:
            logger.error(
                "Extension runner stopped with"
                f" exit code {runner.client._server.returncode}"
            )
        raise ResponseTimeout(
            f"Timeout {timeout}s for response on {method}"
            f" to runner {runner.working_dir_path}"
        )
    except pygls_exceptions.JsonRpcInternalError as error:
        logger.error(f"JsonRpcInternalError: {error.message}")
        raise NoResponse(
            f"Extension runner {runner.working_dir_path} returned no response,"
            " check it logs"
        )


async def initialize(
    runner: ExtensionRunnerInfo,
    client_process_id,
    client_name: str,
    client_version: str,
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
    runner.client.protocol.notify(
        method=types.INITIALIZED, params=types.InitializedParams()
    )


# JSON object or text
type RunActionRawResult = dict[str, Any] | str


class RunActionResponse(typing.NamedTuple):
    result: RunActionRawResult
    return_code: int


class RunResultFormat(enum.Enum):
    JSON = "json"
    STRING = "string"


async def run_action(
    runner: ExtensionRunnerInfo,
    action_name: str,
    params: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> RunActionResponse:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    response = await send_request(
        runner=runner,
        method=types.WORKSPACE_EXECUTE_COMMAND,
        params=types.ExecuteCommandParams(
            command="actions/run",
            arguments=[action_name, params, options],
        ),
        timeout=None,
    )

    return_code = response.return_code
    raw_result = ""
    if response.format == "string":
        raw_result = response.result
    elif response.format == "json" or response.format == "styled_text_json":
        raw_result = json.loads(response.result)
    else:
        raise Exception(f"Not support result format: {response.format}")

    return RunActionResponse(result=raw_result, return_code=return_code)


async def reload_action(runner: ExtensionRunnerInfo, action_name: str) -> None:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

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
    # resolving package path is used directly after initialization of runner to get full
    # config, which is then registered in runner. In this time runner is not available
    # for any other actions, so `runner.started_event` stays not set and should not be
    # checked here.
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
    runner: ExtensionRunnerInfo,
    actions: list[domain.Action],
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
            ],
        ),
    )


async def shutdown(
    runner: ExtensionRunnerInfo,
) -> None:
    await send_request(runner=runner, method=types.SHUTDOWN, params=None)


def shutdown_sync(
    runner: ExtensionRunnerInfo,
) -> None:
    send_request_sync(runner=runner, method=types.SHUTDOWN, params=None)


async def exit(runner: ExtensionRunnerInfo) -> None:
    runner.client.protocol.notify(method=types.EXIT)


def exit_sync(runner: ExtensionRunnerInfo) -> None:
    runner.client.protocol.notify(method=types.EXIT)


async def notify_document_did_open(
    runner: ExtensionRunnerInfo, document_info: domain.TextDocumentInfo
) -> None:
    runner.client.protocol.notify(
        method=types.TEXT_DOCUMENT_DID_OPEN,
        params=types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=document_info.uri,
                language_id="",
                version=int(document_info.version),
                text="",
            )
        ),
    )


async def notify_document_did_close(
    runner: ExtensionRunnerInfo, document_uri: str
) -> None:
    runner.client.protocol.notify(
        method=types.TEXT_DOCUMENT_DID_CLOSE,
        params=types.DidCloseTextDocumentParams(
            text_document=types.TextDocumentIdentifier(document_uri)
        ),
    )
