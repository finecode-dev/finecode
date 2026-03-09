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
import os
import pathlib
import socket
import subprocess
import sys
import typing

from loguru import logger

from finecode.api_server import context, domain

CONTENT_LENGTH_HEADER = "Content-Length: "
DISCONNECT_TIMEOUT_SECONDS = 30
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


async def _handle_get_project_raw_config(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Return the resolved raw config for a project by name.

    Params: ``{"project": "project_name"}``
    Result: ``{"raw_config": {...}}``
    """
    params = params or {}
    project_name = params.get("project")
    if not project_name:
        raise ValueError("project parameter is required")

    for project_dir_path, project in ws_context.ws_projects.items():
        if project.name == project_name:
            raw_config = ws_context.ws_projects_raw_configs.get(project_dir_path, {})
            return _NoConvert({"raw_config": raw_config})

    raise ValueError(f"Project '{project_name}' not found")


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
    """Add a workspace directory. Discovers projects, reads configs, starts runners.

    Params:
      dir_path: str - absolute path to the workspace directory
      start_runners: bool - whether to start extension runners (default true).
        When false, configs are read and actions collected without starting any
        runners. Useful when runner environments may not exist yet (e.g. before
        running prepare-envs).
    """
    from finecode.api_server.config import collect_actions, read_configs
    from finecode.api_server.runner import runner_manager
    from finecode.api_server.runner.runner_client import RunnerStatus

    params = params or {}
    dir_path = pathlib.Path(params["dir_path"])
    start_runners: bool = params.get("start_runners", True)
    logger.trace(f"Add ws dir: {dir_path}")

    if dir_path in ws_context.ws_dirs_paths:
        return {"projects": []}

    ws_context.ws_dirs_paths.append(dir_path)
    new_projects = await read_configs.read_projects_in_dir(dir_path, ws_context)

    for project in new_projects:
        await read_configs.read_project_config(
            project=project, ws_context=ws_context, resolve_presets=False
        )

    if not start_runners:
        # Collect actions directly from raw config without needing runners.
        from finecode.api_server.config import config_models
        for project in new_projects:
            if project.status == domain.ProjectStatus.CONFIG_VALID:
                try:
                    collect_actions.collect_actions(
                        project_path=project.dir_path, ws_context=ws_context
                    )
                except config_models.ConfigurationError as exc:
                    logger.warning(
                        f"Failed to collect actions for {project.name}: {exc.message}"
                    )
        return {"projects": [_project_to_dict(p) for p in new_projects]}

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

    # If config overrides were set before this addDir call (e.g. standalone CLI mode),
    # apply them to the newly discovered projects and push to their running runners.
    if ws_context.handler_config_overrides and new_projects:
        action_names = list(ws_context.handler_config_overrides.keys())
        _apply_config_overrides_to_projects(new_projects, action_names, ws_context.handler_config_overrides)
        try:
            async with asyncio.TaskGroup() as tg:
                for project in new_projects:
                    runners = ws_context.ws_projects_extension_runners.get(project.dir_path, {})
                    for runner in runners.values():
                        if runner.status == RunnerStatus.RUNNING:
                            tg.create_task(
                                runner_manager.update_runner_config(
                                    runner=runner,
                                    project=project,
                                    handlers_to_initialize=None,
                                )
                            )
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.warning(f"Failed to push config update to runner: {exc}")

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
    except run_service.ActionRunFailed:
        raise

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


async def _handle_start_runners(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Start extension runners for all (or specified) projects.

    Complements any runners already running — only missing runners are started.
    Resolves presets so that ``project.actions`` reflects preset-defined handlers.

    Params: ``{"projects": ["project_name", ...]}`` (optional, default: all projects)
    Result: ``{}``
    """
    from finecode.api_server.runner import runner_manager

    params = params or {}
    project_names: list[str] | None = params.get("projects")

    projects = list(ws_context.ws_projects.values())
    if project_names is not None:
        projects = [p for p in projects if p.name in project_names]

    try:
        await runner_manager.start_runners_with_presets(
            projects=projects,
            ws_context=ws_context,
        )
    except runner_manager.RunnerFailedToStart as exc:
        raise ValueError(f"Starting runners failed: {exc.message}") from exc

    return {}


async def _handle_runners_check_env(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Check whether an environment is valid for a given project.

    Params: ``{"project": "project_name", "env_name": "dev_workspace"}``
    Result: ``{"valid": bool}``
    """
    from finecode.api_server.runner import runner_manager

    params = params or {}
    project_name = params.get("project")
    env_name = params.get("env_name")

    if not project_name or not env_name:
        raise ValueError("project and env_name are required")

    project = next(
        (p for p in ws_context.ws_projects.values() if p.name == project_name), None
    )
    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    valid = await runner_manager.check_runner(
        runner_dir=project.dir_path, env_name=env_name
    )
    return {"valid": valid}


async def _handle_runners_remove_env(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Remove an environment for a given project.

    Stops the runner if running, then deletes the environment directory.

    Params: ``{"project": "project_name", "env_name": "dev_workspace"}``
    Result: ``{}``
    """
    from finecode.api_server.runner import runner_manager

    params = params or {}
    project_name = params.get("project")
    env_name = params.get("env_name")

    if not project_name or not env_name:
        raise ValueError("project and env_name are required")

    project = next(
        (p for p in ws_context.ws_projects.values() if p.name == project_name), None
    )
    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    # Stop the runner if it is currently running.
    runners = ws_context.ws_projects_extension_runners.get(project.dir_path, {})
    runner = runners.get(env_name)
    if runner is not None:
        await runner_manager.stop_extension_runner(runner=runner)

    runner_manager.remove_runner_venv(runner_dir=project.dir_path, env_name=env_name)
    return {}


async def _handle_server_reset(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Reset the server state.

    Result: ``{}``
    """
    logger.info("FineCode API: server reset requested")
    return {}


async def _handle_set_config_overrides(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Handle ``workspace/setConfigOverrides``.

    Stores handler config overrides persistently in the workspace context so that
    they are applied to all subsequent action runs. These overrides survive across
    multiple requests and do not require runners to be stopped first.

    If extension runners are already running they receive a config-update push
    immediately; their initialized handlers are dropped and will be re-initialized
    with the new config on the next run.
    """
    from finecode.api_server.runner import runner_manager
    from finecode.api_server.runner.runner_client import RunnerStatus

    params = params or {}
    overrides: dict = params.get("overrides", {})

    ws_context.handler_config_overrides = overrides

    # Apply to all existing project domain objects so that project.action_handler_configs
    # reflects the new overrides
    all_projects = list(ws_context.ws_projects.values())
    action_names = list(overrides.keys())
    if all_projects and action_names:
        _apply_config_overrides_to_projects(all_projects, action_names, overrides)

    # Push the updated config to any already-running runners so they drop their
    # initialized handlers and pick up the new config on the next invocation.
    try:
        async with asyncio.TaskGroup() as tg:
            for project_path, runners_by_env in ws_context.ws_projects_extension_runners.items():
                project = ws_context.ws_projects.get(project_path)
                if project is None or project.actions is None:
                    continue
                for runner in runners_by_env.values():
                    if runner.status == RunnerStatus.RUNNING:
                        tg.create_task(
                            runner_manager.update_runner_config(
                                runner=runner,
                                project=project,
                                handlers_to_initialize=None,
                            )
                        )
    except* Exception as eg:
        for exc in eg.exceptions:
            logger.warning(f"Failed to push config update to runner: {exc}")

    return {}


def _apply_config_overrides_to_projects(
    projects: list[domain.Project],
    actions: list[str],
    config_overrides: dict[str, dict[str, dict[str, typing.Any]]],
) -> dict[pathlib.Path, dict[str, dict[str, typing.Any]]]:
    """Apply handler config overrides to project.action_handler_configs.

    ``config_overrides`` format: ``{action_name: {handler_name_or_"": {param: value}}}``
    where the empty-string key ``""`` means all handlers of that action.

    Returns the original ``action_handler_configs`` per project.
    """
    originals: dict[pathlib.Path, dict[str, dict[str, typing.Any]]] = {}
    actions_set = set(actions)
    for project in projects:
        if project.actions is None:
            continue
        originals[project.dir_path] = {
            source: dict(cfg)
            for source, cfg in project.action_handler_configs.items()
        }
        for action in project.actions:
            if action.name not in actions_set:
                continue
            action_overrides = config_overrides.get(action.name, {})
            if not action_overrides:
                continue
            action_level = action_overrides.get("", {})
            for handler in action.handlers:
                handler_specific = action_overrides.get(handler.name, {})
                merged = {**action_level, **handler_specific}
                if merged:
                    project.action_handler_configs[handler.source] = {
                        **(project.action_handler_configs.get(handler.source) or {}),
                        **merged,
                    }
    return originals


async def _handle_run_batch(
    params: dict | None, ws_context: context.WorkspaceContext
) -> typing.Any:
    """Run multiple actions across multiple (or all) projects.

    Params:
      actions: list[str] - action names to run
      projects: list[str] | None - project names to filter; absent/null means all projects
      params: dict - action payload shared across all projects
      params_by_project: dict[str, dict] - per-project payload overrides keyed by project path string
      options:
        concurrently: bool - run actions concurrently within each project (default false)
        result_formats: list[str] - "string" and/or "json" (default ["string"])
        trigger: str - run trigger (default "user")
        dev_env: str - dev environment (default "cli")

    Result: snake_case keys throughout (entire result is protected from camelCase conversion).
      {"results": {project_path_str: {action_name: {"result_by_format": ..., "return_code": int}}},
       "return_code": int}
    """
    from finecode.api_server.services import run_service

    params = params or {}
    actions: list[str] = params.get("actions", [])
    project_names: list[str] | None = params.get("projects")
    action_params: dict = params.get("params", {})
    params_by_project: dict[str, dict] = params.get("params_by_project", {})
    options: dict = params.get("options", {})

    concurrently: bool = options.get("concurrently", False)
    result_format_strs: list[str] = options.get("result_formats", ["string"])
    result_formats = [
        run_service.RunResultFormat(fmt)
        for fmt in result_format_strs
        if fmt in ("json", "string")
    ]
    trigger = run_service.RunActionTrigger(options.get("trigger", "user"))
    dev_env = run_service.DevEnv(options.get("dev_env", "cli"))

    if not actions:
        raise ValueError("actions list is required and must be non-empty")

    # Build actions_by_project (path -> [action_names])
    if project_names is not None:
        actions_by_project: dict[pathlib.Path, list[str]] = {}
        for project_name in project_names:
            project = next(
                (p for p in ws_context.ws_projects.values() if p.name == project_name),
                None,
            )
            if project is None:
                raise ValueError(f"Project '{project_name}' not found")
            actions_by_project[project.dir_path] = list(actions)
    else:
        actions_by_project = run_service.find_projects_with_actions(ws_context, actions)
        if not actions_by_project:
            raise ValueError(f"No projects found with actions: {actions}")

    await run_service.start_required_environments(
        actions_by_project, ws_context, update_config_in_running_runners=True
    )

    result_by_project = await run_service.run_actions_in_projects(
        actions_by_project=actions_by_project,
        action_payload=action_params,
        ws_context=ws_context,
        concurrently=concurrently,
        result_formats=result_formats,
        run_trigger=trigger,
        dev_env=dev_env,
        payload_overrides_by_project=params_by_project,
    )

    overall_return_code = 0
    results: dict[str, dict] = {}
    for project_path, actions_result in result_by_project.items():
        project_results: dict[str, dict] = {}
        for action_name, response in actions_result.items():
            overall_return_code |= response.return_code
            project_results[action_name] = {
                "result_by_format": response.result_by_format,
                "return_code": response.return_code,
            }
        results[str(project_path)] = project_results

    # Protect the entire result from camelCase conversion: action names like
    # "check_formatting" must not become "checkFormatting", and nested keys
    # (result_by_format, return_code) must stay snake_case for the CLI client.
    return _NoConvert({
        "results": results,
        "return_code": overall_return_code,
    })


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
    "workspace/setConfigOverrides": _handle_set_config_overrides,
    "workspace/getProjectRawConfig": _handle_get_project_raw_config,
    "workspace/startRunners": _handle_start_runners,
    # actions/
    "actions/list": _handle_list_actions,
    "actions/getTree": _handle_get_tree,
    "actions/run": _handle_run_action,
    "actions/runBatch": _handle_run_batch,
    # (runWithPartialResults is handled specially in _handle_client)
    "actions/reload": _handle_actions_reload,
    # runners:
    "runners/list": _handle_runners_list,
    "runners/restart": _handle_runners_restart,
    "runners/checkEnv": _handle_runners_check_env,
    "runners/removeEnv": _handle_runners_remove_env,
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


def start_own_server(workdir: pathlib.Path) -> pathlib.Path:
    """Start a dedicated API server subprocess for exclusive use by one CLI call.

    Unlike ``ensure_running()``, this always starts a *fresh* process and writes
    the listening port to a temporary file (not the shared discovery file), so it
    does not interfere with a concurrently running shared API server (e.g. the one
    used by the LSP/MCP clients).

    Returns the path to the temporary port file.  Pass it to
    ``wait_until_ready_from_file()`` to obtain the port and connect.
    The server auto-stops ``AUTO_STOP_DELAY_SECONDS`` after the client disconnects.
    """
    import tempfile

    fd, port_file_str = tempfile.mkstemp(suffix=".finecode_port")
    os.close(fd)
    port_file = pathlib.Path(port_file_str)
    # Write empty content so the server knows to overwrite rather than append.
    port_file.write_text("")

    logger.info(f"Starting dedicated FineCode API server in {workdir}")
    subprocess.Popen(
        [sys.executable, "-m", "finecode", "start-api-server", "--port-file", str(port_file)],
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return port_file


async def wait_until_ready_from_file(
    port_file: pathlib.Path, timeout: float = 30
) -> int:
    """Wait for a dedicated API server using a custom port file. Returns the port."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            content = port_file.read_text().strip()
            if content:
                port = int(content)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(("127.0.0.1", port))
                    return port
        except (FileNotFoundError, ValueError, OSError):
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Dedicated FineCode API server did not start within {timeout}s. "
        "Check logs for errors."
    )


async def start(
    ws_context: context.WorkspaceContext,
    port_file: pathlib.Path | None = None,
    disconnect_timeout: int = DISCONNECT_TIMEOUT_SECONDS,
) -> None:
    """Start the FineCode API TCP server and write the discovery file.

    Args:
        ws_context: Shared workspace context.
        port_file: Path to write the listening port to.  Defaults to the shared
            discovery file (``_cache_dir() / "api_port"``).  Pass a custom path
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


async def start_standalone(
    port_file: pathlib.Path | None = None,
    disconnect_timeout: int = DISCONNECT_TIMEOUT_SECONDS,
) -> None:
    """Start the API server as a standalone process with its own WorkspaceContext.

    Args:
        port_file: Optional custom path to write the listening port to.  Used by
            dedicated instances started via ``start_own_server()`` so they do not
            overwrite the shared server's discovery file.
        disconnect_timeout: Seconds to wait after the last client disconnects
            before shutting down.
    """
    ws_context = context.WorkspaceContext([])
    _register_callbacks()
    await start(ws_context, port_file=port_file, disconnect_timeout=disconnect_timeout)
