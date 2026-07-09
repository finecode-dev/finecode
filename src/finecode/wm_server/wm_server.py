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
import pathlib
import socket
import typing

from loguru import logger

import finecode_jsonrpc
import finecode_jsonrpc.client as jsonrpc_client

from finecode.wm_server import context, domain
from finecode.wm_server.errors import ConfigurationError
from finecode.wm_server.services import log_delivery
from finecode.wm_server.services.run_service.exceptions import (
    ActionCancelledError,
    ActionRunFailed,
    StartingEnvironmentsFailed,
)
from finecode.wm_server._api_handlers import (
    _handle_actions_reload,
    _handle_add_dir,
    _handle_find_project_for_file,
    _handle_get_payload_schemas,
    _handle_get_tree,
    _handle_list_actions,
    _handle_list_projects,
    _handle_prepare_envs,
    _handle_get_project_raw_config,
    _handle_get_workspace_editable_packages,
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
    _handle_server_reset,
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
    _NotImplementedError,
    _jsonrpc_error,
    _jsonrpc_response,
    _read_message,
    _write_message,
)
from finecode.wm_server.wm_lifecycle import discovery_file_path
from finecode.wm_server import wal

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

    Returns static information about the running WM Server instance,
    including the path to its log file.
    """
    return {
        "logFilePath": str(_log_file_path) if _log_file_path is not None else None,
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
    "server/reset": _handle_server_reset,
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


# ---------------------------------------------------------------------------
# Client log streaming (ADR-0049) — subscription registry, batching, sink
# ---------------------------------------------------------------------------

_log_registry: log_delivery.SubscriptionRegistry
_log_batcher: log_delivery.LogBatcher
_log_flush_task: asyncio.Task | None = None
_log_loop: asyncio.AbstractEventLoop | None = None
_log_sink_id: int | None = None
_log_interval_ms: int = 200  # timer cadence; the LogBatcher hides its interval, so track it here


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
        _emit_log_records, interval_ms=interval_ms,
        max_batch=max_batch, buffer_limit=buffer_limit,
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


async def push_er_forwarding_to_runner(runner) -> None:
    """Send updateLogging to one runner iff its desired state changed. Best-effort."""
    if runner.client is None or not runner.initialized_event.is_set():
        return
    enabled, level = _desired_forwarding()
    normalized = (True, level) if enabled else (False, "")  # level irrelevant when disabled
    if runner.log_forwarding == normalized:
        return
    try:
        from finecode.wm_server.runner import runner_client

        await runner_client.update_logging(runner, normalized[0], level)
        runner.log_forwarding = normalized
    except Exception:
        logger.trace(f"updateLogging to {runner.readable_id} failed; will retry on next change")


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
        logger.info(f"FineCode API: no clients connected for {_disconnect_timeout}s, shutting down")
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
        _log_batcher.flush(writer)  # ADR-0049: force-flush the tail before the final response
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(writer, _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)))
        await writer.drain()
    except ValueError as exc:
        logger.warning(f"FineCode API: invalid request for {method}: {exc}")
        _write_message(writer, _jsonrpc_error(req_id, -32602, str(exc)))
        await writer.drain()
    except ConfigurationError as exc:
        logger.warning(f"FineCode API: configuration error in {method}: {exc.message}")
        _write_message(writer, _jsonrpc_error(req_id, -32603, exc.message))
        await writer.drain()
    except ActionCancelledError as exc:
        logger.debug(f"FineCode API: action cancelled while handling {method} (client: {label}): {exc}")
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
        logger.error(f"FineCode API: error handling {method} (client: {label}): {exc.message}")
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
                if not is_notification:
                    _write_message(
                        writer, _jsonrpc_error(req_id, -32600, "Invalid request: no method")
                    )
                    await writer.drain()
                continue

            # Notifications (no id) — dispatch and don't respond.
            if is_notification:
                notification_handler = _NOTIFICATIONS.get(method)
                if notification_handler is not None:
                    logger.trace(f"[{label}] Received notification {method}")
                    try:
                        await notification_handler(params, ws_context)
                    except Exception as exc:
                        logger.exception(f"FineCode API: error in notification {method} (client: {label})")
                else:
                    logger.trace(f"[{label}] FineCode API: unknown notification {method}, ignoring")
                continue

            # Requests (has id) — dispatch and respond.
            # ``client/initialize`` and streaming action requests are handled
            # specially because they need access to the writer to send
            # notifications mid-request.
            if method == "client/initialize":
                new_label = (params or {}).get("clientId")
                if new_label:
                    logger.info(f"FineCode API: client {label} identified as '{new_label}'")
                    _client_labels[writer] = new_label
                    label = new_label
                _write_message(writer, _jsonrpc_response(req_id, {
                    "logFilePath": str(_log_file_path) if _log_file_path is not None else None,
                }))
                await writer.drain()
                continue

            if method == log_delivery.SUBSCRIBE_METHOD:
                _write_message(
                    writer, _jsonrpc_response(req_id, _handle_subscribe_logs(writer, params))
                )
                _sync_er_forwarding(ws_context)
                await writer.drain()
                continue

            if method == log_delivery.UNSUBSCRIBE_METHOD:
                _write_message(
                    writer, _jsonrpc_response(req_id, _handle_unsubscribe_logs(writer, params))
                )
                _sync_er_forwarding(ws_context)
                await writer.drain()
                continue

            if method == "actions/run" and (params or {}).get("partialResultToken") is not None:
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
                task.add_done_callback(lambda t: _running_partial_result_tasks[writer].discard(t) if writer in _running_partial_result_tasks else None)
                continue

            if method == "actions/run" and (params or {}).get("progressToken") is not None:
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
                task.add_done_callback(lambda t: _running_partial_result_tasks[writer].discard(t) if writer in _running_partial_result_tasks else None)
                continue

            if method == "actions/runBatch" and (params or {}).get("partialResultToken") is not None:
                task = asyncio.create_task(
                    _handle_run_batch_with_partial_results_task(
                        params, ws_context, writer, req_id
                    )
                )
                if writer not in _running_partial_result_tasks:
                    _running_partial_result_tasks[writer] = set()
                _running_partial_result_tasks[writer].add(task)
                task.add_done_callback(lambda t: _running_partial_result_tasks[writer].discard(t) if writer in _running_partial_result_tasks else None)
                continue

            if method == "actions/runBatch" and (params or {}).get("progressToken") is not None:
                task = asyncio.create_task(
                    _handle_run_batch_with_progress_task(
                        params, ws_context, writer, req_id
                    )
                )
                if writer not in _running_partial_result_tasks:
                    _running_partial_result_tasks[writer] = set()
                _running_partial_result_tasks[writer].add(task)
                task.add_done_callback(lambda t: _running_partial_result_tasks[writer].discard(t) if writer in _running_partial_result_tasks else None)
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
                _handle_request_task(handler, params, ws_context, writer, req_id, label, method)
            )
            if writer not in _running_partial_result_tasks:
                _running_partial_result_tasks[writer] = set()
            _running_partial_result_tasks[writer].add(task)
            task.add_done_callback(
                lambda t: _running_partial_result_tasks[writer].discard(t)
                if writer in _running_partial_result_tasks else None
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

        # Cancel any running partial result tasks for this client
        if writer in _running_partial_result_tasks:
            for task in _running_partial_result_tasks[writer]:
                task.cancel()
            del _running_partial_result_tasks[writer]

        writer.close()
        await writer.wait_closed()

        # Schedule auto-stop if no clients remain.
        if not _connected_clients:
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
    """
    global _server, _discovery_file, _no_client_timeout_task, _had_client, _disconnect_timeout
    _had_client = False
    _disconnect_timeout = disconnect_timeout
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

    # Shut down if no client connects within the timeout.
    _no_client_timeout_task = asyncio.create_task(_no_client_timeout())

    try:
        async with _server:
            await _server.serve_forever()
    finally:
        stop()
        # Clean up workspace resources (runners, IO thread).
        from finecode.wm_server.services import shutdown_service
        shutdown_service.on_shutdown(ws_context)
        if ws_context.wal_writer is not None:
            ws_context.wal_writer.close()


