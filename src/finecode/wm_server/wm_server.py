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

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers import (
    _handle_actions_reload,
    _handle_add_dir,
    _handle_find_project_for_file,
    _handle_get_payload_schemas,
    _handle_get_tree,
    _handle_list_actions,
    _handle_list_projects,
    _handle_get_project_raw_config,
    _handle_remove_dir,
    _handle_run_action,
    _handle_run_action_with_progress_task,
    _handle_run_batch,
    _handle_run_batch_with_progress_task,
    _handle_run_with_partial_results_task,
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
    "workspace/startRunners": _handle_start_runners,
    # actions/
    "actions/list": _handle_list_actions,
    "actions/getTree": _handle_get_tree,
    "actions/getPayloadSchemas": _handle_get_payload_schemas,
    "actions/run": _handle_run_action,
    "actions/runBatch": _handle_run_batch,
    # (runWithPartialResults is handled specially in _handle_client)
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
            # ``client/initialize`` and ``actions/runWithPartialResults`` are
            # handled specially because they need access to the writer.
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

            if method == "actions/runWithPartialResults":
                # Spawn a task to handle this long-running request without blocking
                # the client handler loop. This allows the client to send other
                # requests while this action is running.
                task = asyncio.create_task(
                    _handle_run_with_partial_results_task(
                        params, ws_context, writer, req_id
                    )
                )
                # Track the task associated with this client
                if writer not in _running_partial_result_tasks:
                    _running_partial_result_tasks[writer] = set()
                _running_partial_result_tasks[writer].add(task)
                task.add_done_callback(lambda t: _running_partial_result_tasks[writer].discard(t) if writer in _running_partial_result_tasks else None)
                continue

            if method == "actions/run" and (params or {}).get("progressToken") is not None:
                # actions/run with a progressToken needs writer access to
                # forward progress notifications, so handle like runWithPartialResults.
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

            try:
                result = await handler(params, ws_context)
                _write_message(writer, _jsonrpc_response(req_id, result))
                await writer.drain()
            except _NotImplementedError as exc:
                _write_message(
                    writer, _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc))
                )
                await writer.drain()
            except Exception as exc:
                logger.exception(f"FineCode API: error handling {method} (client: {label})")
                _write_message(
                    writer, _jsonrpc_error(req_id, -32603, str(exc))
                )
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        logger.info(f"FineCode API: client disconnected ({label})")
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
    global _server, _discovery_file

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
                "node_id": str(project.dir_path),
                "name": project.name,
                "node_type": 1,
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
) -> None:
    """Start the WM server as a standalone process with its own WorkspaceContext.

    Args:
        port_file: Optional custom path to write the listening port to.  Used by
            dedicated instances started via ``start_own_server()`` so they do not
            overwrite the shared server's discovery file.
        disconnect_timeout: Seconds to wait after the last client disconnects
            before shutting down.
    """
    ws_context = context.WorkspaceContext([])
    if wal_config is not None and wal_config.enabled:
        ws_context.wal_writer = wal.WalWriter(wal_config)
    _register_callbacks()
    await start(ws_context, port_file=port_file, disconnect_timeout=disconnect_timeout)
