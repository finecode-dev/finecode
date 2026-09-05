# docs: docs/concepts.md, docs/cli.md
"""FineCode WM Server — TCP JSON-RPC server for external tool integration.

The WM server is the shared backbone that holds the WorkspaceContext. Any client
(LSP server, MCP server, CLI) can start it if not already running and connect to it.
When the last client disconnects, the server shuts down automatically.

Discovery: writes the listening port to .venvs/dev_workspace/cache/finecode/wm_port
so clients can find it (same cache directory used for action results).

Protocol:  see _jsonrpc.py (framing) and _api_handlers.py (method implementations).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import os
import pathlib
import socket
import typing

import finecode_jsonrpc.client as jsonrpc_client
from loguru import logger

import finecode_jsonrpc
from finecode.wm_server import context, domain, wal
from finecode.wm_server._api_handlers import (
    _handle_actions_reload,
    _handle_add_dir,
    _handle_find_project_for_file,
    _handle_get_payload_schemas,
    _handle_get_project_raw_config,
    _handle_get_tree,
    _handle_get_workspace_editable_packages,
    _handle_list_actions,
    _handle_list_projects,
    _handle_prepare_envs,
    _handle_reload_config,
    _handle_remove_dir,
    _handle_run_action,
    _handle_run_action_with_partial_results_task,
    _handle_run_action_with_progress_task,
    _handle_run_batch,
    _handle_run_batch_with_partial_results_task,
    _handle_run_batch_with_progress_task,
    _handle_runners_check_env,
    _handle_runners_list,
    _handle_runners_remove_env,
    _handle_runners_restart,
    _handle_set_config_overrides,
    _handle_start_runners,
    handle_documents_changed,
    handle_documents_closed,
    handle_documents_opened,
)
from finecode.wm_server._jsonrpc import (
    NOT_IMPLEMENTED_CODE,
    MethodHandler,
    NotificationHandler,
    _jsonrpc_error,
    _jsonrpc_response,
    _NotImplementedError,
    _read_message,
    _write_message,
)
from finecode.wm_server.errors import ConfigurationError, RunnerNotFoundError
from finecode.wm_server.runner import elicitation_bridge, wm_bridge
from finecode.wm_server.services import (  # noqa: F401
    knowledge_service as _knowledge_service,
)
from finecode.wm_server.services import (
    log_delivery,
)
from finecode.wm_server.services.run_service.exceptions import (
    ActionCancelledError,
    ActionRunFailed,
    StartingEnvironmentsFailed,
)
from finecode.wm_server.wm_lifecycle import discovery_file_path

if typing.TYPE_CHECKING:
    from finecode.wm_server.runner.runner_client import ExtensionRunnerInfo

DISCONNECT_TIMEOUT_SECONDS = 30
NO_CLIENT_TIMEOUT_SECONDS = 30

# save so that server/getInfo can return it
_log_file_path: pathlib.Path | None = None


# ---------------------------------------------------------------------------
# Server → all-clients notification helper
# ---------------------------------------------------------------------------


def _notify_all_clients(method: str, params: dict) -> None:
    """Broadcast a JSON-RPC notification to all connected clients."""
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    for writer in list(_connected_clients):
        try:
            _write_message(writer, msg)
        except Exception:
            logger.trace("FineCode API: failed to notify client, skipping")


# ---------------------------------------------------------------------------
# Server-info handlers (kept here because they reference _log_file_path / stop)
# ---------------------------------------------------------------------------


async def _handle_server_get_info(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Handle ``server/getInfo``.

    Returns information about the running WM Server instance: the path to its
    log file, its process id, its package version, and the labels of every
    currently connected client — which is how a caller about to replace this
    server learns whose session it is disturbing (PRD-0008 R8).

    ``version`` is what ``finecode version`` reports: unlike printing
    ``finecode.__version__`` from the invoking process, a client can only get
    it *from here* by actually completing the server's full startup path
    (spawn, import, bind, respond) — which is the point of asking.

    Result: ``{"logFilePath", "pid", "version", "clients": ["lsp", "mcp-...", ...]}``
    """
    try:
        version = importlib.metadata.version("finecode")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {
        "logFilePath": str(_log_file_path) if _log_file_path is not None else None,
        "pid": os.getpid(),
        "version": version,
        "clients": sorted(_client_labels.values()),
    }


