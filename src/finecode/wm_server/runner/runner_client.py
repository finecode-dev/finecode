"""
API of ER client for "higher" layers like services, CLI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
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
    progress_notifications: IterableSubscribe = dataclasses.field(
        default_factory=IterableSubscribe
    )
    cmd_override: str | None = None


# Alias for backward compatibility — status enum now lives in domain
RunnerStatus = domain.ExtensionRunnerStatus


# JSON object or text
type RunActionRawResult = dict[str, Any] | str


@dataclasses.dataclass
class RunActionResponse:
    result_by_format: dict[str, RunActionRawResult]
    return_code: int
    status: str = "success"

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


@dataclasses.dataclass
class RunHandlersResponse:
    """Response from actions/runHandlers.

    ``raw_result`` is the serialized RunActionResult dict for context chaining
    (pass as ``previous_result`` to the next segment's run_handlers call).
    ``result_by_format`` is populated only for the final segment of a run.
    """
    raw_result: dict
    result_by_format: dict[str, RunActionRawResult]
    return_code: int
    status: str = "success"

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
    GIT_HOOK = 'git_hook'
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
            method=_internal_client_types.ER_RUN_ACTION,
            params=_internal_client_types.ErRunActionParams(
                action_name=action_name, params=params, options=options
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

    run_result = response.result

    if run_result.error is not None:
        raise ActionRunFailed(run_result.error)

    return_code = run_result.return_code
    result_by_format = run_result.result_by_format

    status = run_result.status

    if status == "stopped":
        raise ActionRunStopped(message=result_by_format)

    return RunActionResponse(result_by_format=result_by_format, return_code=return_code, status=status)


async def run_handlers(
    runner: ExtensionRunnerInfo,
    action_name: str,
    handler_names: list[str],
    params: dict[str, typing.Any] | None = None,
    previous_result: dict | None = None,
    options: dict[str, typing.Any] | None = None,
) -> RunHandlersResponse:
    """Call actions/runHandlers on the ER for multi-env segment orchestration.

    ``handler_names`` is the ordered list of handler names belonging to this ER's env.
    ``previous_result`` is the serialized RunActionResult from the preceding segment
    (or None for the first segment). The ER seeds context.current_result from it.
    """
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

        if runner.status != RunnerStatus.RUNNING:
            raise ActionRunFailed(
                f"Runner {runner.readable_id} is not running: {runner.status}"
            )

    try:
        response = await runner.client.send_request(
            method=_internal_client_types.ER_RUN_HANDLERS,
            params=_internal_client_types.ErRunHandlersParams(
                action_name=action_name,
                handler_names=handler_names,
                params=params or {},
                previous_result=previous_result,
                options=options,
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

    run_result = response.result

    if run_result.error is not None:
        raise ActionRunFailed(run_result.error)

    if run_result.status == "stopped":
        raise ActionRunStopped(message=str(run_result.result_by_format))

    return RunHandlersResponse(
        raw_result=run_result.result or {},
        result_by_format=run_result.result_by_format or {},
        return_code=run_result.return_code or 0,
        status=run_result.status or "success",
    )


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
        method=_internal_client_types.ER_MERGE_RESULTS,
        params=_internal_client_types.ErMergeResultsParams(
            action_name=action_name, results=results
        ),
        timeout=None,
    )
    merge_result = response.result
    if merge_result.error is not None:
        raise ActionRunFailed(merge_result.error)
    return merge_result.merged


async def reload_action(runner: ExtensionRunnerInfo, action_name: str) -> None:
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    await runner.client.send_request(
        method=_internal_client_types.ER_RELOAD_ACTION,
        params=_internal_client_types.ErReloadActionParams(action_name=action_name),
    )


async def resolve_source(runner: ExtensionRunnerInfo, source: str) -> str | None:
    """Ask the ER to resolve an import-path alias to its canonical source.

    Returns the canonical source string
    on success, or ``None`` when the alias cannot be imported in the runner's
    environment.
    """
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    if runner.status != RunnerStatus.RUNNING:
        return None

    try:
        response = await runner.client.send_request(
            method=_internal_client_types.ER_RESOLVE_SOURCE,
            params=_internal_client_types.ErResolveSourceParams(source=source),
            timeout=10,
        )
    except jsonrpc_client.BaseRunnerRequestException as exc:
        logger.debug(f"ER could not resolve source '{source}': {exc}")
        return None
    return response.result.canonical_source


async def resolve_action_meta(runner: ExtensionRunnerInfo) -> dict[str, dict]:
    """Ask the ER to resolve action meta info (canonical source + execution mode)."""
    response = await runner.client.send_request(
        method=_internal_client_types.ER_RESOLVE_ACTION_META,
        timeout=None,
    )
    return response.result


async def get_payload_schemas(runner: ExtensionRunnerInfo) -> dict[str, dict | None]:
    """Fetch payload schemas for all actions known to the runner."""
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    if runner.status != RunnerStatus.RUNNING:
        raise ActionRunFailed(
            f"Runner {runner.readable_id} is not running: {runner.status}"
        )

    response = await runner.client.send_request(
        method=_internal_client_types.ER_GET_PAYLOAD_SCHEMAS,
        timeout=None,
    )
    return response.result


async def resolve_package_path(
    runner: ExtensionRunnerInfo, package_name: str
) -> dict[str, str]:
    # resolving package path is used directly after initialization of runner to get full
    # config, which is then registered in runner. In this time runner is not available
    # for any other actions, so `runner.started_event` stays not set and should not be
    # checked here.
    response = await runner.client.send_request(
        method=_internal_client_types.ER_RESOLVE_PACKAGE_PATH,
        params=_internal_client_types.ErResolvePackagePathParams(package_name=package_name),
    )
    return {"packagePath": response.result.package_path}


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
        method=_internal_client_types.ER_UPDATE_CONFIG,
        params=_internal_client_types.ErUpdateConfigParams(
            working_dir=runner.working_dir_path.as_posix(),
            project_name=runner.working_dir_path.stem,
            project_def_path=project_def_path.as_posix(),
            config=config.to_dict(),
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
    "resolve_action_meta",
    "get_payload_schemas",
    "resolve_package_path",
    "RunnerConfig",
    "update_config",
    "notify_document_did_open",
    "notify_document_did_close",
]