def stop() -> None:
    """Stop the WM server and remove the discovery file."""
    global _server, _discovery_file, _log_flush_task, _log_sink_id

    # flush any buffered tails to all subscribers before tearing down
    try:
        _log_batcher.flush_all()
    except Exception:
        pass
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
        _notify_all_clients("actions/treeChanged", {
            "node": {
                "nodeId": str(project.dir_path),
                "name": project.name,
                "nodeType": 1,
                "status": project.status.name,
                "subnodes": [],
            },
        })

    async def on_user_message(message: str, message_type: str) -> None:
        _notify_all_clients("server/userMessage", {
            "message": message,
            "type": message_type.upper(),
        })

    runner_manager.project_changed_callback = on_project_changed
    user_messages._notification_sender = on_user_message


async def start_standalone(
    port_file: pathlib.Path | None = None,
    disconnect_timeout: int = DISCONNECT_TIMEOUT_SECONDS,
    wal_config: wal.WalConfig | None = None,
    otlp_endpoint: str | None = None,
) -> None:
    """Start the WM server as a standalone process with its own WorkspaceContext.

    Args:
        port_file: Optional custom path to write the listening port to.  Used by
            dedicated instances started via ``start_own_server()`` so they do not
            overwrite the shared server's discovery file.
        disconnect_timeout: Seconds to wait after the last client disconnects
            before shutting down.
        otlp_endpoint: OTLP endpoint for telemetry forwarding to extension runners.
    """
    ws_context = context.WorkspaceContext([])
    ws_context.otlp_endpoint = otlp_endpoint
    if wal_config is not None and wal_config.enabled:
        ws_context.wal_writer = wal.WalWriter(wal_config)
    _register_callbacks()
    await start(ws_context, port_file=port_file, disconnect_timeout=disconnect_timeout)