async def _handle_server_shutdown(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Shut down the WM server.

    Responds with ``{}`` and then stops the server on the next event-loop
    iteration, giving the transport layer time to flush the response.

    Result: ``{}``
    """
    logger.info("FineCode API: shutdown requested by client")
    asyncio.get_event_loop().call_soon(stop)
    return {}


# ---------------------------------------------------------------------------
# Method dispatch tables
# See docs/wm-protocol.md for full protocol documentation.
# ---------------------------------------------------------------------------

_METHODS: dict[str, MethodHandler] = {
    # workspace/
    "workspace/listProjects": _handle_list_projects,
    "workspace/findProjectForFile": _handle_find_project_for_file,
    "workspace/addDir": _handle_add_dir,
    "workspace/removeDir": _handle_remove_dir,
    "workspace/setConfigOverrides": _handle_set_config_overrides,
    "workspace/getProjectRawConfig": _handle_get_project_raw_config,
    "workspace/getWorkspaceEditablePackages": _handle_get_workspace_editable_packages,
    "workspace/startRunners": _handle_start_runners,
    "workspace/reloadConfig": _handle_reload_config,
    "workspace/prepareEnvs": _handle_prepare_envs,
    # actions/
    "actions/list": _handle_list_actions,
    "actions/getTree": _handle_get_tree,
    "actions/getPayloadSchemas": _handle_get_payload_schemas,
    "actions/run": _handle_run_action,
    "actions/runBatch": _handle_run_batch,
    "actions/reload": _handle_actions_reload,
    # runners/
    "runners/list": _handle_runners_list,
    "runners/restart": _handle_runners_restart,
    "runners/checkEnv": _handle_runners_check_env,
    "runners/removeEnv": _handle_runners_remove_env,
    # server/
    "server/getInfo": _handle_server_get_info,
    "server/shutdown": _handle_server_shutdown,
}

_NOTIFICATIONS: dict[str, NotificationHandler] = {
    # documents/
    "documents/opened": handle_documents_opened,
    "documents/closed": handle_documents_closed,
    "documents/changed": handle_documents_changed,
}


# ---------------------------------------------------------------------------
# Connection tracking and client handler
# ---------------------------------------------------------------------------

_connected_clients: set[asyncio.StreamWriter] = set()
_auto_stop_task: asyncio.Task | None = None
_no_client_timeout_task: asyncio.Task | None = None
_server: asyncio.Server | None = None
_discovery_file: pathlib.Path | None = None
_had_client: bool = False
_running_partial_result_tasks: dict[asyncio.StreamWriter, set[asyncio.Task]] = {}
_client_labels: dict[asyncio.StreamWriter, str] = {}
_disconnect_timeout: int = DISCONNECT_TIMEOUT_SECONDS
_keep_alive: bool = False

# What each connection declared it can be asked at ``client/initialize``
# (ADR-0082 rule 2). A connection absent from here declared nothing and is never
# sent a question: the point of declaring is that a surface which cannot answer
# is known before the question is sent rather than discovered by waiting.
_client_capabilities: dict[asyncio.StreamWriter, dict] = {}


# ---------------------------------------------------------------------------
# Server → one-client requests (ADR-0082)
# ---------------------------------------------------------------------------

# Outbound requests this server is waiting on an answer for, and the connection
# each was addressed to. Two dicts rather than one of tuples because the id →
# future lookup is on the hot path (every inbound response) and the owner lookup
# only on disconnect.
_pending_client_requests: dict[int, asyncio.Future] = {}
_pending_client_request_owners: dict[int, asyncio.StreamWriter] = {}
# Ids for outbound requests come from a counter of their own. Nothing else on
# the wire allocates from it, so an answer can never be confused with a client's
# own request id.
_last_client_request_id: int = 0


class ClientRequestFailed(Exception):
    """No answer will come from the addressed client.

    Raised for every way of not being answered — the client went away, it
    replied with a JSON-RPC error, the deadline passed — because the caller's
    reaction to all of them is the same: stop waiting. Which one it was is in
    the message, for the log.
    """


async def _request_client(
    writer: asyncio.StreamWriter,
    method: str,
    params: dict,
    timeout_sec: float,
) -> dict:
    """Ask one specific client something and wait for its answer.

    Addressed rather than broadcast: an answer is not idempotent across
    recipients, so the caller names the connection (ADR-0082 rule 1).

    The deadline is the server's, not the caller's (rule 4). An answer that
    arrives after it is dropped rather than applied — the future is off the
    registry by then, and the response route below has nowhere to put it, which
    is the intended outcome and not a leak.

    Raises:
        ClientRequestFailed: the client disconnected, answered with an error, or
            did not answer within *timeout_sec*.
    """
    global _last_client_request_id
    _last_client_request_id += 1
    request_id = _last_client_request_id

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_client_requests[request_id] = future
    _pending_client_request_owners[request_id] = writer
    try:
        _write_message(
            writer,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        )
        await writer.drain()
    except Exception as exception:
        _discard_pending_client_request(request_id)
        raise ClientRequestFailed(
            f"could not send {method} to the client: {exception}"
        ) from exception

    try:
        response = await asyncio.wait_for(future, timeout=timeout_sec)
    except TimeoutError as exception:
        raise ClientRequestFailed(
            f"the client did not answer {method} within {timeout_sec}s"
        ) from exception
    finally:
        _discard_pending_client_request(request_id)

    if "error" in response:
        error = response["error"] or {}
        raise ClientRequestFailed(
            f"the client answered {method} with an error: "
            f"{error.get('code')} {error.get('message')}"
        )
    return response.get("result") or {}


def _discard_pending_client_request(request_id: int) -> None:
    _pending_client_requests.pop(request_id, None)
    _pending_client_request_owners.pop(request_id, None)


def _resolve_client_response(
    request_id: int, msg: dict, writer: asyncio.StreamWriter | None = None
) -> None:
    """Route a client's answer back to whoever asked.

    An id nobody is waiting on is logged and dropped, never answered: replying
    to a response would make this server the one violating the protocol, and
    the common cause is an answer that arrived after its deadline.

    An answer is only taken from the connection the question was *put to*.
    Request ids come from one counter shared by every connection, so without
    this check any client could answer another client's question just by
    guessing an id — and the run would act on it (ADR-0082 rule 1: one
    addressee). *writer* is optional so the registry can still be resolved
    directly in tests that never stood up a second connection.
    """
    if writer is not None:
        owner = _pending_client_request_owners.get(request_id)
        if owner is not None and owner is not writer:
            logger.warning(
                f"FineCode API: a client answered request {request_id}, which was "
                f"put to a different client; discarding"
            )
            return
    future = _pending_client_requests.get(request_id)
    if future is None:
        logger.debug(
            f"FineCode API: response for request {request_id} arrived with nobody "
            f"waiting on it (late answer, or never sent by this server); discarding"
        )
        return
    if not future.done():
        future.set_result(msg)


def _fail_pending_requests_for(writer: asyncio.StreamWriter) -> None:
    """Resolve every question outstanding on a connection that just went away.

    ADR-0082 rule 4: the WM stops ~30s after its last client disconnects
    (ADR-0004), so waiting out a five-minute deadline for an answer from a
    client that no longer exists would outlive the server holding the question.
    The asking run learns at once that nobody can be asked.
    """
    for request_id, owner in list(_pending_client_request_owners.items()):
        if owner is not writer:
            continue
        future = _pending_client_requests.get(request_id)
        _discard_pending_client_request(request_id)
        if future is not None and not future.done():
            future.set_exception(
                ClientRequestFailed(
                    "the client this question was addressed to disconnected"
                )
            )


# ---------------------------------------------------------------------------
# Client log streaming (ADR-0049) — subscription registry, batching, sink
# ---------------------------------------------------------------------------

_log_registry: log_delivery.SubscriptionRegistry
_log_batcher: log_delivery.LogBatcher
_log_flush_task: asyncio.Task | None = None
_log_loop: asyncio.AbstractEventLoop | None = None
_log_sink_id: int | None = None
_log_interval_ms: int = (
    200  # timer cadence; the LogBatcher hides its interval, so track it here
)


def _emit_log_records(conn, records: list[dict], dropped: int) -> None:
    """LogBatcher flush_callback. Runs on the loop thread. Writes one
    server/logRecords notification to `conn` (a StreamWriter)."""
    msg = {
        "jsonrpc": "2.0",
        "method": log_delivery.LOG_RECORDS_METHOD,
        "params": log_delivery.build_log_notification(records, dropped),
    }
    try:
        _write_message(conn, msg)
    except Exception:
        pass  # slow/broken client; do not crash the WM (mirrors _notify_client)


def reset_log_delivery(
    *, interval_ms: int = 200, max_batch: int = 100, buffer_limit: int = 1000
) -> None:
    """(Re)initialise the delivery pipeline. Called at import, at start(), and by tests."""
    global _log_registry, _log_batcher, _log_interval_ms
    _log_interval_ms = interval_ms
    _log_registry = log_delivery.SubscriptionRegistry()
    _log_batcher = log_delivery.LogBatcher(
        _emit_log_records,
        interval_ms=interval_ms,
        max_batch=max_batch,
        buffer_limit=buffer_limit,
    )


reset_log_delivery()  # module-import default so the sink never sees an unset batcher


def _deliver_record(record: log_delivery.ClientLogRecord) -> None:
    """Loop-thread: fan out one record to subscribers and enqueue it."""
    for conn in _log_registry.subscribers_for(record.level):
        _log_batcher.enqueue(conn, record)


def _client_log_sink(message) -> None:
    """loguru sink. May run off the loop thread — marshal accordingly (see
    ADR-0049 §1 threading model)."""
    if not _log_registry.has_subscribers():
        return
    rec = message.record
    level = rec["level"].name
    mlv = _log_registry.min_level_value()
    if mlv is None or log_delivery.level_value(level) < mlv:
        return
    client_record = log_delivery.ClientLogRecord(
        timestamp=rec["time"].timestamp(),
        level=level,
        source="wm",
        group=rec["name"] or "",
        message=log_delivery.redact(rec["message"]),
    )
    try:
        on_loop = asyncio.get_running_loop() is _log_loop
    except RuntimeError:
        on_loop = False
    if on_loop:
        _deliver_record(client_record)
    elif _log_loop is not None:
        _log_loop.call_soon_threadsafe(_deliver_record, client_record)


def install_client_log_sink() -> int:
    """Register the loguru sink and capture the running loop. Returns the loguru
    handler id (for logger.remove in teardown). Call from within the running loop."""
    global _log_loop, _log_sink_id
    _log_loop = asyncio.get_running_loop()
    _log_sink_id = logger.add(_client_log_sink, level="TRACE")
    return _log_sink_id


def _start_log_flush_loop() -> asyncio.Task:
    global _log_flush_task

    async def _loop() -> None:
        while True:
            await asyncio.sleep(_log_interval_ms / 1000)
            _log_batcher.tick()

    _log_flush_task = asyncio.create_task(_loop())
    return _log_flush_task


def _handle_subscribe_logs(writer: asyncio.StreamWriter, params: dict | None) -> dict:
    _log_registry.register(writer, (params or {}).get("minLevel", "INFO"))
    return {}


def _handle_unsubscribe_logs(writer: asyncio.StreamWriter, params: dict | None) -> dict:
    _log_batcher.flush(writer)  # deliver the tail before dropping the subscription
    _log_registry.unregister(writer)
    return {}


# ---------------------------------------------------------------------------
# ER -> WM log forwarding control (ADR-0049)
# ---------------------------------------------------------------------------


def _min_forward_level_name() -> str:
    mlv = _log_registry.min_level_value()  # int | None
    if mlv is None:
        return "INFO"
    for name, val in log_delivery.LOG_LEVEL_VALUES.items():
        if val == mlv:
            return name
    return "INFO"


def _desired_forwarding() -> tuple[bool, str]:
    return (_log_registry.has_subscribers(), _min_forward_level_name())


async def push_er_forwarding_to_runner(runner: ExtensionRunnerInfo) -> None:
    """Send updateLogging to one runner iff its desired state changed. Best-effort."""
    if runner.client is None or not runner.initialized_event.is_set():
        return
    enabled, level = _desired_forwarding()
    normalized = (
        (True, level) if enabled else (False, "")
    )  # level irrelevant when disabled
    if runner.log_forwarding == normalized:
        return
    try:
        from finecode.wm_server.runner import runner_client

        await runner_client.update_logging(runner, normalized[0], level)
        runner.log_forwarding = normalized
    except Exception:
        logger.trace(
            f"updateLogging to {runner.readable_id} failed; will retry on next change"
        )


class _WmClientBridge:
    """``wm_bridge``'s slot, filled by this module since it owns client connections."""

    def notify_all_clients(self, method: str, params: dict[str, typing.Any]) -> None:
        _notify_all_clients(method, params)

    def deliver_er_log_record(
        self, *, source: str, timestamp: float, level: str, group: str, message: str
    ) -> None:
        _deliver_record(
            log_delivery.ClientLogRecord(
                timestamp=timestamp,
                level=level,
                source=source,
                group=group,
                message=log_delivery.redact(message),
            )
        )

    async def push_er_forwarding_to_runner(self, runner: ExtensionRunnerInfo) -> None:
        await push_er_forwarding_to_runner(runner)


wm_bridge.install(_WmClientBridge())


# Deadline for a question nobody ever sees: how long a client that declared it
# can answer is given before the WM gives up on it. Bounds the case where the
# client is alive but its person is not looking; a client that *goes* is not
# waited for at all (`_fail_pending_requests_for`).
_ELICIT_MAX_TIMEOUT_SEC: typing.Final = 900.0


class _WmElicitationBridge:
    """``elicitation_bridge``'s slot, filled by this module since it owns clients.

    Every branch here returns an outcome rather than raising: the ER turns an
    error response into "unavailable" anyway, and a run that asked a question is
    entitled to a typed answer for each way of not getting one (ADR-0082 rule 3).
    """

    async def elicit(
        self,
        *,
        message: str,
        options: list[str],
        default: str | None,
        timeout_sec: float,
        run_writer_key: object | None,
    ) -> dict:
        # Opaque to the runner layer that passed it back, an
        # ``asyncio.StreamWriter`` here: this module put it in the registry the
        # runner read it from, and this module is the only one that dereferences
        # it (`elicitation_bridge`'s module docstring).
        writer = typing.cast("asyncio.StreamWriter", run_writer_key)
        if run_writer_key is None:
            # A run with no identifiable originating connection: dispatched
            # through a non-streaming path, or through no client at all.
            logger.debug("Elicitation: no originating client for this run")
            return {"outcome": "unavailable"}
        if writer not in _connected_clients:
            logger.debug("Elicitation: the originating client is no longer connected")
            return {"outcome": "unavailable"}
        if not _client_capabilities.get(writer, {}).get("elicitation"):
            # Rule 2: known before the question is sent, so the common
            # non-interactive case costs a fast answer instead of a deadline.
            logger.debug(
                f"Elicitation: client '{_client_labels.get(writer)}' did not declare "
                f"that it can answer questions"
            )
            return {"outcome": "unavailable"}

        bounded = min(max(timeout_sec, 1.0), _ELICIT_MAX_TIMEOUT_SEC)
        try:
            result = await _request_client(
                writer,
                "client/elicit",
                {
                    "message": message,
                    "options": options,
                    "default": default,
                    # Informational: the deadline is enforced here, but a client
                    # that holds its own pending request (the MCP server does)
                    # needs to know when to stop holding it.
                    "timeoutSec": bounded,
                },
                timeout_sec=bounded,
            )
        except ClientRequestFailed as exception:
            logger.info(f"Elicitation: no answer — {exception}")
            return {"outcome": "unavailable"}

        outcome = result.get("outcome")
        if outcome == "answered":
            value = result.get("value")
            if value not in options:
                # A client that answered with something nobody offered has not
                # answered the question that was asked.
                logger.warning(
                    f"Elicitation: client answered with {value!r}, which is not one "
                    f"of the offered options; treating the question as unanswered"
                )
                return {"outcome": "unavailable"}
            return {"outcome": "answered", "value": value}
        if outcome == "declined":
            return {"outcome": "declined"}
        logger.warning(f"Elicitation: client returned unknown outcome {outcome!r}")
        return {"outcome": "unavailable"}


elicitation_bridge.install(_WmElicitationBridge())


def _sync_er_forwarding(ws_context: context.WorkspaceContext) -> None:
    """Schedule updateLogging to every running runner to match current subscription state.

    Called (on the loop thread) after any subscribe/unsubscribe/disconnect change.
    """

    async def _run() -> None:
        for per_project in ws_context.ws_projects_extension_runners.values():
            for runner in per_project.values():
                await push_er_forwarding_to_runner(runner)

    asyncio.ensure_future(_run())


async def _schedule_auto_stop() -> None:
    """Wait after the last client disconnects, then stop the server."""
    await asyncio.sleep(_disconnect_timeout)
    if not _connected_clients:
        logger.info(
            f"FineCode API: no clients connected for {_disconnect_timeout}s, shutting down"
        )
        stop()


async def _no_client_timeout() -> None:
    """Stop the server if no client connects within the timeout after startup."""
    await asyncio.sleep(NO_CLIENT_TIMEOUT_SECONDS)
    if not _had_client:
        logger.info(
            f"FineCode API: no client connected within {NO_CLIENT_TIMEOUT_SECONDS}s after startup, shutting down"
        )
        stop()


async def _handle_request_task(
    handler: MethodHandler,
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int,
    label: str,
    method: str,
) -> None:
    """Run a request handler and write the response. Runs as a task so multiple
    requests from the same client can be handled concurrently."""
    try:
        result = await handler(params, ws_context)
        _log_batcher.flush(
            writer
        )  # ADR-0049: force-flush the tail before the final response
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(writer, _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)))
        await writer.drain()
    except ValueError as exc:
        logger.warning(f"FineCode API: invalid request for {method}: {exc}")
        _write_message(writer, _jsonrpc_error(req_id, -32602, str(exc)))
        await writer.drain()
    except RunnerNotFoundError as exc:
        logger.warning(f"FineCode API: unknown runner in {method}: {exc.message}")
        _write_message(writer, _jsonrpc_error(req_id, -32602, exc.message))
        await writer.drain()
    except ConfigurationError as exc:
        logger.warning(f"FineCode API: configuration error in {method}: {exc.message}")
        _write_message(writer, _jsonrpc_error(req_id, -32603, exc.message))
        await writer.drain()
    except ActionCancelledError as exc:
        logger.debug(
            f"FineCode API: action cancelled while handling {method} (client: {label}): {exc}"
        )
        _write_message(
            writer, _jsonrpc_error(req_id, finecode_jsonrpc.REQUEST_CANCELLED, str(exc))
        )
        await writer.drain()
    except (ActionRunFailed, StartingEnvironmentsFailed) as exc:
        logger.error(f"FineCode API: error handling {method} (client: {label}): {exc}")
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()
    except jsonrpc_client.ServerFailedToStart as exc:
        # Already logged with details in runner_manager; no traceback needed here.
        logger.error(
            f"FineCode API: error handling {method} (client: {label}): {exc.message}"
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, exc.message))
        await writer.drain()
    except Exception as exc:
        logger.exception(f"FineCode API: error handling {method} (client: {label})")
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()
    except asyncio.CancelledError:
        pass


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ws_context: context.WorkspaceContext,
) -> None:
    global _auto_stop_task, _had_client, _no_client_timeout_task

    peer = writer.get_extra_info("peername")
    label = str(peer)
    _client_labels[writer] = label
    logger.info(f"FineCode API: client connected from {peer}")
    _connected_clients.add(writer)
    _had_client = True

    # Cancel the initial no-client timeout since a client connected.
    if _no_client_timeout_task is not None and not _no_client_timeout_task.done():
        _no_client_timeout_task.cancel()
        _no_client_timeout_task = None

    # Cancel pending auto-stop since a client connected.
    if _auto_stop_task is not None and not _auto_stop_task.done():
        _auto_stop_task.cancel()
        _auto_stop_task = None

    try:
        while True:
            msg = await _read_message(reader)
            if msg is None:
                break

            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params")
            is_notification = req_id is None

            if method is None:
                # An id without a method is a *response*, not a malformed
                # request: since ADR-0082 this server asks its clients things,
                # and this is what an answer looks like. Answering it with an
                # error — which is what this branch used to do — would have been
                # a response to a response.
                if not is_notification:
                    _resolve_client_response(req_id, msg, writer)
                else:
                    logger.warning(
                        f"[{label}] FineCode API: message with neither id nor "
                        f"method, ignoring"
                    )
                continue

            # Notifications (no id) — dispatch and don't respond.
            if is_notification:
                notification_handler = _NOTIFICATIONS.get(method)
                if notification_handler is not None:
                    logger.trace(f"[{label}] Received notification {method}")
                    try:
                        await notification_handler(params, ws_context)
                    except Exception:
                        logger.exception(
                            f"FineCode API: error in notification {method} (client: {label})"
                        )
                else:
                    logger.trace(
                        f"[{label}] FineCode API: unknown notification {method}, ignoring"
                    )
                continue

            # Requests (has id) — dispatch and respond.
            # ``client/initialize`` and streaming action requests are handled
            # specially because they need access to the writer to send
            # notifications mid-request.
            if method == "client/initialize":
                new_label = (params or {}).get("clientId")
                if new_label:
                    logger.info(
                        f"FineCode API: client {label} identified as '{new_label}'"
                    )
                    _client_labels[writer] = new_label
                    label = new_label
                # Recorded per connection, not per client program: the same CLI
                # binary can answer a question from a terminal and cannot from a
                # pipeline, and it is the connection that knows which it is
                # (ADR-0082 rule 2).
                # `or {}` rather than a `.get` default: a client sending
                # `"capabilities": null` would otherwise crash this dispatch
                # loop before the initialize response is written, leaving it
                # waiting on its own request.
                elicitation = ((params or {}).get("capabilities") or {}).get(
                    "elicitation"
                )
                if elicitation:
                    _client_capabilities[writer] = {"elicitation": elicitation}
                    logger.info(f"FineCode API: client '{label}' can answer questions")
                _write_message(
                    writer,
                    _jsonrpc_response(
                        req_id,
                        {
                            "logFilePath": str(_log_file_path)
                            if _log_file_path is not None
                            else None,
                            # What this server will actually use of what the
                            # client offered, so the client can see it landed.
                            "capabilities": {"elicitation": bool(elicitation)},
                        },
                    ),
                )
                await writer.drain()
                continue

            if method == log_delivery.SUBSCRIBE_METHOD:
                _write_message(
                    writer,
                    _jsonrpc_response(req_id, _handle_subscribe_logs(writer, params)),
                )
                _sync_er_forwarding(ws_context)
                await writer.drain()
                continue

            if method == log_delivery.UNSUBSCRIBE_METHOD:
                _write_message(
                    writer,
                    _jsonrpc_response(req_id, _handle_unsubscribe_logs(writer, params)),
                )
                _sync_er_forwarding(ws_context)
                await writer.drain()
                continue

            if (
                method == "actions/run"
                and (params or {}).get("partialResultToken") is not None
            ):
                # partialResultToken takes priority: the handler also forwards
                # progressToken notifications if present.
                task = asyncio.create_task(
                    _handle_run_action_with_partial_results_task(
                        params, ws_context, writer, req_id
                    )
                )
                if writer not in _running_partial_result_tasks:
                    _running_partial_result_tasks[writer] = set()
                _running_partial_result_tasks[writer].add(task)
                task.add_done_callback(
                    lambda t: (
                        _running_partial_result_tasks[writer].discard(t)
                        if writer in _running_partial_result_tasks
                        else None
                    )
                )
                continue

            if (
                method == "actions/run"
                and (params or {}).get("progressToken") is not None
            ):
                # actions/run with only a progressToken needs writer access to
                # forward progress notifications.
                task = asyncio.create_task(
                    _handle_run_action_with_progress_task(
                        params, ws_context, writer, req_id
                    )
                )
                if writer not in _running_partial_result_tasks:
                    _running_partial_result_tasks[writer] = set()
                _running_partial_result_tasks[writer].add(task)
                task.add_done_callback(
                    lambda t: (
                        _running_partial_result_tasks[writer].discard(t)
                        if writer in _running_partial_result_tasks
                        else None
                    )
                )
                continue

            if (
                method == "actions/runBatch"
                and (params or {}).get("partialResultToken") is not None
            ):
                task = asyncio.create_task(
                    _handle_run_batch_with_partial_results_task(
                        params, ws_context, writer, req_id
                    )
                )
                if writer not in _running_partial_result_tasks:
                    _running_partial_result_tasks[writer] = set()
                _running_partial_result_tasks[writer].add(task)
                task.add_done_callback(
                    lambda t: (
                        _running_partial_result_tasks[writer].discard(t)
                        if writer in _running_partial_result_tasks
                        else None
                    )
                )
                continue

            if (
                method == "actions/runBatch"
                and (params or {}).get("progressToken") is not None
            ):
                task = asyncio.create_task(
                    _handle_run_batch_with_progress_task(
                        params, ws_context, writer, req_id
                    )
                )
                if writer not in _running_partial_result_tasks:
                    _running_partial_result_tasks[writer] = set()
                _running_partial_result_tasks[writer].add(task)
                task.add_done_callback(
                    lambda t: (
                        _running_partial_result_tasks[writer].discard(t)
                        if writer in _running_partial_result_tasks
                        else None
                    )
                )
                continue

            handler = _METHODS.get(method)
            if handler is None:
                _write_message(
                    writer,
                    _jsonrpc_error(req_id, -32601, f"Method not found: {method}"),
                )
                await writer.drain()
                continue

            # Dispatch as a task so the read loop can immediately pick up the
            # next request — this lets concurrent client requests (e.g. multiple
            # runners/checkEnv from a TaskGroup) run in parallel on the server.
            task = asyncio.create_task(
                _handle_request_task(
                    handler, params, ws_context, writer, req_id, label, method
                )
            )
            if writer not in _running_partial_result_tasks:
                _running_partial_result_tasks[writer] = set()
            _running_partial_result_tasks[writer].add(task)
            task.add_done_callback(
                lambda t: (
                    _running_partial_result_tasks[writer].discard(t)
                    if writer in _running_partial_result_tasks
                    else None
                )
            )
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        logger.info(f"FineCode API: client disconnected ({label})")
        try:
            _log_batcher.flush(writer)  # deliver any buffered tail
        except Exception:
            pass
        _log_registry.unregister(writer)
        _sync_er_forwarding(ws_context)
        _connected_clients.discard(writer)
        _client_labels.pop(writer, None)
        _client_capabilities.pop(writer, None)
        # Before the tasks below are cancelled: a run blocked on a question put
        # to this client has to be told at once that nobody can answer it, or it
        # would sit on its deadline while the server counts down to auto-stop.
        _fail_pending_requests_for(writer)

        # Cancel any running partial result tasks for this client
        if writer in _running_partial_result_tasks:
            for task in _running_partial_result_tasks[writer]:
                task.cancel()
            del _running_partial_result_tasks[writer]

        writer.close()
        await writer.wait_closed()

        # Schedule auto-stop if no clients remain.
        if not _connected_clients and not _keep_alive:
            _auto_stop_task = asyncio.create_task(_schedule_auto_stop())


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def start(
    ws_context: context.WorkspaceContext,
    port_file: pathlib.Path | None = None,
    disconnect_timeout: int = DISCONNECT_TIMEOUT_SECONDS,
    keep_alive: bool = False,
) -> None:
    """Start the FineCode API TCP server and write the discovery file.

    Args:
        ws_context: Shared workspace context.
        port_file: Path to write the listening port to.  Defaults to the shared
            discovery file (``_cache_dir() / "wm_port"``).  Pass a custom path
            when starting a dedicated instance so it does not overwrite the shared
            server's discovery file.
        disconnect_timeout: Seconds to wait after the last client disconnects
            before shutting down. Defaults to DISCONNECT_TIMEOUT_SECONDS (30).
        keep_alive: Never stop on our own — neither when no client connects after
            startup nor when the last one disconnects.  For a server whose
            lifetime something else owns (a devcontainer, a supervisor), where
            both timers would end a workspace that is meant to stay warm.
            ``server/shutdown`` still stops it.
    """
    global \
        _server, \
        _discovery_file, \
        _no_client_timeout_task, \
        _had_client, \
        _disconnect_timeout, \
        _keep_alive
    _had_client = False
    _disconnect_timeout = disconnect_timeout
    _keep_alive = keep_alive
    port = _find_free_port()

    _server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, ws_context),
        host="127.0.0.1",
        port=port,
    )

    # Write discovery file so clients can find us.
    _discovery_file = port_file if port_file is not None else discovery_file_path()
    _discovery_file.parent.mkdir(parents=True, exist_ok=True)
    _discovery_file.write_text(str(port))

    logger.info(f"FineCode WM server listening on 127.0.0.1:{port}")
    logger.info(f"Discovery file: {_discovery_file}")

    reset_log_delivery()  # production defaults (interval 200ms)
    install_client_log_sink()
    _start_log_flush_loop()

    if keep_alive:
        logger.info("FineCode WM server: keep-alive, auto-stop timers disabled")
    else:
        # Shut down if no client connects within the timeout.
        _no_client_timeout_task = asyncio.create_task(_no_client_timeout())

    try:
        async with _server:
            await _server.serve_forever()
    finally:
        stop()
        # Clean up workspace resources (runners, IO thread).
        from finecode.wm_server.services import shutdown_service

        await shutdown_service.on_shutdown(ws_context)
        if ws_context.wal_writer is not None:
            ws_context.wal_writer.close()


