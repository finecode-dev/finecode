"""Shared tool core for the MCP server.

Ownership: core. Owns WM lifecycle, the action catalog, dispatch, result
merging, token queues and WM callback registration. Imports neither the legacy
front, the assembly nor the CLI; it reaches the front only through the neutral
callback fields it was constructed with.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing
import uuid

from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.schema_utils import FieldSchema, JsonValue
from loguru import logger

from finecode.mcp_server.catalog import BUILTIN_TOOLS
from finecode.mcp_server.contracts import (
    BuiltinToolArguments,
    DescribedPayloadSchema,
    JsonObject,
    ProgressValue,
    Question,
    QuestionOutcome,
    RawConfigPayload,
    RestartPayload,
    ToolData,
    ToolDependencies,
    ToolDescriptor,
    ToolEffects,
    ToolFailure,
    ToolInputSchema,
    ToolOutcome,
)
from finecode.wm_client import ApiClient, ReconnectPolicy
from finecode.wm_server import wm_lifecycle

__all__ = ["ToolService", "prepare_wm"]


def prepare_wm(workdir: pathlib.Path, port_file: pathlib.Path | None) -> int:
    """Start or discover the WM server and wait until it accepts connections.

    Raises:
        TimeoutError: no ready server within the lifecycle wait.
    """
    if port_file is not None:
        wm_lifecycle.start_own_server(workdir, port_file=port_file)
        return asyncio.run(wm_lifecycle.wait_until_ready_from_file(port_file))
    wm_lifecycle.ensure_running(workdir)
    return asyncio.run(wm_lifecycle.wait_until_ready())


class ToolService:
    """WM-backed tool execution shared by MCP fronts."""

    def __init__(
        self,
        *,
        workdir: pathlib.Path,
        port: int,
        client: ApiClient,
        dependencies: ToolDependencies,
        effects: ToolEffects,
    ) -> None:
        self._workdir = workdir
        self._port = port
        self._client = client
        self._dependencies = dependencies
        self._effects = effects
        self._connected = False
        self._tool_name_to_source: dict[str, str] = {}
        self._partial_result_queues: dict[str, asyncio.Queue[JsonValue]] = {}
        self._progress_queues: dict[str, asyncio.Queue[ProgressValue]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def ensure_connected(self) -> None:
        if self._connected:
            return
        config = self._dependencies.connection_config()
        logger.info(
            f"MCP: Connecting to WM server on 127.0.0.1:{self._port}"
            f" as {config.client_id!r}"
        )
        # Registered before connecting: the handlers live on the client object
        # and survive a reconnect, so only the server-side session needs
        # re-establishing.
        self._setup_partial_result_forwarding()
        self._setup_progress_forwarding()
        self._setup_elicitation_forwarding()
        self._client.configure_reconnect(
            ReconnectPolicy(may_start_server=True, workdir=self._workdir),
            on_reattach=self.attach_session,
        )
        try:
            await self._client.connect(
                "127.0.0.1",
                self._port,
                client_id=config.client_id,
                # Declared only when the AI client on the other side of this
                # server can actually be asked; this server has no user of its
                # own.
                capabilities={"elicitation": {"choice": True}}
                if config.can_answer_choices
                else None,
            )
        except BaseException:
            # The socket and reader task outlive a `connect` that failed in
            # `attach_session`, while the flag stays false — so every retried
            # request would open one more and leave it behind.
            await self._client.close()
            raise
        logger.info("MCP: Connected to WM server")
        self._connected = True

    async def attach_session(self, *, first_connect: bool) -> None:
        """Establish everything the WM holds on this client's behalf.

        Called by ``ApiClient`` on first connect and again after every
        reconnect, so the two cannot drift (ADR-0074 rule 3). A restarted WM
        knows nothing about this client: it has not been told the workspace
        directory, and the actions it now exposes may differ from the ones the
        tool list was built from.

        Raises:
            Exception: whatever the WM calls raise. [untranslated]
        """
        if not first_connect:
            # A restarted WM listens on a new port (ADR-0002) and the stored
            # value is what the log lines and any later connect would use.
            self._port = wm_lifecycle.running_port() or self._port
        logger.debug(f"MCP: Add dir to API Client: {self._workdir}")
        await self._client.add_dir(self._workdir, start_runners=False)
        logger.info(f"MCP: Added workspace dir {self._workdir}")
        if not first_connect:
            # The action set may have changed while this client was
            # disconnected.
            await self.invalidate_tools()

    async def list_tools(self) -> list[ToolDescriptor]:
        """Build the MCP tool list from live WM data."""
        logger.info("MCP tools/list called")
        await self.ensure_connected()
        tools: list[ToolDescriptor] = [*BUILTIN_TOOLS]
        actions: list[dict[str, typing.Any]] = []
        try:
            listing = await self._client.list_actions()
            actions = listing.actions
            if listing.unresolved_projects:
                # The tool list itself cannot carry the error; the one-time
                # server/userMessage from auto-prepare already reached the
                # client.
                missing = " ".join(
                    f"{item.get('project')}: {item.get('error')}"
                    for item in listing.unresolved_projects
                )
                logger.warning(
                    f"MCP: {len(listing.unresolved_projects)} project(s) could not be"
                    f" resolved; their actions are missing from the tool list:"
                    f" {missing}"
                )
            logger.info(f"MCP: Fetched {len(actions)} actions from WM")
        except Exception as exc:
            logger.error(f"MCP: Failed to fetch actions from WM: {exc}", exc_info=True)
            actions = []
        # Deduplicate by source: first project that exposes an action owns its
        # schema.
        seen: dict[str, dict[str, typing.Any]] = {}
        for action in actions:
            if action["source"] not in seen:
                seen[action["source"]] = action
        logger.info(f"MCP: After deduplication, {len(seen)} unique actions")
        # Group by project to keep schema requests batched.
        unique_by_project: dict[str, list[dict[str, typing.Any]]] = {}
        for action in seen.values():
            unique_by_project.setdefault(action["project"], []).append(action)
        logger.info(f"MCP: Actions grouped by {len(unique_by_project)} projects")
        self._tool_name_to_source.clear()
        for project_path, project_actions in unique_by_project.items():
            action_sources = [a["source"] for a in project_actions]
            logger.debug(
                f"MCP: Fetching schemas for {len(action_sources)} actions"
                f" in {project_path}"
            )
            schemas: dict[str, typing.Any] = {}
            try:
                schemas = await self._client.get_payload_schemas(
                    project_path, action_sources
                )
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
                self._tool_name_to_source[name] = source
                raw_schema = schemas.get(source)
                described = typing.cast(DescribedPayloadSchema | None, raw_schema)
                description = (
                    described.get("description") if described else None
                ) or f"Run {name} on a project or the whole workspace"
                # A workspace-scoped action is always dispatched once and routed
                # by the WM itself: exposing "project" here would suggest a
                # caller can scope the run by picking a project, which is wrong
                # for these — the action's own payload carries whatever
                # restriction field it defines instead, already included via
                # schema properties below.
                properties: dict[str, typing.Any] = (
                    dict(described["properties"]) if described else {}
                )
                if action.get("scope") != "workspace":
                    properties = {
                        "project": {
                            "type": "string",
                            "description": "Absolute path to the project directory. Use the list_projects tool to see available projects. Omit to run on all projects in the workspace.",
                        },
                        **properties,
                    }
                input_schema = ToolInputSchema(
                    type="object",
                    properties=typing.cast(dict[str, FieldSchema], properties),
                    required=described.get("required", []) if described else [],
                )
                tools.append(
                    ToolDescriptor(
                        name=name,
                        description=description,
                        inputSchema=input_schema,
                    )
                )
        logger.info(f"MCP tools/list returning {len(tools)} tools total")
        return tools

    async def resolve_action_source(self, tool_name: str) -> str:
        """Map an action tool name to the action source the WM addresses it by.

        The mapping is only filled by ``list_tools``, so a caller that reaches
        a tool from a cached list this process never built would otherwise have
        its tool name passed through as a source and silently miss.
        """
        source = self._tool_name_to_source.get(tool_name)
        if source is not None:
            return source
        for action in (await self._client.list_actions()).actions:
            self._tool_name_to_source.setdefault(action["name"], action["source"])
        # An unknown name may already be a source (ADR-0019 alias); let the WM
        # judge.
        return self._tool_name_to_source.get(tool_name, tool_name)

    async def call_tool(self, name: str, arguments: JsonObject) -> ToolOutcome:
        """Dispatch one tool call to the WM server."""
        await self.ensure_connected()
        if name == "list_projects":
            result = await self._client.list_projects()
            return ToolData(typing.cast(JsonValue, {"projects": result}))
        if name == "list_runners":
            result = await self._client.list_runners()
            return ToolData(typing.cast(JsonValue, {"runners": result}))
        if name == "get_resource_usage":
            args = typing.cast(BuiltinToolArguments, arguments)
            result = await self._client.get_resource_usage(
                include_processes=bool(args.get("includeProcesses", False))
            )
            return ToolData(typing.cast(JsonValue, result))
        if name == "list_actions":
            args = typing.cast(BuiltinToolArguments, arguments)
            result = (
                await self._client.list_actions(project=args.get("project"))
            ).actions
            return ToolData(typing.cast(JsonValue, {"actions": result}))
        if name == "get_project_raw_config":
            result = await self._client.get_project_raw_config(
                typing.cast(str, arguments["project"])
            )
            return ToolData(
                typing.cast(
                    JsonValue, typing.cast(RawConfigPayload, {"rawConfig": result})
                )
            )
        if name == "reload_action":
            args = typing.cast(BuiltinToolArguments, arguments)
            action = args.get("action")
            if not action:
                return ToolFailure("Missing 'action' argument")
            result = await self._client.reload_action(
                action_source=await self.resolve_action_source(action),
                project=args.get("project"),
            )
            return ToolData(typing.cast(JsonValue, result))
        if name == "restart_runner":
            args = typing.cast(BuiltinToolArguments, arguments)
            result = await self._client.restart_runner(
                project=args.get("project"),
                all_projects=args.get("allProjects", False),
                env=args.get("env"),
                kill_in_flight_runs=args.get("killInFlightRuns", False),
            )
            return ToolData(typing.cast(JsonValue, result))
        if name == "reload_config":
            args = typing.cast(BuiltinToolArguments, arguments)
            projects = await self._client.reload_config(
                project=args.get("project"),
                all_projects=args.get("allProjects", False),
                rescan=args.get("rescan", False),
                kill_in_flight_runs=args.get("killInFlightRuns", False),
            )
            if any(
                entry.get("actionsAdded") or entry.get("actionsRemoved")
                for entry in projects
            ):
                await self.invalidate_tools()
            return ToolData(typing.cast(JsonValue, {"projects": projects}))
        if name == "restart_wm":
            # Read before the server goes away: this is the disclosure of whose
            # session the caller is about to disturb (PRD-0008 R8). It informs
            # rather than gates — the others reconnect on their own (ADR-0074).
            info = await self._client.get_info()
            own_label = self._dependencies.current_client_label()
            other_clients = [
                label for label in info.get("clients", []) if label != own_label
            ]
            replacement = await wm_lifecycle.replace_running_server(
                self._client, self._workdir
            )
            return ToolData(
                typing.cast(
                    JsonValue,
                    typing.cast(
                        RestartPayload,
                        {
                            "restarted": True,
                            "previousPid": info.get("pid"),
                            "port": replacement["port"],
                            "otherClientsDisconnected": other_clients,
                        },
                    ),
                )
            )
        if name == "dump_config":
            project = typing.cast(str, arguments["project"])
            project_path = pathlib.Path(project)
            raw_config = await self._client.get_project_raw_config(project)
            params: JsonObject = {
                "source_file_path": str(
                    path_to_resource_uri(project_path / "pyproject.toml")
                ),
                "project_raw_config": typing.cast(JsonValue, raw_config),
                "target_file_path": str(
                    path_to_resource_uri(
                        project_path / "finecode_config_dump" / "pyproject.toml"
                    )
                ),
            }
            if not arguments.get("format", True):
                params["format_output"] = False
            result = await self._client.run_action(
                action_source="fine_envs.DumpConfigAction",
                project=project,
                params=typing.cast(dict[str, typing.Any], params),
                options={"resultFormats": ["json"], "trigger": "user", "devEnv": "ai"},
            )
            return ToolData(typing.cast(JsonValue, result))
        args = dict(arguments)
        project_value = typing.cast(str, args.pop("project", None) or "")
        action_source = self._tool_name_to_source.get(name, name)
        options: JsonObject = {
            "resultFormats": ["json"],
            "trigger": "user",
            "devEnv": "ai",
        }
        result = await self._run_with_progress(
            action_source, project_value, args, options
        )
        return ToolData(typing.cast(JsonValue, result))

    async def invalidate_tools(self) -> None:
        """Drop the cached action tools and tell the client its list is stale.

        An action that a recovery added is unreachable until the client asks for
        the tool list again, and it has no reason to ask unless it is told.
        """
        self._tool_name_to_source.clear()
        await self._effects.catalog_changed()

    async def close(self) -> None:
        if self._connected:
            await self._client.close()

    def _setup_partial_result_forwarding(self) -> None:
        """Register the WM partial-result notification handler."""

        async def _on_partial_result(params: dict[str, typing.Any]) -> None:
            token = params.get("token")
            value = params.get("value")
            if token and value is not None:
                queue = self._partial_result_queues.get(token)
                if queue is not None:
                    queue.put_nowait(typing.cast(JsonValue, value))

        self._client.on_notification("actions/partialResult", _on_partial_result)

    def _setup_progress_forwarding(self) -> None:
        """Register the WM progress notification handler."""

        async def _on_progress(params: dict[str, typing.Any]) -> None:
            token = params.get("token")
            value = params.get("value")
            if token and value is not None:
                queue = self._progress_queues.get(token)
                if queue is not None:
                    queue.put_nowait(typing.cast(ProgressValue, value))

        self._client.on_notification("actions/progress", _on_progress)

    def _setup_elicitation_forwarding(self) -> None:
        """Answer the WM's ``client/elicit`` through the injected question effect.

        The WM addresses the question to the connection that started the run;
        this core's whole job here is translating between the two protocols.
        """

        async def _on_elicit(
            params: dict[str, typing.Any] | None,
        ) -> dict[str, typing.Any]:
            value = params or {}
            options = [str(option) for option in value.get("options") or []]
            if not options:
                return {"outcome": "unavailable"}
            raw_timeout = value.get("timeoutSec")
            question = Question(
                message=str(value.get("message", "")),
                options=tuple(options),
                timeout_seconds=float(raw_timeout)
                if isinstance(raw_timeout, (int, float))
                else None,
            )
            try:
                answer = await self._effects.question(question)
            # The front may answer with an error, time out, or be gone; each
            # is the same thing to the run that asked, and none of them may
            # propagate into the WM client's reader loop.
            except Exception as exception:  # noqa: BLE001
                logger.warning(f"MCP: elicitation failed: {exception}")
                return {"outcome": "unavailable"}
            if answer.outcome is not QuestionOutcome.ANSWERED:
                return {"outcome": answer.outcome.value}
            return {"outcome": "answered", "value": answer.value}

        self._client.on_request("client/elicit", _on_elicit)

    async def _run_with_progress(
        self,
        action_source: str,
        project: str,
        params: JsonObject,
        options: JsonObject,
    ) -> JsonObject:
        """Run a WM action with streaming partial results and progress forwarded."""
        token = str(uuid.uuid4())
        progress_token = str(uuid.uuid4())
        queue: asyncio.Queue[JsonValue] = asyncio.Queue()
        progress_queue: asyncio.Queue[ProgressValue] = asyncio.Queue()
        self._partial_result_queues[token] = queue
        self._progress_queues[progress_token] = progress_queue
        # Opt-in: have the WM type-safely merge streamed partials per
        # project/action and return the merged result, so the data returned
        # below is complete even when a project streams several partials.
        options = {**(options or {}), "mergeResults": True}

        async def _forward_partials() -> None:
            try:
                while True:
                    value = await queue.get()
                    await self._effects.partial_result(value)
            except asyncio.CancelledError:
                pass

        async def _forward_progress() -> None:
            try:
                while True:
                    value = await progress_queue.get()
                    await self._effects.progress(value)
            except asyncio.CancelledError:
                pass

        result_task = asyncio.create_task(
            self._client.run_action(
                action_source,
                project,
                typing.cast(dict[str, typing.Any], params),
                typing.cast(dict[str, typing.Any], options),
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
            # concurrently with result_task completion finish enqueuing their
            # values.
            await asyncio.sleep(0)
            forward_task.cancel()
            progress_forward_task.cancel()
            await asyncio.gather(
                forward_task, progress_forward_task, return_exceptions=True
            )
            self._partial_result_queues.pop(token, None)
            self._progress_queues.pop(progress_token, None)
        # Expose the WM's type-safely merged results, flattening
        # {project: {actionSource: {resultByFormat, returnCode}}} to the
        # {project: resultByFormat} shape MCP reports.
        merged: JsonObject = typing.cast(JsonObject, result)
        server_results = (
            merged.pop("results", None) if isinstance(merged, dict) else None
        )
        if server_results:
            results_by_project = {
                project_key: action_results.get(action_source, {}).get(
                    "resultByFormat", {}
                )
                for project_key, action_results in typing.cast(
                    dict[str, typing.Any], server_results
                ).items()
            }
            results_by_project = {k: v for k, v in results_by_project.items() if v}
            if results_by_project:
                merged = {
                    **merged,
                    "resultsByProject": typing.cast(JsonValue, results_by_project),
                }
        return merged
