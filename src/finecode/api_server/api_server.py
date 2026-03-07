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


def _snake_to_camel(s: str) -> str:
    """Convert snake_case to camelCase."""
    parts = s.split('_')
    return parts[0] + ''.join(word.capitalize() for word in parts[1:])


class _NoConvert:
    """Wrap a value to prevent camelCase conversion of its contents."""
    def __init__(self, value: typing.Any) -> None:
        self.value = value


def _convert_to_camel_case(obj: typing.Any) -> typing.Any:
    """Recursively convert all snake_case keys to camelCase in dicts/lists."""
    if isinstance(obj, _NoConvert):
        return obj.value
    if isinstance(obj, dict):
        return {_snake_to_camel(k): _convert_to_camel_case(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_to_camel_case(item) for item in obj]
    else:
        return obj


def _jsonrpc_response(id: int | str, result: typing.Any) -> dict:
    # Convert result to camelCase before embedding in response
    camel_result = _convert_to_camel_case(result)
    return {"jsonrpc": "2.0", "id": id, "result": camel_result}


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
    # Convert params to camelCase before sending
    camel_params = _convert_to_camel_case(params)
    msg = {"jsonrpc": "2.0", "method": method, "params": camel_params}
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


async def _handle_find_project_for_file(
    params: dict, ws_context: context.WorkspaceContext
) -> dict:
    """Return project name containing a given file.

    It finds the *nearest* project in the
    workspace that actually "uses finecode" (i.e. has a valid config).  The
    project is determined purely based on path containment.

    **Params:** ``{"file_path": "/abs/path/to/file"}``
    **Result:** ``{"project": "project_name"}`` or ``{"project": null}`` if
    the file does not belong to any suitable project.
    """

    file_path = pathlib.Path(params["file_path"])

    # iterate over known projects in reverse-sorted order so that nested/child
    # projects are considered before their parents.  This mirrors the behaviour
    # in ``find_project_with_action_for_file`` but without any action-specific
    # checks.
    sorted_dirs = list(ws_context.ws_projects.keys())
    # reverse sort by path (string) ensures children come first
    sorted_dirs.sort(reverse=True)

    for project_dir in sorted_dirs:
        if file_path.is_relative_to(project_dir):
            project = ws_context.ws_projects[project_dir]
            if project.status == domain.ProjectStatus.CONFIG_VALID:
                return {"project": project.name}
            # skip projects that aren't using finecode
            continue

    # not in any project or none of the containing projects are CONFIG_VALID
    return {"project": None}


async def _handle_add_dir(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Add a workspace directory. Discovers projects, reads configs, starts runners."""
    from finecode.api_server.config import read_configs
    from finecode.api_server.runner import runner_manager

    dir_path = pathlib.Path(params["dir_path"])
    logger.trace(f"Add ws dir: {dir_path}")

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
    logger.trace(f'Remove ws dir: {dir_path}')
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


async def _handle_run_action(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Run an action on a project."""
    params = params or {}
    action_name = params.get("action")
    project_name = params.get("project")
    action_params = params.get("params", {})
    options = params.get("options", {})

    if not action_name:
        raise ValueError("action parameter is required")
    if not project_name:
        raise ValueError("project parameter is required")

    # Find the project
    project = None
    for proj in ws_context.ws_projects.values():
        if proj.name == project_name:
            project = proj
            break

    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    # Import run_service here to avoid circular imports
    from finecode.api_server.services import run_service

    result_format_strs: list[str] = options.get("result_formats", ["json"])
    result_formats = [
        run_service.RunResultFormat(fmt)
        for fmt in result_format_strs
        if fmt in ("json", "string")
    ]
    trigger = run_service.RunActionTrigger(options.get("trigger", "unknown"))
    dev_env = run_service.DevEnv(options.get("dev_env", "cli"))

    try:
        result = await run_service.run_action(
            action_name=action_name,
            params=action_params,
            project_def=project,
            ws_context=ws_context,
            run_trigger=trigger,
            dev_env=dev_env,
            result_formats=result_formats,
            preprocess_payload=True,
            initialize_all_handlers=True,
        )
        return {
            "result_by_format": _NoConvert(result.result_by_format),
            "return_code": result.return_code,
        }
    except run_service.ActionRunFailed as e:
        raise RuntimeError(f"Action failed: {e}")

from finecode.api_server.services.action_tree import (
    _handle_get_tree,
)
from finecode.api_server.services.document_sync import (
    handle_documents_opened,
    handle_documents_closed,
    handle_documents_changed,
)


async def _handle_actions_reload(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Reload an action's handlers in all relevant extension runners.

    Params: ``{"action_node_id": "project_path::action_name"}``
    Result: ``{}``
    """
    from finecode.api_server.runner import runner_client

    params = params or {}
    action_node_id = params.get("action_node_id", "")
    parts = action_node_id.split("::")
    if len(parts) < 2:
        raise ValueError(f"Invalid action_node_id: {action_node_id!r}")

    project_path = pathlib.Path(parts[0])
    action_name = parts[1]

    runners_by_env = ws_context.ws_projects_extension_runners.get(project_path, {})
    for runner in runners_by_env.values():
        await runner_client.reload_action(runner, action_name)

    return {}


async def _handle_runners_list(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """List all extension runners and their status.

    Result: ``{"runners": [{"project_path", "env_name", "status", "readable_id"}]}``
    """
    from finecode.api_server.runner import runner_client

    runners = []
    for project_path, runners_by_env in ws_context.ws_projects_extension_runners.items():
        for env_name, runner in runners_by_env.items():
            runners.append({
                "project_path": str(project_path),
                "env_name": env_name,
                "status": runner.status.name,
                "readable_id": runner.readable_id,
            })
    return {"runners": runners}


async def _handle_runners_restart(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Restart a specific extension runner.

    Params: ``{"runner_working_dir": "/abs/path", "env_name": "dev_workspace", "debug": false}``
    Result: ``{}``
    """
    from finecode.api_server.runner import runner_manager

    params = params or {}
    runner_working_dir = params.get("runner_working_dir")
    env_name = params.get("env_name")
    debug = params.get("debug", False)

    if not runner_working_dir or not env_name:
        raise ValueError("runner_working_dir and env_name are required")

    await runner_manager.restart_extension_runner(
        runner_working_dir_path=pathlib.Path(runner_working_dir),
        env_name=env_name,
        ws_context=ws_context,
        debug=debug,
    )
    return {}


async def _handle_server_reset(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Reset the server state.

    Result: ``{}``
    """
    logger.info("FineCode API: server reset requested")
    return {}


# -- helpers ---------------------------------------------------------------

def _notify_client(writer: asyncio.StreamWriter, method: str, params: dict) -> None:
    """Send a notification to a single client only.

    Unlike ``_notify_all_clients`` this helper targets the provided writer,
    which is useful for streaming partial results back to the request originator
    without broadcasting to every connected client.
    """
    camel_params = _convert_to_camel_case(params)
    msg = {"jsonrpc": "2.0", "method": method, "params": camel_params}
    try:
        _write_message(writer, msg)
    except Exception:
        logger.trace("FineCode API: failed to notify client, skipping")


# -- Request handlers ------------------------------------------------------

async def _handle_run_with_partial_results(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
) -> dict:
    """Handle the ``actions/runWithPartialResults`` request.

    The handler uses :mod:`partial_results_service` to obtain an async iterator
    of partial values and forwards them to the requesting client only.  When the
    iterator completes an aggregated result dict is returned exactly as the
    ``actions/run`` method would produce.
    """
    if params is None:
        raise ValueError("params required")
    action_name = params.get("action")
    token = params.get("partial_result_token")
    if not action_name or token is None:
        raise ValueError("action and partial_result_token are required")
    project_name = params.get("project", "")
    options = params.get("options", {})

    from finecode.api_server.services import run_service, partial_results_service

    trigger = run_service.RunActionTrigger(options.get("trigger", "system"))
    dev_env = run_service.DevEnv(options.get("dev_env", "ide"))
    result_formats = options.get("result_formats", ["json"])

    logger.info(f"runWithPartialResults: action={action_name} project={project_name!r} token={token} formats={result_formats}")

    stream = await partial_results_service.run_action_with_partial_results(
        action_name=action_name,
        project_name=project_name,
        params=params.get("params", {}),
        partial_result_token=token,
        run_trigger=trigger,
        dev_env=dev_env,
        ws_context=ws_context,
        result_formats=result_formats,
    )

    partial_count = 0
    async for value in stream:
        partial_count += 1
        logger.trace(f"runWithPartialResults: sending partial #{partial_count} for token={token}, keys={list(value.keys()) if isinstance(value, dict) else type(value)}")
        # Wrap the per-format action data to prevent camelCase conversion of result content.
        protected_value = dict(value)
        if "result_by_format" in protected_value:
            protected_value["result_by_format"] = _NoConvert(protected_value["result_by_format"])
        _notify_client(
            writer,
            "actions/partialResult",
            {"token": token, "value": protected_value},
        )
        await writer.drain()

    final = await stream.final_result()
    logger.trace(f"runWithPartialResults: done, sent {partial_count} partials, final keys={list(final.keys()) if isinstance(final, dict) else type(final)}")
    # Protect action result data from camelCase conversion.
    if "result_by_format" in final:
        final = dict(final)
        final["result_by_format"] = _NoConvert(final["result_by_format"])
    return final


async def _handle_run_with_partial_results_task(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int | str,
) -> None:
    """Task to handle the ``actions/runWithPartialResults`` request asynchronously.

    This runs in a separate task to avoid blocking the client handler loop
    during long-running actions.
    """
    try:
        result = await _handle_run_with_partial_results(
            params, ws_context, writer
        )
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(
            writer,
            _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)),
        )
        await writer.drain()
    except Exception as exc:
        logger.exception(
            "FineCode API: error handling actions/runWithPartialResults"
        )
        _write_message(
            writer, _jsonrpc_error(req_id, -32603, str(exc))
        )
        await writer.drain()


# -- Method dispatch tables ------------------------------------------------

_METHODS: dict[str, MethodHandler] = {
    # workspace/
    "workspace/listProjects": _handle_list_projects,
    "workspace/findProjectForFile": _handle_find_project_for_file,
    "workspace/addDir": _handle_add_dir,
    "workspace/removeDir": _handle_remove_dir,
    # actions/
    "actions/list": _handle_list_actions,
    "actions/getTree": _handle_get_tree,
    "actions/run": _handle_run_action,
    "actions/runBatch": _stub("actions/runBatch"),
    # (runWithPartialResults is handled specially in _handle_client)
    "actions/reload": _handle_actions_reload,
    # runners:
    "runners/list": _handle_runners_list,
    "runners/restart": _handle_runners_restart,
    # server/
    "server/reset": _handle_server_reset,
    "server/shutdown": _stub("server/shutdown"),
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
                    logger.trace(f"Received notification {method}")
                    try:
                        await notification_handler(params, ws_context)
                    except Exception as exc:
                        logger.exception(f"FineCode API: error in notification {method}")
                else:
                    logger.trace(f"FineCode API: unknown notification {method}, ignoring")
                continue

            # Requests (has id) — dispatch and respond.
            # ``actions/runWithPartialResults`` is handled specially because it
            # needs access to the writer in order to stream notifications back to
            # the requesting client only.  Any other method uses the generic
            # _METHODS table.
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
        [python_cmd, "-m", "finecode", "start-api-server", "--trace"],
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


async def start_standalone() -> None:
    """Start the API server as a standalone process with its own WorkspaceContext.
    """
    ws_context = context.WorkspaceContext([])
    _register_callbacks()
    await start(ws_context)