def stop() -> None:
    """Stop the WM server and remove the discovery file."""
    global _server, _discovery_file, _log_flush_task, _log_sink_id

    # flush any buffered tails to all subscribers before tearing down
    with contextlib.suppress(Exception):
        _log_batcher.flush_all()
    if _log_flush_task is not None:
        _log_flush_task.cancel()
        _log_flush_task = None
    if _log_sink_id is not None:
        try:
            logger.remove(_log_sink_id)
        except ValueError:
            pass
        _log_sink_id = None

    if _server is not None:
        _server.close()
        _server = None

    if _discovery_file is not None and _discovery_file.exists():
        try:
            _discovery_file.unlink()
            logger.trace(f"Removed API discovery file: {_discovery_file}")
        except OSError:
            pass
        _discovery_file = None

    # Cancel any running partial result tasks
    for tasks in _running_partial_result_tasks.values():
        for task in tasks:
            task.cancel()
    _running_partial_result_tasks.clear()


# ---------------------------------------------------------------------------
# Standalone startup (with workspace initialization)
# ---------------------------------------------------------------------------


def _register_callbacks() -> None:
    """Register runner_manager and user_messages callbacks that broadcast
    server→client notifications."""
    from finecode import user_messages
    from finecode.wm_server.runner import runner_manager

    async def on_project_changed(project: domain.Project) -> None:
        _notify_all_clients(
            "actions/treeChanged",
            {
                "node": {
                    "nodeId": str(project.dir_path),
                    "name": project.name,
                    "nodeType": 1,
                    "status": project.status.name,
                    "subnodes": [],
                },
            },
        )

    async def on_user_message(message: str, message_type: str) -> None:
        _notify_all_clients(
            "server/userMessage",
            {
                "message": message,
                "type": message_type.upper(),
            },
        )

    runner_manager.project_changed_callback = on_project_changed
    user_messages._notification_sender = on_user_message


async def start_standalone(
    port_file: pathlib.Path | None = None,
    disconnect_timeout: int = DISCONNECT_TIMEOUT_SECONDS,
    wal_config: wal.WalConfig | None = None,
    otlp_endpoint: str | None = None,
    keep_alive: bool = False,
) -> None:
    """Start the WM server as a standalone process with its own WorkspaceContext.

    Args:
        port_file: Optional custom path to write the listening port to.  Used by
            dedicated instances started via ``start_own_server()`` so they do not
            overwrite the shared server's discovery file.
        disconnect_timeout: Seconds to wait after the last client disconnects
            before shutting down.
        otlp_endpoint: OTLP endpoint for telemetry forwarding to extension runners.
        keep_alive: Disable both auto-stop timers — see ``start()``.
    """
    ws_context = context.WorkspaceContext([])
    ws_context.otlp_endpoint = otlp_endpoint
    if wal_config is not None and wal_config.enabled:
        ws_context.wal_writer = wal.WalWriter(wal_config)
    _register_callbacks()
    await start(
        ws_context,
        port_file=port_file,
        disconnect_timeout=disconnect_timeout,
        keep_alive=keep_alive,
    )
