"""
API of ER client for "higher" layers like services, CLI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import pathlib
import typing
from typing import Any

from loguru import logger

import finecode_jsonrpc as jsonrpc_client
from finecode.wm_server import domain
from finecode.wm_server.domain import ErLoggingConfig
from finecode.wm_server.runner import _internal_client_api, _internal_client_types
from finecode.wm_server.utils.iterable_subscribe import IterableSubscribe
from finecode_extension_runner import schema_utils

# reexport
BaseRunnerRequestException = jsonrpc_client.BaseRunnerRequestException
DidChangeTextDocumentParams = _internal_client_types.DidChangeTextDocumentParams
VersionedTextDocumentIdentifier = _internal_client_types.VersionedTextDocumentIdentifier
TextDocumentContentChangeWholeDocument = (
    _internal_client_types.TextDocumentContentChangeWholeDocument
)
TextDocumentContentChangePartial = (
    _internal_client_types.TextDocumentContentChangePartial
)
Range = _internal_client_types.Range
Position = _internal_client_types.Position


# Control-plane RPCs are never legitimately long, so a dead channel must fail
# them within a bound instead of parking forever. ``update_config`` gets the
# larger bound because it rebuilds the ER's RunnerContext (imports handlers)
# right after a cold start; the others introspect already-imported modules.
_ER_UPDATE_CONFIG_TIMEOUT_SEC: typing.Final = 60
_ER_CONTROL_RPC_TIMEOUT_SEC: typing.Final = 30


class ActionRunFailed(jsonrpc_client.BaseRunnerRequestException): ...


class ActionRunStopped(jsonrpc_client.BaseRunnerRequestException): ...


class ActionRunCancelled(jsonrpc_client.BaseRunnerRequestException): ...


@dataclasses.dataclass
class ExtensionRunnerInfo(domain.ExtensionRunner):
    # NOTE: initialized doesn't mean the runner is running, check its status
    initialized_event: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    # Set when status transitions to REPAIRING; fired when repair completes
    # (regardless of outcome).  Waiters re-fetch the runner from context after
    # the event fires because repair replaces the runner object in context.
    repair_complete_event: asyncio.Event | None = None
    # e.g. if there is no venv for env, client can be None
    client: jsonrpc_client.JsonRpcClient | None = None
    partial_results: IterableSubscribe = dataclasses.field(
        default_factory=IterableSubscribe
    )
    progress_notifications: IterableSubscribe = dataclasses.field(
        default_factory=IterableSubscribe
    )
    cmd_override: str | None = None
    # Last (enabled, level) sent via finecodeRunner/updateLogging, to avoid
    # redundant RPCs (ADR-0049).
    log_forwarding: tuple[bool, str] | None = dataclasses.field(default=None)


# Alias for backward compatibility — status enum now lives in domain
RunnerStatus = domain.ExtensionRunnerStatus


# JSON object or text
RunActionRawResult: typing.TypeAlias = dict[str, Any] | str


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
        result = self.result_by_format.get(
            "styled_text_json"
        ) or self.result_by_format.get("string")
        if result is None:
            raise ActionRunFailed("Expected text result format but it was not returned")
        return result


@dataclasses.dataclass
class RunHandlersResponse:
    """Response from actions/runHandlers.

    ``raw_result`` is the serialized RunActionResult dict for context chaining
    (pass as ``previous_result`` to the next segment's run_handlers call).
    ``result_by_format`` is populated only for the final segment of a run.
    ``context`` is the serialized STATE_TYPE dict for context chaining
    (pass as ``previous_context`` to the next segment's run_handlers call).
    """

    raw_result: dict
    result_by_format: dict[str, RunActionRawResult]
    return_code: int
    status: str = "success"
    context: dict | None = None

    def json(self) -> dict[str, Any]:
        result = self.result_by_format.get("json")
        if result is None:
            raise ActionRunFailed("Expected json result format but it was not returned")
        return result

    def text(self) -> str:
        result = self.result_by_format.get(
            "styled_text_json"
        ) or self.result_by_format.get("string")
        if result is None:
            raise ActionRunFailed("Expected text result format but it was not returned")
        return result


class RunResultFormat(enum.Enum):
    JSON = "json"
    STRING = "string"


class RunActionTrigger(enum.StrEnum):
    USER = "user"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class DevEnv(enum.StrEnum):
    IDE = "ide"
    CLI = "cli"
    AI = "ai"
    GIT_HOOK = "git_hook"
    CI = "ci"


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

    if runner.client is None:
        raise ActionRunFailed(
            f"Runner {runner.readable_id} has no active client connection (status: {runner.status})"
        )

    try:
        response = await runner.client.send_request(
            method=_internal_client_types.ER_RUN_ACTION,
            params=_internal_client_types.ErRunActionParams(
                action_name=action_name, params=params, options=options
            ),
            timeout=None,
        )
    except jsonrpc_client.ServerStoppedError as exc:
        raise ActionRunFailed(
            "Runner stopped during execution — it may have been restarted. Try again."
        ) from exc
    except jsonrpc_client.RequestCancelledError as error:
        logger.trace(
            f"Request {error.request_id} to {runner.readable_id} was cancelled"
        )
        await _internal_client_api.cancel_request(
            client=runner.client, request_id=error.request_id
        )
        raise
    except jsonrpc_client.ErrorOnRequest as error:
        if error.error.code == jsonrpc_client.REQUEST_CANCELLED:
            raise ActionRunCancelled(error.error.message) from error
        raise

    run_result = response.result

    if run_result.error is not None:
        raise ActionRunFailed(run_result.error)

    return_code = run_result.return_code
    result_by_format = run_result.result_by_format

    status = run_result.status

    if status == "stopped":
        raise ActionRunStopped(message=result_by_format)

    return RunActionResponse(
        result_by_format=result_by_format, return_code=return_code, status=status
    )


async def run_handlers(
    runner: ExtensionRunnerInfo,
    action_name: str,
    handler_names: list[str],
    params: dict[str, typing.Any] | None = None,
    previous_result: dict | None = None,
    previous_context: dict | None = None,
    caller_kwargs: dict | None = None,
    options: dict[str, typing.Any] | None = None,
) -> RunHandlersResponse:
    """Call actions/runHandlers on the ER for multi-env segment orchestration.

    ``handler_names`` is the ordered list of handler names belonging to this ER's env.
    ``previous_result`` is the serialized RunActionResult from the preceding segment
    (or None for the first segment). The ER seeds context.current_result from it.
    ``previous_context`` is the serialized STATE_TYPE from the preceding segment
    (or None for the first segment or when the context has no STATE_TYPE).
    """
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    if runner.status != RunnerStatus.RUNNING:
        raise ActionRunFailed(
            f"Runner {runner.readable_id} is not running: {runner.status}"
        )

    if runner.client is None:
        raise ActionRunFailed(
            f"Runner {runner.readable_id} has no active client connection (status: {runner.status})"
        )

    try:
        response = await runner.client.send_request(
            method=_internal_client_types.ER_RUN_HANDLERS,
            params=_internal_client_types.ErRunHandlersParams(
                action_name=action_name,
                handler_names=handler_names,
                params=params or {},
                previous_result=previous_result,
                previous_context=previous_context,
                caller_kwargs=caller_kwargs,
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
        raise
    except jsonrpc_client.ErrorOnRequest as error:
        if error.error.code == jsonrpc_client.REQUEST_CANCELLED:
            raise ActionRunCancelled(error.error.message) from error
        raise

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
        context=run_result.context,
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


async def reload_action(runner: ExtensionRunnerInfo, action_name: str) -> bool:
    """Ask a runner to re-import an action and its handlers.

    Returns whether the request was sent: a runner that is not running has
    nothing to reload, which is a normal condition rather than an error, but the
    caller must be able to tell that from a reload that happened.
    """
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    if runner.status != RunnerStatus.RUNNING:
        return False

    await runner.client.send_request(
        method=_internal_client_types.ER_RELOAD_ACTION,
        params=_internal_client_types.ErReloadActionParams(action_name=action_name),
        timeout=_ER_CONTROL_RPC_TIMEOUT_SEC,
    )
    return True


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
        timeout=_ER_CONTROL_RPC_TIMEOUT_SEC,
    )
    return response.result


async def get_payload_schemas(
    runner: ExtensionRunnerInfo,
) -> dict[str, schema_utils.PayloadSchema | None]:
    """Fetch payload schemas for all actions known to the runner."""
    if not runner.initialized_event.is_set():
        await runner.initialized_event.wait()

    if runner.status != RunnerStatus.RUNNING:
        raise ActionRunFailed(
            f"Runner {runner.readable_id} is not running: {runner.status}"
        )

    response = await runner.client.send_request(
        method=_internal_client_types.ER_GET_PAYLOAD_SCHEMAS,
        timeout=_ER_CONTROL_RPC_TIMEOUT_SEC,
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
        params=_internal_client_types.ErResolvePackagePathParams(
            package_name=package_name
        ),
        timeout=_ER_CONTROL_RPC_TIMEOUT_SEC,
    )
    return {"packagePath": response.result.package_path}


@dataclasses.dataclass
class ErTelemetryConfig:
    otlp_endpoint: str | None = None


@dataclasses.dataclass
class RunnerConfig:
    actions: list[domain.Action]
    # config by handler source
    action_handler_configs: dict[str, dict[str, Any]]
    services: list[domain.ServiceDeclaration] = dataclasses.field(default_factory=list)
    # Service config overrides keyed by derived alias. Forwarded verbatim: the ER
    # matches them against its bindings, because activator-registered services
    # are invisible from here (ADR-0070).
    service_config_overrides: dict[str, dict[str, Any]] = dataclasses.field(
        default_factory=dict
    )
    # If provided, eagerly instantiate these handlers after config update.
    # Keys are action names, values are lists of handler names within that action.
    handlers_to_initialize: dict[str, list[str]] | None = None
    logging: ErLoggingConfig = dataclasses.field(default_factory=ErLoggingConfig)
    telemetry: ErTelemetryConfig = dataclasses.field(default_factory=ErTelemetryConfig)

    def to_dict(self) -> dict[str, typing.Any]:
        result: dict[str, typing.Any] = {
            "actions": [action.to_dict() for action in self.actions],
            "action_handler_configs": self.action_handler_configs,
            "services": [svc.to_dict() for svc in self.services],
            "service_config_overrides": self.service_config_overrides,
            "logging": {
                "defaultLevel": self.logging.default_level,
                "logGroups": self.logging.log_groups,
            },
            "telemetry": {
                "otlp_endpoint": self.telemetry.otlp_endpoint,
            },
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
        timeout=_ER_UPDATE_CONFIG_TIMEOUT_SEC,
    )


async def update_logging(
    runner: ExtensionRunnerInfo, forward: bool, forward_level: str
) -> None:
    """Toggle ER->WM log forwarding via the dedicated ``finecodeRunner/updateLogging``
    request. Process-level only on the ER side -- does NOT rebuild RunnerContext
    (contrast ``update_config``)."""
    await runner.client.send_request(
        method=_internal_client_types.ER_UPDATE_LOGGING,
        params=_internal_client_types.ErUpdateLoggingParams(
            forward=forward, forward_level=forward_level
        ),
        timeout=_ER_CONTROL_RPC_TIMEOUT_SEC,
    )


async def update_process_budget(runner: ExtensionRunnerInfo, target: int) -> None:
    """Resize an ER's process-slot gate via ``finecodeRunner/updateProcessBudget``.

    Process-level only on the ER side -- does NOT rebuild RunnerContext
    (contrast ``update_config``). See ADR-0090.
    """
    await runner.client.send_request(
        method=_internal_client_types.ER_UPDATE_PROCESS_BUDGET,
        params=_internal_client_types.ErUpdateProcessBudgetParams(target=target),
        timeout=_ER_CONTROL_RPC_TIMEOUT_SEC,
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


async def notify_document_did_change(
    runner: ExtensionRunnerInfo,
    change_params: _internal_client_types.DidChangeTextDocumentParams,
) -> None:
    runner.client.notify(
        method=_internal_client_types.TEXT_DOCUMENT_DID_CHANGE,
        params=change_params,
    )


__all__ = [
    "ActionRunCancelled",
    "ActionRunFailed",
    "ActionRunStopped",
    "ExtensionRunnerInfo",
    "RunActionRawResult",
    "RunActionResponse",
    "RunResultFormat",
    "RunnerConfig",
    "RunnerStatus",
    "get_payload_schemas",
    "merge_results",
    "notify_document_did_close",
    "notify_document_did_open",
    "reload_action",
    "resolve_action_meta",
    "resolve_package_path",
    "run_action",
    "update_config",
    "update_logging",
    "update_process_budget",
]
