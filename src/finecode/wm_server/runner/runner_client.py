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

import finecode.wm_server.domain as domain
from finecode.wm_server.runner import _internal_client_types, _internal_client_api
from finecode.wm_server.utils.iterable_subscribe import IterableSubscribe
import finecode_jsonrpc as jsonrpc_client


# reexport
BaseRunnerRequestException = jsonrpc_client.BaseRunnerRequestException
DidChangeTextDocumentParams = _internal_client_types.DidChangeTextDocumentParams
VersionedTextDocumentIdentifier = _internal_client_types.VersionedTextDocumentIdentifier
TextDocumentContentChangeWholeDocument = _internal_client_types.TextDocumentContentChangeWholeDocument
TextDocumentContentChangePartial = _internal_client_types.TextDocumentContentChangePartial
Range = _internal_client_types.Range
Position = _internal_client_types.Position


class ActionRunFailed(jsonrpc_client.BaseRunnerRequestException): ...


class ActionRunStopped(jsonrpc_client.BaseRunnerRequestException): ...


@dataclasses.dataclass
class ExtensionRunnerInfo(domain.ExtensionRunner):
    # NOTE: initialized doesn't mean the runner is running, check its status
    initialized_event: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    # e.g. if there is no venv for env, client can be None
    client: jsonrpc_client.JsonRpcClient | None = None
    partial_results: IterableSubscribe = dataclasses.field(
        default_factory=IterableSubscribe
    )


# Alias for backward compatibility — status enum now lives in domain
RunnerStatus = domain.ExtensionRunnerStatus


# JSON object or text
type RunActionRawResult = dict[str, Any] | str


@dataclasses.dataclass
class RunActionResponse:
    result_by_format: dict[str, RunActionRawResult]
    return_code: int

    def json(self) -> dict[str, Any]:
        result = self.result_by_format.get("json")
        if result is None:
            raise ActionRunFailed("Expected json result format but it was not returned")
        return result

    def text(self) -> str:
        result = self.result_by_format.get("styled_text_json") or self.result_by_format.get("string")
        if result is None:
            raise ActionRunFailed("Expected text result format but it was not returned")
        return result


class RunResultFormat(enum.Enum):
    JSON = "json"
    STRING = "string"


class RunActionTrigger(enum.StrEnum):
    USER = 'user'
    SYSTEM = 'system'
    UNKNOWN = 'unknown'


class DevEnv(enum.StrEnum):
    IDE = 'ide'
    CLI = 'cli'
    AI = 'ai'
    PRECOMMIT = 'precommit'
    CI = 'ci'


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

    return_code = command_result["returnCode"]
    stringified_result = command_result["resultByFormat"]
    # currently result is always dumped to json even if response format is expected to
    # be a string. See docs of ER lsp server for more details.
    try:
        result_by_format = json.loads(stringified_result)
    except json.JSONDecodeError as exception:
        raise ActionRunFailed(f"Failed to decode result json: {exception}") from exception

    if command_result["status"] == "stopped":
        raise ActionRunStopped(message=result_by_format)

    return RunActionResponse(result_by_format=result_by_format, return_code=return_code)


async def merge_results(
    runner: ExtensionRunnerInfo,
    action_name: str,
    results: list[dict],
) -> dict:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    if runner.status != RunnerStatus.RUNNING:
        raise ActionRunFailed(
            f"Runner {runner.readable_id} is not running: {runner.status}"
        )

    response = await runner.client.send_request(
        method=_internal_client_types.WORKSPACE_EXECUTE_COMMAND,
        params=_internal_client_types.ExecuteCommandParams(
            command="actions/mergeResults",
            arguments=[action_name, results],
        ),
        timeout=None,
    )
    command_result = response.result
    if "error" in command_result:
        raise ActionRunFailed(command_result["error"])
    return command_result["merged"]


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
    services: list[domain.ServiceDeclaration] = dataclasses.field(default_factory=list)
    # If provided, eagerly instantiate these handlers after config update.
    # Keys are action names, values are lists of handler names within that action.
    handlers_to_initialize: dict[str, list[str]] | None = None

    def to_dict(self) -> dict[str, typing.Any]:
        result: dict[str, typing.Any] = {
            "actions": [action.to_dict() for action in self.actions],
            "action_handler_configs": self.action_handler_configs,
            "services": [svc.to_dict() for svc in self.services],
        }
        if self.handlers_to_initialize is not None:
            result["handlers_to_initialize"] = self.handlers_to_initialize
        return result


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
                text=document_info.text,
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

async def notify_document_did_change(runner: ExtensionRunnerInfo, change_params: _internal_client_types.DidChangeTextDocumentParams) -> None:
    runner.client.notify(
        method=_internal_client_types.TEXT_DOCUMENT_DID_CHANGE,
        params=change_params,
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
    "merge_results",
    "reload_action",
    "resolve_package_path",
    "RunnerConfig",
    "update_config",
    "notify_document_did_open",
    "notify_document_did_close",
]
