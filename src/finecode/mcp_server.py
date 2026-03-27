# docs: docs/cli.md
"""FineCode MCP Server — proxy to the FineCode WM server.

Connects to the FineCode WM server over TCP JSON-RPC and translates MCP tool calls into
WM server requests. If no WM server is running, starts one as a subprocess.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import uuid

from finecode.wm_client import ApiClient
from finecode.wm_server import wm_lifecycle
from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_wm_client = ApiClient()
server = Server("FineCode")

_partial_result_queues: dict[str, asyncio.Queue] = {}
_progress_queues: dict[str, asyncio.Queue] = {}


def _setup_partial_result_forwarding() -> None:
    """Register the WM partial-result notification handler.

    Must be called once after ``_wm_client.connect()``.  Each ``actions/partialResult``
    notification is routed by token to the matching per-call asyncio.Queue.
    """

    async def _on_partial_result(params: dict) -> None:
        token = params.get("token")
        value = params.get("value")
        if token and value is not None:
            queue = _partial_result_queues.get(token)
            if queue is not None:
                queue.put_nowait(value)

    _wm_client.on_notification("actions/partialResult", _on_partial_result)


def _setup_progress_forwarding() -> None:
    """Register the WM progress notification handler.

    Must be called once after ``_wm_client.connect()``.  Each ``actions/progress``
    notification is routed by token to the matching per-call asyncio.Queue.
    """

    async def _on_progress(params: dict) -> None:
        token = params.get("token")
        value = params.get("value")
        if token and value is not None:
            queue = _progress_queues.get(token)
            if queue is not None:
                queue.put_nowait(value)

    _wm_client.on_notification("actions/progress", _on_progress)


async def _run_with_progress(
    action: str,
    project: str,
    params: dict,
    options: dict,
    session,
) -> dict:
    """Run a WM action with streaming partial results and progress forwarded as MCP messages.

    ``project`` may be ``""`` to run across all projects that expose the action.
    Each ``actions/partialResult`` notification is forwarded to the MCP client as a
    ``notifications/message`` log message while the call blocks waiting for the final result.
    Progress notifications are forwarded as log messages with the progress metadata.
    """
    token = str(uuid.uuid4())
    progress_token = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    progress_queue: asyncio.Queue = asyncio.Queue()
    _partial_result_queues[token] = queue
    _progress_queues[progress_token] = progress_queue

    async def _forward_partials() -> None:
        try:
            while True:
                value = await queue.get()
                await session.send_log_message(
                    level="info", data=value, logger="finecode"
                )
        except asyncio.CancelledError:
            pass

    async def _forward_progress() -> None:
        try:
            while True:
                value = await progress_queue.get()
                progress_type = value.get("type", "")
                message = value.get("message") or value.get("title") or ""
                percentage = value.get("percentage")
                log_data = {"progress_type": progress_type, "message": message}
                if percentage is not None:
                    log_data["percentage"] = percentage
                await session.send_log_message(
                    level="info", data=log_data, logger="finecode.progress"
                )
        except asyncio.CancelledError:
            pass

    result_task = asyncio.create_task(
        _wm_client.run_action_with_partial_results(
            action, project, token, params, options,
            progress_token=progress_token,
        )
    )
    forward_task = asyncio.create_task(_forward_partials())
    progress_forward_task = asyncio.create_task(_forward_progress())
    try:
        return await result_task
    finally:
        forward_task.cancel()
        progress_forward_task.cancel()
        await asyncio.gather(forward_task, progress_forward_task, return_exceptions=True)
        _partial_result_queues.pop(token, None)
        _progress_queues.pop(progress_token, None)


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Build the MCP tool list from live WM data.

    Fetches all actions and their payload schemas from the WM, then
    constructs one ``Tool`` per action with the real input schema.
    A static ``list_projects`` tool is always included.
    """
    tools: list[Tool] = [
        Tool(
            name="list_projects",
            description="List all projects in the FineCode workspace with their names, paths, and statuses",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_runners",
            description="List all extension runners and their status (running, stopped, error). Use this to diagnose failures when actions do not respond.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_actions",
            description="List actions available in the workspace, optionally filtered to a single project. Returns action names and which projects expose them.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Absolute path to the project directory. Use the list_projects tool to see available projects. Omit to list actions across all projects.",
                    }
                },
            },
        ),
        Tool(
            name="get_project_raw_config",
            description="Return the resolved (post-preset-merge) configuration for a project. Use this to understand what actions and handlers are configured.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Absolute path to the project directory. Use the list_projects tool to see available projects.",
                    }
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="dump_config",
            description="Return the fully resolved project configuration with all presets applied and the presets key removed. Use this to understand the complete effective configuration a project runs with.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Absolute path to the project directory. Use the list_projects tool to see available projects.",
                    }
                },
                "required": ["project"],
            },
        ),
    ]

    actions = await _wm_client.list_actions()

    # Deduplicate: first project that exposes an action owns its schema.
    seen: dict[str, dict] = {}
    for action in actions:
        if action["name"] not in seen:
            seen[action["name"]] = action

    # Group by project to keep schema requests batched.
    unique_by_project: dict[str, list[dict]] = {}
    for action in seen.values():
        unique_by_project.setdefault(action["project"], []).append(action)

    for project_path, project_actions in unique_by_project.items():
        action_names = [a["name"] for a in project_actions]
        try:
            schemas = await _wm_client.get_payload_schemas(project_path, action_names)
        except Exception as exc:
            logger.debug(f"Could not fetch payload schemas for {project_path}: {exc}")
            schemas = {}

        for action in project_actions:
            name = action["name"]
            schema: dict | None = schemas.get(name)
            description = (
                schema.get("description") if schema else None
            ) or f"Run {name} on a project or the whole workspace"
            input_schema: dict = {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Absolute path to the project directory. Use the list_projects tool to see available projects. Omit to run on all projects in the workspace.",
                    },
                    **(schema["properties"] if schema else {}),
                },
                "required": schema.get("required", []) if schema else [],
            }
            tools.append(
                Tool(
                    name=name,
                    description=description,
                    inputSchema=input_schema,
                )
            )

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch an MCP tool call to the WM server."""
    if name == "list_projects":
        result = await _wm_client.list_projects()
        return [TextContent(type="text", text=json.dumps({"projects": result}))]

    if name == "list_runners":
        result = await _wm_client.list_runners()
        return [TextContent(type="text", text=json.dumps({"runners": result}))]

    if name == "list_actions":
        project = arguments.get("project")
        result = await _wm_client.list_actions(project=project)
        return [TextContent(type="text", text=json.dumps({"actions": result}))]

    if name == "get_project_raw_config":
        project = arguments["project"]
        result = await _wm_client.get_project_raw_config(project)
        return [TextContent(type="text", text=json.dumps({"rawConfig": result}))]

    if name == "dump_config":
        project = arguments["project"]
        project_path = pathlib.Path(project)
        raw_config = await _wm_client.get_project_raw_config(project)
        result = await _wm_client.run_action(
            "dump_config",
            project,
            params={
                "source_file_path": str(project_path / "pyproject.toml"),
                "project_raw_config": raw_config,
                "target_file_path": str(
                    project_path / "finecode_config_dump" / "pyproject.toml"
                ),
            },
            options={"resultFormats": ["json"], "trigger": "user", "devEnv": "ai"},
        )
        return [TextContent(type="text", text=json.dumps(result))]

    from mcp.server.lowlevel.server import request_ctx

    session = request_ctx.get().session
    project = arguments.pop("project", None)
    options = {"resultFormats": ["json"], "trigger": "user", "devEnv": "ai"}
    result = await _run_with_progress(
        name, project or "", arguments or {}, options, session
    )
    return [TextContent(type="text", text=json.dumps(result))]


def start(workdir: pathlib.Path, port_file: pathlib.Path | None = None) -> None:
    """Start the MCP server on stdio, connecting to the FineCode API.

    If *port_file* is given, a dedicated WM server is started that writes its
    port to that file instead of the shared discovery file.
    """
    if port_file is not None:
        wm_lifecycle.start_own_server(workdir, port_file=port_file)
        try:
            port = asyncio.run(wm_lifecycle.wait_until_ready_from_file(port_file))
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)
    else:
        wm_lifecycle.ensure_running(workdir)
        try:
            port = asyncio.run(wm_lifecycle.wait_until_ready())
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)

    async def _run() -> None:
        try:
            await _wm_client.connect("127.0.0.1", port, client_id="mcp")
        except (ConnectionRefusedError, OSError) as exc:
            logger.error(
                f"Could not connect to FineCode WM server on port {port}: {exc}"
            )
            sys.exit(1)
        _setup_partial_result_forwarding()
        _setup_progress_forwarding()
        logger.debug(f"Add dir to API Client: {workdir}")
        await _wm_client.add_dir(workdir)
        logger.debug("Added dir")
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
        finally:
            await _wm_client.close()

    asyncio.run(_run())
