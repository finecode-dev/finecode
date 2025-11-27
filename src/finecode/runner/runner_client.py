"""
API of ER client for "higher" layers like services, CLI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import typing
import pathlib
from typing import Any

from loguru import logger

import finecode.domain as domain
from finecode.runner import jsonrpc_client, _internal_client_types, _internal_client_api


# reexport
BaseRunnerRequestException = jsonrpc_client.BaseRunnerRequestException
GetDocumentParams = _internal_client_types.GetDocumentParams
GetDocumentResult = _internal_client_types.GetDocumentResult


class ActionRunFailed(jsonrpc_client.BaseRunnerRequestException): ...


class ActionRunStopped(jsonrpc_client.BaseRunnerRequestException): ...


@dataclasses.dataclass
class ExtensionRunnerInfo:
    working_dir_path: pathlib.Path
    env_name: str
    status: RunnerStatus
    # NOTE: initialized doesn't mean the runner is running, check its status
    initialized_event: asyncio.Event
    # e.g. if there is no venv for env, client can be None
    client: jsonrpc_client.JsonRpcClient | None = None

    @property
    def readable_id(self) -> str:
        return f"{self.working_dir_path} ({self.env_name})"

    @property
    def logs_path(self) -> pathlib.Path:
        return self.working_dir_path / ".venvs" / self.env_name / "logs" / "runner.log"


class RunnerStatus(enum.Enum):
    NO_VENV = enum.auto()
    INITIALIZING = enum.auto()
    FAILED = enum.auto()
    RUNNING = enum.auto()
    EXITED = enum.auto()


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

        if runner.status != RunnerStatus.RUNNING:
            raise ActionRunFailed(
                f"Runner {runner.readable_id} is not running: {runner.status}"
            )

    try:
        response = await runner.client.send_request(
            method=_internal_client_types.WORKSPACE_EXECUTE_COMMAND,
            params=_internal_client_types.ExecuteCommandParams(
                command="actions/run",
                arguments=[action_name, params, options],
            ),
            timeout=None,
        )
    except jsonrpc_client.RequestCancelledError as error:
        logger.trace(
            f"Request {error.request_id} to {runner.readable_id} was cancelled"
        )
        await _internal_client_api.cancel_request(
            client=runner.client, request_id=error.request_id
        )
        raise error

    command_result = response.result

    if "error" in command_result:
        raise ActionRunFailed(command_result["error"])

    return_code = command_result["return_code"]
    raw_result = ""
    stringified_result = command_result["result"]
    # currently result is always dumped to json even if response format is expected to
    # be a string. See docs of ER lsp server for more details.
    raw_result = json.loads(stringified_result)
    if command_result["format"] == "string":
        result = raw_result
    elif (
        command_result["format"] == "json"
        or command_result["format"] == "styled_text_json"
    ):
        # string was already converted to dict above
        result = raw_result
    else:
        raise Exception(f"Not support result format: {command_result['format']}")

    if command_result["status"] == "stopped":
        raise ActionRunStopped(message=result)

    return RunActionResponse(result=result, return_code=return_code)


async def reload_action(runner: ExtensionRunnerInfo, action_name: str) -> None:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    await runner.client.send_request(
        method=_internal_client_types.WORKSPACE_EXECUTE_COMMAND,
        params=_internal_client_types.ExecuteCommandParams(
            command="actions/reload",
            arguments=[
                action_name,
            ],
        ),
    )


async def resolve_package_path(
    runner: ExtensionRunnerInfo, package_name: str
) -> dict[str, str]:
    # resolving package path is used directly after initialization of runner to get full
    # config, which is then registered in runner. In this time runner is not available
    # for any other actions, so `runner.started_event` stays not set and should not be
    # checked here.
    response = await runner.client.send_request(
        method=_internal_client_types.WORKSPACE_EXECUTE_COMMAND,
        params=_internal_client_types.ExecuteCommandParams(
            command="packages/resolvePath",
            arguments=[
                package_name,
            ],
        ),
    )
    return {"packagePath": response.result["packagePath"]}


@dataclasses.dataclass
class RunnerConfig:
    actions: list[domain.Action]
    # config by handler source
    action_handler_configs: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "action_handler_configs": self.action_handler_configs,
        }


async def update_config(
    runner: ExtensionRunnerInfo, project_def_path: pathlib.Path, config: RunnerConfig
) -> None:
    await runner.client.send_request(
        method=_internal_client_types.WORKSPACE_EXECUTE_COMMAND,
        params=_internal_client_types.ExecuteCommandParams(
            command="finecodeRunner/updateConfig",
            arguments=[
                runner.working_dir_path.as_posix(),
                runner.working_dir_path.stem,
                project_def_path.as_posix(),
                config.to_dict(),
            ],
        ),
    )


async def notify_document_did_open(
    runner: ExtensionRunnerInfo, document_info: domain.TextDocumentInfo
) -> None:
    runner.client.notify(
        method=_internal_client_types.TEXT_DOCUMENT_DID_OPEN,
        params=_internal_client_types.DidOpenTextDocumentParams(
            text_document=_internal_client_types.TextDocumentItem(
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
    runner.client.notify(
        method=_internal_client_types.TEXT_DOCUMENT_DID_CLOSE,
        params=_internal_client_types.DidCloseTextDocumentParams(
            text_document=_internal_client_types.TextDocumentIdentifier(document_uri)
        ),
    )


__all__ = [
    "ActionRunFailed",
    "ActionRunStopped",
    "ExtensionRunnerInfo",
    "RunnerStatus",
    "RunActionRawResult",
    "RunActionResponse",
    "RunResultFormat",
    "run_action",
    "reload_action",
    "resolve_package_path",
    "RunnerConfig",
    "update_config",
    "notify_document_did_open",
    "notify_document_did_close",
]
