"""FineCode API Server — TCP JSON-RPC server for external tool integration.

The API server is the shared backbone that holds the WorkspaceContext. Any client
(LSP server, MCP server, CLI) can start it if not already running and connect to it.
When the last client disconnects, the server shuts down automatically.

Discovery: writes the listening port to .venvs/dev_workspace/cache/finecode/api_port
so clients can find it (same cache directory used for action results).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import socket
import subprocess
import sys
import typing

from loguru import logger

from finecode.api_server import context, domain

CONTENT_LENGTH_HEADER = "Content-Length: "
AUTO_STOP_DELAY_SECONDS = 5
NO_CLIENT_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _jsonrpc_response(id: int | str, result: typing.Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _jsonrpc_error(
    id: int | str | None, code: int, message: str
) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Content-Length framing (shared with finecode_jsonrpc)
# ---------------------------------------------------------------------------


async def _read_message(reader: asyncio.StreamReader) -> dict | None:
    """Read one Content-Length framed JSON-RPC message. Returns None on EOF."""
    header_line = await reader.readline()
    if not header_line:
        return None
    header = header_line.decode("utf-8").strip()
    if not header.startswith(CONTENT_LENGTH_HEADER):
        logger.warning(f"FineCode API: unexpected header: {header!r}")
        return None
    content_length = int(header[len(CONTENT_LENGTH_HEADER) :])

    # Read the blank separator line
    separator = await reader.readline()
    if separator.strip():
        logger.warning(f"FineCode API: expected blank line, got: {separator!r}")

    body = await reader.readexactly(content_length)
    return json.loads(body.decode("utf-8"))


def _write_message(writer: asyncio.StreamWriter, msg: dict) -> None:
    """Write one Content-Length framed JSON-RPC message."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    writer.write(header + body)


# ---------------------------------------------------------------------------
# Method handlers (requests — client sends id, server responds)
# See docs/api-protocol.md for full protocol documentation.
# ---------------------------------------------------------------------------

NOT_IMPLEMENTED_CODE = -32002
NOT_IMPLEMENTED_MSG = "Not yet implemented"

MethodHandler = typing.Callable[
    [dict | None, context.WorkspaceContext],
    typing.Coroutine[typing.Any, typing.Any, typing.Any],
]

NotificationHandler = typing.Callable[
    [dict | None, context.WorkspaceContext],
    typing.Coroutine[typing.Any, typing.Any, None],
]


class _NotImplementedError(Exception):
    """Raised by stubs to signal that the method is not yet implemented."""


def _stub(method_name: str) -> MethodHandler:
    """Create a stub handler that raises _NotImplementedError."""

    async def handler(
        params: dict | None, ws_context: context.WorkspaceContext
    ) -> typing.Any:
        raise _NotImplementedError(f"{method_name}: {NOT_IMPLEMENTED_MSG}")

    handler.__doc__ = f"Stub for {method_name}. See docs/api-protocol.md."
    return handler


def _notification_stub(method_name: str) -> NotificationHandler:
    """Create a stub notification handler that logs and does nothing."""

    async def handler(
        params: dict | None, ws_context: context.WorkspaceContext
    ) -> None:
        logger.trace(f"FineCode API: notification {method_name} received (stub, ignoring)")

    handler.__doc__ = f"Stub for {method_name}. See docs/api-protocol.md."
    return handler


# -- Server → client notifications ------------------------------------------


def _notify_all_clients(method: str, params: dict) -> None:
    """Broadcast a JSON-RPC notification to all connected clients."""
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    for writer in list(_connected_clients):
        try:
            _write_message(writer, msg)
        except Exception:
            logger.trace(f"FineCode API: failed to notify client, skipping")


def _project_to_dict(project: domain.Project) -> dict:
    return {
        "name": project.name,
        "path": str(project.dir_path),
        "status": project.status.name,
    }


# -- Implemented handlers --------------------------------------------------


