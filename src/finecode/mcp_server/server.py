# docs: docs/cli.md
"""FineCode MCP Server — proxy to the FineCode WM server.

Connects to the FineCode WM server over TCP JSON-RPC and translates MCP tool calls into
WM server requests. If no WM server is running, starts one as a subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import sys
import uuid

from finecode_extension_api.resource_uri import path_to_resource_uri
from loguru import logger

import finecode_jsonrpc
from finecode import telemetry
from finecode.wm_client import ApiClient, ReconnectPolicy
from finecode.wm_server import wm_lifecycle

_wm_client = ApiClient()

_partial_result_queues: dict[str, asyncio.Queue] = {}
_progress_queues: dict[str, asyncio.Queue] = {}

_wm_port: int | None = None
_workdir: pathlib.Path | None = None
_wm_connected: bool = False

# Populated by _handle_list_tools(); maps MCP tool name (action name) → action source.
_tool_name_to_source: dict[str, str] = {}

_client_name: str | None = None
_session: finecode_jsonrpc.JsonRpcServerSession | None = None

_PROJECT_ARG_DESCRIPTION = (
    "Absolute path to the project directory. Use the list_projects tool to see"
    " available projects."
)

# Tools the MCP server implements itself. The recovery ones are hardcoded rather
# than actions because an action executes inside the runner it would replace.
# Their descriptions are the only thing that tells a caller which rung to pick —
# staleness is never detected (ADR-0075) — so each names what it covers and what
# to reach for next.
_META_TOOLS: list[dict] = [
    {
        "name": "list_projects",
        "description": "List all projects in the FineCode workspace with their names, paths, and statuses",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_runners",
        "description": "List all extension runners and their status (running, stopped, error). Use this to diagnose failures when actions do not respond.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_actions",
        "description": "List actions available in the workspace, optionally filtered to a single project. Returns action names and which projects expose them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Omit to list actions across all projects.",
                }
            },
        },
    },
    {
        "name": "get_project_raw_config",
        "description": "Return the resolved (post-preset-merge) configuration for a project. Use this to understand what actions and handlers are configured.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": _PROJECT_ARG_DESCRIPTION}
            },
            "required": ["project"],
        },
    },
    {
        "name": "dump_config",
        "description": "Return the fully resolved project configuration with all presets applied and the presets key removed. Use this to understand the complete effective configuration a project runs with.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": _PROJECT_ARG_DESCRIPTION}
            },
            "required": ["project"],
        },
    },
    {
        "name": "reload_action",
        "description": (
            "Make a running FineCode workspace pick up an edit to an action or"
            " its handlers, without restarting anything."
            " Covers: edits to the packages that own the action class and its"
            " handler classes — the extension runner re-imports exactly those"
            " top-level packages, in every environment of every target project."
            " Does not cover: an edit to any other package (a shared library, a"
            " module those packages import) — use restart_runner, which replaces"
            " the whole interpreter; or an edit to pyproject.toml or a preset,"
            " which no restart picks up either, because a runner re-reads code"
            " and not configuration — use reload_config for that."
            " Nothing is detected automatically: pick the rung that matches what"
            " you edited. This is the cheapest one, so prefer it when it applies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Name of the action to reload, as returned by the list_actions tool.",
                },
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Omit to reload the action in every project that exposes it.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "restart_runner",
        "description": (
            "Replace the extension runner processes of a project, so a running"
            " FineCode workspace picks up an edit anywhere in the code they"
            " imported."
            " Covers: edits to any Python module a runner has loaded, including"
            " shared libraries no single action owns, and runners that are stuck,"
            " crashed or failed. Refused while an action is running in the target"
            " project — the refusal names what is running, and killInFlightRuns"
            " proceeds anyway and kills it."
            " Does not cover: an edit to pyproject.toml or a preset — a restarted"
            " runner re-reads code, not configuration, so use reload_config for"
            " those."
            " Prefer reload_action when only an action and its handlers changed:"
            " it starts no processes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Required unless allProjects is true.",
                },
                "allProjects": {
                    "type": "boolean",
                    "description": "Restart the runners of every project in the workspace. Supply this or project, never both — the whole workspace is asked for, never defaulted into.",
                },
                "env": {
                    "type": "string",
                    "description": "Name of a single execution environment to restart (e.g. dev_no_runtime). Omit to restart every environment of each target project.",
                },
                "killInFlightRuns": {
                    "type": "boolean",
                    "description": "Proceed even though an action is running in the target, killing it. Every run in flight there dies mid-execution: its caller gets a transport failure, and for an action with side effects nobody can tell whether they completed. Only set this when the run is stuck, or when you know what is running and accept losing it — the refusal names both.",
                },
            },
        },
    },
    {
        "name": "reload_config",
        "description": (
            "Make the configuration on disk take effect for a project, so a"
            " running FineCode workspace stops using the configuration it read"
            " at startup."
            " Covers: every edit to a project's configuration — pyproject.toml,"
            " finecode.toml or a preset: a changed handler parameter, an added"
            " or removed action, a new environment, a handler moved between"
            " environments — and, because"
            " the project's runners are replaced as part of it, every code"
            " change the narrower rungs cover as well. Use it when unsure which"
            " kind of edit you made: it is the rung that is right either way."
            " Refused while an action is running in the target project, since"
            " replacing its runners would kill that run; the refusal names what is"
            " running, and killInFlightRuns proceeds anyway and kills it."
            " Does not cover: an edit to FineCode's own source, which runs in the"
            " workspace server process rather than in a runner — use restart_wm"
            " for that."
            " Costs process startups proportional to the target, so prefer"
            " restart_runner or reload_action when you know the edit was code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Required unless allProjects is true.",
                },
                "allProjects": {
                    "type": "boolean",
                    "description": "Recover every project in the workspace. Supply this or project, never both — the whole workspace is asked for, never defaulted into. Use it after editing a shared preset.",
                },
                "rescan": {
                    "type": "boolean",
                    "description": "Walk the workspace directories again first, picking up projects created since the server started. Needed only when a new project directory appeared.",
                },
                "killInFlightRuns": {
                    "type": "boolean",
                    "description": "Proceed even though an action is running in the target, killing it. Every run in flight there dies mid-execution: its caller gets a transport failure, and for an action with side effects nobody can tell whether they completed. Only set this when the run is stuck, or when you know what is running and accept losing it — the refusal names both.",
                },
            },
        },
    },
    {
        "name": "restart_wm",
        "description": (
            "Replace the FineCode workspace server process itself, so a running"
            " session picks up an edit to FineCode's own code."
            " Covers: every change the narrower rungs cover, plus edits to the"
            " workspace server — the part that reads configuration, routes"
            " actions and owns the runners, and which no reload or runner"
            " restart re-imports because it is the process performing them."
            " Does not cover: nothing above it; this is the widest rung."
            " Every connected client is disconnected and every runner is"
            " stopped. Other clients reconnect on their own and this tool"
            " reports which ones it disturbed; runs in flight anywhere in the"
            " workspace die and are not resumed. Prefer reload_config, which"
            " leaves other projects and other clients alone."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


async def _attach_session(*, first_connect: bool) -> None:
    """Establish everything the WM holds on this client's behalf.

    Called by ``ApiClient`` on first connect and again after every reconnect, so
    the two cannot drift (ADR-0074 rule 3).  A restarted WM knows nothing about
    this client: it has not been told the workspace directory, and the actions it
    now exposes may differ from the ones the tool list was built from.
    """
    global _wm_port
    if not first_connect:
        # A restarted WM listens on a new port (ADR-0002) and the module-level
        # value is what the log lines and any later connect would use.
        _wm_port = wm_lifecycle.running_port() or _wm_port

    logger.debug(f"MCP: Add dir to API Client: {_workdir}")
    await _wm_client.add_dir(_workdir)
    logger.info(f"MCP: Added workspace dir {_workdir}")

    if not first_connect:
        # The action set may have changed while this client was disconnected.
        await _invalidate_tool_list()


async def _ensure_wm_connected() -> None:
    global _wm_connected
    if _wm_connected:
        return

    client_id = f"mcp-{_client_name}" if _client_name else "mcp"
    logger.info(
        f"MCP: Connecting to WM server on 127.0.0.1:{_wm_port} as {client_id!r}"
    )
    # Registered before connecting: the handlers live on the client object and
    # survive a reconnect, so only the server-side session needs re-establishing.
    _setup_partial_result_forwarding()
    _setup_progress_forwarding()
    _wm_client.configure_reconnect(
        ReconnectPolicy(may_start_server=True, workdir=_workdir),
        on_reattach=_attach_session,
    )
    try:
        await _wm_client.connect("127.0.0.1", _wm_port, client_id=client_id)
    except BaseException:
        # The socket and reader task outlive a `connect` that failed in
        # `_attach_session`, while `_wm_connected` stays false — so every
        # retried request would open one more and leave it behind.
        await _wm_client.close()
        raise
    logger.info("MCP: Connected to WM server")
    _wm_connected = True


def _setup_partial_result_forwarding() -> None:
    """Register the WM partial-result notification handler."""

    async def _on_partial_result(params: dict) -> None:
        token = params.get("token")
        value = params.get("value")
        if token and value is not None:
            queue = _partial_result_queues.get(token)
            if queue is not None:
                queue.put_nowait(value)

    _wm_client.on_notification("actions/partialResult", _on_partial_result)


def _setup_progress_forwarding() -> None:
    """Register the WM progress notification handler."""

    async def _on_progress(params: dict) -> None:
        token = params.get("token")
        value = params.get("value")
        if token and value is not None:
            queue = _progress_queues.get(token)
            if queue is not None:
                queue.put_nowait(value)

    _wm_client.on_notification("actions/progress", _on_progress)


async def _send_log_message(
    level: str, data: object, logger_name: str = "finecode"
) -> None:
    if _session is None:
        return
    await _session.send_notification(
        "notifications/message",
        {"level": level, "data": data, "logger": logger_name},
    )


async def _invalidate_tool_list() -> None:
    """Drop the cached action tools and tell the client its list is stale.

    An action that a recovery added is unreachable until the client asks for the
    tool list again, and it has no reason to ask unless it is told.
    """
    _tool_name_to_source.clear()
    if _session is None:
        return
    await _session.send_notification("notifications/tools/list_changed", {})


async def _run_with_progress(
    action_source: str,
    project: str,
    params: dict,
    options: dict,
) -> dict:
    """Run a WM action with streaming partial results and progress forwarded as MCP messages."""
    token = str(uuid.uuid4())
    progress_token = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    progress_queue: asyncio.Queue = asyncio.Queue()
    _partial_result_queues[token] = queue
    _progress_queues[progress_token] = progress_queue

    # Opt-in: have the WM type-safely merge streamed partials per project/action and
    # return the merged result, so the data returned below is complete even when a
    # project streams several partials.
    options = {**(options or {}), "mergeResults": True}

    async def _forward_partials() -> None:
        try:
            while True:
                value = await queue.get()
                await _send_log_message("info", value)
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
                await _send_log_message("info", log_data, "finecode.progress")
        except asyncio.CancelledError:
            pass

    result_task = asyncio.create_task(
        _wm_client.run_action(
            action_source,
            project,
            params,
            options,
            progress_token=progress_token,
            partial_result_token=token,
        )
    )
    forward_task = asyncio.create_task(_forward_partials())
    progress_forward_task = asyncio.create_task(_forward_progress())
    try:
        result = await result_task
    finally:
        # Yield to let any notification handler tasks that were scheduled
        # concurrently with result_task completion finish enqueuing their values.
        await asyncio.sleep(0)
        forward_task.cancel()
        progress_forward_task.cancel()
        await asyncio.gather(
            forward_task, progress_forward_task, return_exceptions=True
        )
        _partial_result_queues.pop(token, None)
        _progress_queues.pop(progress_token, None)

    # Expose the WM's type-safely merged results, flattening
    # {project: {actionSource: {resultByFormat, returnCode}}} to the
    # {project: resultByFormat} shape MCP reports.
    server_results = result.pop("results", None) if isinstance(result, dict) else None
    if server_results:
        results_by_project = {
            project_key: action_results.get(action_source, {}).get("resultByFormat", {})
            for project_key, action_results in server_results.items()
        }
        results_by_project = {k: v for k, v in results_by_project.items() if v}
        if results_by_project:
            result = {**result, "resultsByProject": results_by_project}
    return result


# ---------------------------------------------------------------------------
# MCP protocol handlers
# ---------------------------------------------------------------------------


async def _handle_initialize(params: dict | None) -> dict:
    global _client_name
    if params:
        client_info = params.get("clientInfo") or {}
        _client_name = client_info.get("name")
    return {
        # A recovery can add or remove actions, and a client that cached the tool
        # list would keep calling the old one; listChanged is what lets it be told.
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {"listChanged": True}},
        "serverInfo": {"name": "FineCode", "version": "1.0.0"},
    }


async def _handle_ping(_params: dict | None) -> dict:
    return {}


async def _handle_list_tools(_params: dict | None) -> dict:
    """Build the MCP tool list from live WM data."""
    logger.info("MCP tools/list called")
    await _ensure_wm_connected()

    tools: list[dict] = [*_META_TOOLS]

    try:
        actions = await _wm_client.list_actions()
        logger.info(f"MCP: Fetched {len(actions)} actions from WM")
    except Exception as exc:
        logger.error(f"MCP: Failed to fetch actions from WM: {exc}", exc_info=True)
        actions = []

    # Deduplicate by source: first project that exposes an action owns its schema.
    seen: dict[str, dict] = {}
    for action in actions:
        if action["source"] not in seen:
            seen[action["source"]] = action

    logger.info(f"MCP: After deduplication, {len(seen)} unique actions")

    # Group by project to keep schema requests batched.
    unique_by_project: dict[str, list[dict]] = {}
    for action in seen.values():
        unique_by_project.setdefault(action["project"], []).append(action)

    logger.info(f"MCP: Actions grouped by {len(unique_by_project)} projects")

    _tool_name_to_source.clear()
    for project_path, project_actions in unique_by_project.items():
        action_sources = [a["source"] for a in project_actions]
        logger.debug(
            f"MCP: Fetching schemas for {len(action_sources)} actions in {project_path}"
        )
        try:
            schemas = await _wm_client.get_payload_schemas(project_path, action_sources)
            logger.debug(f"MCP: Got {len(schemas)} schemas for {project_path}")
        except Exception as exc:
            logger.error(
                f"MCP: Could not fetch payload schemas for {project_path}: {exc}",
                exc_info=True,
            )
            schemas = {}

        for action in project_actions:
            name = action["name"]
            source = action["source"]
            _tool_name_to_source[name] = source
            schema: dict | None = schemas.get(source)
            description = (
                schema.get("description") if schema else None
            ) or f"Run {name} on a project or the whole workspace"
            # A workspace-scoped action is always dispatched once and routed by
            # the WM itself (see partial_results_service.run_action_with_partial_results:
            # it resolves the workspace root when no project is given, and rejects
            # an explicit one). Exposing "project" here would suggest a caller can
            # scope the run by picking a project, which is wrong for these — the
            # action's own payload carries whatever restriction field it defines
            # instead (e.g. lint's project_paths), already included via schema
            # properties below.
            properties: dict = dict(schema["properties"]) if schema else {}
            if action.get("scope") != "workspace":
                properties = {
                    "project": {
                        "type": "string",
                        "description": "Absolute path to the project directory. Use the list_projects tool to see available projects. Omit to run on all projects in the workspace.",
                    },
                    **properties,
                }
            input_schema: dict = {
                "type": "object",
                "properties": properties,
                "required": schema.get("required", []) if schema else [],
            }
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": input_schema,
                }
            )

    logger.info(f"MCP tools/list returning {len(tools)} tools total")
    return {"tools": tools}


async def _resolve_action_source(tool_name: str) -> str:
    """Map an action tool name to the action source the WM addresses it by.

    ``_tool_name_to_source`` is only filled by ``tools/list``, so a caller that
    reaches a tool from a cached list this process never built would otherwise
    have its tool name passed through as a source and silently miss.
    """
    source = _tool_name_to_source.get(tool_name)
    if source is not None:
        return source

    for action in await _wm_client.list_actions():
        _tool_name_to_source.setdefault(action["name"], action["source"])
    # An unknown name may already be a source (ADR-0019 alias); let the WM judge.
    return _tool_name_to_source.get(tool_name, tool_name)


async def _handle_call_tool(params: dict | None) -> dict:
    """Dispatch an MCP tool call to the WM server."""
    if not params:
        return {
            "content": [{"type": "text", "text": "Missing params"}],
            "isError": True,
        }

    name = params.get("name", "")
    with telemetry.mcp_tool_span(name):
        arguments: dict = dict(params.get("arguments") or {})

        await _ensure_wm_connected()

        if name == "list_projects":
            result = await _wm_client.list_projects()
            return {
                "content": [{"type": "text", "text": json.dumps({"projects": result})}]
            }

        if name == "list_runners":
            result = await _wm_client.list_runners()
            return {
                "content": [{"type": "text", "text": json.dumps({"runners": result})}]
            }

        if name == "list_actions":
            project = arguments.get("project")
            result = await _wm_client.list_actions(project=project)
            return {
                "content": [{"type": "text", "text": json.dumps({"actions": result})}]
            }

        if name == "get_project_raw_config":
            project = arguments["project"]
            result = await _wm_client.get_project_raw_config(project)
            return {
                "content": [{"type": "text", "text": json.dumps({"rawConfig": result})}]
            }

        if name == "reload_action":
            action = arguments.get("action")
            if not action:
                return {
                    "content": [{"type": "text", "text": "Missing 'action' argument"}],
                    "isError": True,
                }
            result = await _wm_client.reload_action(
                action_source=await _resolve_action_source(action),
                project=arguments.get("project"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        if name == "restart_runner":
            result = await _wm_client.restart_runner(
                project=arguments.get("project"),
                all_projects=arguments.get("allProjects", False),
                env=arguments.get("env"),
                kill_in_flight_runs=arguments.get("killInFlightRuns", False),
            )
            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        if name == "reload_config":
            projects = await _wm_client.reload_config(
                project=arguments.get("project"),
                all_projects=arguments.get("allProjects", False),
                rescan=arguments.get("rescan", False),
                kill_in_flight_runs=arguments.get("killInFlightRuns", False),
            )
            if any(
                entry.get("actionsAdded") or entry.get("actionsRemoved")
                for entry in projects
            ):
                await _invalidate_tool_list()
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"projects": projects})}
                ]
            }

        if name == "restart_wm":
            # Read before the server goes away: this is the disclosure of whose
            # session the caller is about to disturb (PRD-0008 R8). It informs
            # rather than gates — the others reconnect on their own (ADR-0074).
            info = await _wm_client.get_info()
            own_label = f"mcp-{_client_name}" if _client_name else "mcp"
            other_clients = [
                label for label in info.get("clients", []) if label != own_label
            ]
            replacement = await wm_lifecycle.replace_running_server(
                _wm_client, _workdir
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "restarted": True,
                                "previousPid": info.get("pid"),
                                "port": replacement["port"],
                                "otherClientsDisconnected": other_clients,
                            }
                        ),
                    }
                ]
            }

        if name == "dump_config":
            project = arguments["project"]
            project_path = pathlib.Path(project)
            raw_config = await _wm_client.get_project_raw_config(project)
            result = await _wm_client.run_action(
                action_source="fine_envs.DumpConfigAction",
                project=project,
                params={
                    "source_file_path": str(
                        path_to_resource_uri(project_path / "pyproject.toml")
                    ),
                    "project_raw_config": raw_config,
                    "target_file_path": str(
                        path_to_resource_uri(
                            project_path / "finecode_config_dump" / "pyproject.toml"
                        )
                    ),
                },
                options={"resultFormats": ["json"], "trigger": "user", "devEnv": "ai"},
            )
            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        project = arguments.pop("project", None)
        action_source = _tool_name_to_source.get(name, name)
        options = {"resultFormats": ["json"], "trigger": "user", "devEnv": "ai"}
        result = await _run_with_progress(
            action_source, project or "", arguments or {}, options
        )
        return {"content": [{"type": "text", "text": json.dumps(result)}]}


async def _noop(_params: dict | None) -> None:
    pass


def start(workdir: pathlib.Path, port_file: pathlib.Path | None = None) -> None:
    """Start the MCP server on stdio, connecting to the FineCode API.

    If *port_file* is given, a dedicated WM server is started that writes its
    port to that file instead of the shared discovery file.

    The WM connection is established lazily on the first ``tools/list`` call so
    that the MCP client name (from the ``initialize`` handshake) can be included
    in the ``client_id`` sent to the WM server.
    """
    global _wm_port, _workdir
    if port_file is not None:
        wm_lifecycle.start_own_server(workdir, port_file=port_file)
        try:
            _wm_port = asyncio.run(wm_lifecycle.wait_until_ready_from_file(port_file))
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)
    else:
        wm_lifecycle.ensure_running(workdir)
        try:
            _wm_port = asyncio.run(wm_lifecycle.wait_until_ready())
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)
    _workdir = workdir

    async def _run() -> None:
        global _session
        transport = finecode_jsonrpc.ServerStdioTransport(
            readable_id="mcp_server", framing="newline"
        )
        _session = finecode_jsonrpc.JsonRpcServerSession()
        _session.attach(transport)
        _session.on_request("initialize", _handle_initialize)
        _session.on_request("ping", _handle_ping)
        _session.on_request("tools/list", _handle_list_tools)
        _session.on_request("tools/call", _handle_call_tool)
        _session.on_notification("notifications/initialized", _noop)

        await transport.start()
        logger.info("MCP: stdio server ready")
        while not transport._stop_event.is_set():
            await asyncio.sleep(0.05)

        logger.info("MCP: stdio transport stopped")
        if _wm_connected:
            logger.info("MCP: Closing WM client")
            await _wm_client.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