async def _handle_list_projects(
    params: dict | None, ws_context: context.WorkspaceContext
) -> list[dict]:
    """List all projects. Params: {}. Result: [{name, path, status}]."""
    return [_project_to_dict(p) for p in ws_context.ws_projects.values()]


async def _handle_add_dir(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Add a workspace directory. Discovers projects, reads configs, starts runners."""
    from finecode.api_server.config import read_configs
    from finecode.api_server.runner import runner_manager

    dir_path = pathlib.Path(params["dir_path"])

    if dir_path in ws_context.ws_dirs_paths:
        return {"projects": []}

    ws_context.ws_dirs_paths.append(dir_path)
    new_projects = await read_configs.read_projects_in_dir(dir_path, ws_context)

    for project in new_projects:
        await read_configs.read_project_config(
            project=project, ws_context=ws_context, resolve_presets=False
        )

    try:
        await runner_manager.start_runners_with_presets(
            projects=new_projects,
            ws_context=ws_context,
            initialize_all_handlers=True,
        )
    except runner_manager.RunnerFailedToStart as exc:
        _notify_all_clients("server/userMessage", {
            "message": f"Starting runners failed: {exc.message}. "
                       f"Did you run `finecode prepare-envs`?",
            "type": "ERROR",
        })

    return {"projects": [_project_to_dict(p) for p in new_projects]}


async def _handle_remove_dir(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Remove a workspace directory. Stops runners, removes affected projects."""
    from finecode.api_server.runner import runner_manager

    dir_path = pathlib.Path(params["dir_path"])
    ws_context.ws_dirs_paths.remove(dir_path)

    for project_dir in list(ws_context.ws_projects.keys()):
        if not project_dir.is_relative_to(dir_path):
            continue

        # Keep if the project is also under another remaining ws_dir.
        keep = any(
            project_dir.is_relative_to(d) for d in ws_context.ws_dirs_paths
        )
        if keep:
            continue

        runners = ws_context.ws_projects_extension_runners.get(project_dir, {})
        for runner in runners.values():
            await runner_manager.stop_extension_runner(runner=runner)
        del ws_context.ws_projects[project_dir]
        ws_context.ws_projects_raw_configs.pop(project_dir, None)

    return {}


async def _handle_list_actions(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """List available actions, optionally filtered by project name."""
    project_filter = (params or {}).get("project")
    actions = []
    for project in ws_context.ws_projects.values():
        if project_filter and project.name != project_filter:
            continue
        if project.actions is None:
            continue
        for action in project.actions:
            actions.append({
                "name": action.name,
                "source": action.source,
                "project": project.name,
                "handlers": [
                    {"name": h.name, "source": h.source, "env": h.env}
                    for h in action.handlers
                ],
            })
    return {"actions": actions}


# -- Method dispatch tables ------------------------------------------------

_METHODS: dict[str, MethodHandler] = {
    # workspace/
    "workspace/listProjects": _handle_list_projects,
    "workspace/addDir": _handle_add_dir,
    "workspace/removeDir": _handle_remove_dir,
    # actions/
    "actions/list": _handle_list_actions,
    "actions/getTree": _stub("actions/getTree"),
    "actions/run": _stub("actions/run"),
    "actions/runBatch": _stub("actions/runBatch"),
    "actions/runWithPartialResults": _stub("actions/runWithPartialResults"),
    "actions/reload": _stub("actions/reload"),
    # runners/
    "runners/list": _stub("runners/list"),
    "runners/restart": _stub("runners/restart"),
    # server/
    "server/shutdown": _stub("server/shutdown"),
}

_NOTIFICATIONS: dict[str, NotificationHandler] = {
    # documents/
    "documents/opened": _notification_stub("documents/opened"),
    "documents/closed": _notification_stub("documents/closed"),
    "documents/changed": _notification_stub("documents/changed"),
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


async def _schedule_auto_stop() -> None:
    """Wait a bit after the last client disconnects, then stop the server."""
    await asyncio.sleep(AUTO_STOP_DELAY_SECONDS)
    if not _connected_clients:
        logger.info("FineCode API: no clients connected, shutting down")
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
                    try:
                        await notification_handler(params, ws_context)
                    except Exception as exc:
                        logger.exception(f"FineCode API: error in notification {method}")
                else:
                    logger.trace(f"FineCode API: unknown notification {method}, ignoring")
                continue

            # Requests (has id) — dispatch and respond.
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
                logger.exception(f"FineCode API: error handling {method}")
                _write_message(
                    writer, _jsonrpc_error(req_id, -32603, str(exc))
                )
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        logger.info(f"FineCode API: client disconnected ({peer})")
        _connected_clients.discard(writer)
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


def _cache_dir() -> pathlib.Path:
    """Return the FineCode cache directory inside the dev_workspace venv."""
    return pathlib.Path(sys.executable).parent.parent / "cache" / "finecode"


def discovery_file_path() -> pathlib.Path:
    return _cache_dir() / "api_port"


def read_port() -> int | None:
    """Read the API server port from the discovery file. Returns None if not found."""
    path = discovery_file_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running() -> bool:
    """Check if an API server is already listening (discovery file exists and port responds)."""
    port = read_port()
    if port is None:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            return True
    except (ConnectionRefusedError, OSError):
        return False


def ensure_running(workdir: pathlib.Path) -> None:
    """Start the API server as a subprocess if not already running."""
    if is_running():
        return

    python_cmd = sys.executable
    logger.info(f"Starting FineCode API server subprocess in {workdir}")
    subprocess.Popen(
        [python_cmd, "-m", "finecode", "start-api-server", "--workdir", str(workdir)],
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_until_ready(timeout: float = 30) -> int:
    """Wait for the API server to become available. Returns the port."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if is_running():
            port = read_port()
            if port is not None:
                return port
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"FineCode API server did not start within {timeout}s. "
        f"Check logs for errors."
    )


async def start(ws_context: context.WorkspaceContext) -> None:
    """Start the FineCode API TCP server and write the discovery file."""
    global _server, _discovery_file, _no_client_timeout_task, _had_client
    _had_client = False
    port = _find_free_port()

    _server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, ws_context),
        host="127.0.0.1",
        port=port,
    )

    # Write discovery file so clients can find us.
    _discovery_file = discovery_file_path()
    _discovery_file.parent.mkdir(parents=True, exist_ok=True)
    _discovery_file.write_text(str(port))

    logger.info(f"FineCode API server listening on 127.0.0.1:{port}")
    logger.info(f"Discovery file: {_discovery_file}")

    # Shut down if no client connects within the timeout.
    _no_client_timeout_task = asyncio.create_task(_no_client_timeout())

    try:
        async with _server:
            await _server.serve_forever()
    finally:
        stop()
        # Clean up workspace resources (runners, IO thread).
        from finecode.api_server.services import shutdown_service
        shutdown_service.on_shutdown(ws_context)


def stop() -> None:
    """Stop the API server and remove the discovery file."""
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


# ---------------------------------------------------------------------------
# Standalone startup (with workspace initialization)
# ---------------------------------------------------------------------------


def _register_callbacks() -> None:
    """Register runner_manager and user_messages callbacks that broadcast
    server→client notifications."""
    from finecode import user_messages
    from finecode.api_server.runner import runner_manager

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


async def start_standalone(workdir: pathlib.Path) -> None:
    """Start the API server as a standalone process with its own WorkspaceContext.

    Discovers projects, reads configs, and starts extension runners before
    accepting client connections. Used when no LSP server is running.
    """
    ws_context = context.WorkspaceContext([])
    _register_callbacks()
    # await _handle_add_dir({"dir_path": str(workdir)}, ws_context)
    await start(ws_context)
