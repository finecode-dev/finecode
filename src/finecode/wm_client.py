"""FineCode WM client — JSON-RPC client for the FineCode WM server.

Connects to the FineCode WM server over TCP using Content-Length framing.
Supports both request/response and server→client notifications via a
background reader loop.

Used by LSP server, MCP server, and potentially CLI.
"""

from __future__ import annotations

import asyncio
import collections.abc
import json
import pathlib

from loguru import logger

CONTENT_LENGTH_HEADER = "Content-Length: "


class ApiError(Exception):
    """Base class for API client errors."""


class ApiServerError(ApiError):
    """Server returned a JSON-RPC error response."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(f"API error ({code}): {message}")


class ApiResponseError(ApiError):
    """Server returned an unexpected or malformed response."""

    def __init__(self, method: str, detail: str) -> None:
        self.method = method
        super().__init__(f"Unexpected response for '{method}': {detail}")


async def _read_message(reader: asyncio.StreamReader) -> dict | None:
    """Read one Content-Length framed JSON-RPC message. Returns None on EOF."""
    header_line = await reader.readline()
    if not header_line:
        return None
    header_str = header_line.decode("utf-8").strip()
    if not header_str.startswith(CONTENT_LENGTH_HEADER):
        logger.warning(f"WmClient: unexpected header: {header_str!r}")
        return None
    content_length = int(header_str[len(CONTENT_LENGTH_HEADER):])

    # Blank separator line
    await reader.readline()

    body = await reader.readexactly(content_length)
    return json.loads(body.decode("utf-8"))


class ApiClient:
    """JSON-RPC client using Content-Length framing over TCP.

    After connect(), a background reader loop dispatches incoming messages:
    - Responses (with ``id``) resolve the matching pending request future.
    - Notifications (without ``id``) are dispatched to registered callbacks.

    Errors:
    - ``ApiServerError``: the server returned a JSON-RPC error.
    - ``ApiResponseError``: the server response was missing an expected field.
    - ``ConnectionError``: the connection was lost.
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notification_handlers: dict[
            str, collections.abc.Callable[..., collections.abc.Coroutine]
        ] = {}
        self._reader_task: asyncio.Task | None = None
        self.server_info: dict = {}

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, host: str, port: int, client_id: str | None = None) -> None:
        self._reader, self._writer = await asyncio.open_connection(host, port)
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info(f"Connected to FineCode API at {host}:{port}")
        try:
            params: dict = {}
            if client_id is not None:
                params["clientId"] = client_id
            self.server_info = await self.request("client/initialize", params) or {}
            log_path = self.server_info.get("logFilePath")
            if log_path:
                logger.info(f"WM Server log file: {log_path}")
            else:
                logger.info("WM Server returned no log file path")
        except Exception as exception:
            logger.info(f"Failed to initialize with WM Server: {exception}")

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None

        # Fail any pending requests.
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Connection closed"))
        self._pending.clear()

    # -- Notifications ------------------------------------------------------

    def on_notification(
        self,
        method: str,
        callback: collections.abc.Callable[..., collections.abc.Coroutine],
    ) -> None:
        """Register an async callback for a server→client notification."""
        self._notification_handlers[method] = callback

    # -- Server methods -----------------------------------------------------

    async def get_info(self) -> dict:
        """Return static info about the WM Server (e.g. log file path)."""
        return await self.request("server/getInfo")

    # -- Workspace methods --------------------------------------------------

    async def list_projects(self) -> list[dict]:
        """List all projects in the workspace."""
        return await self.request("workspace/listProjects")

    async def find_project_for_file(self, file_path: str) -> str | None:
        """Return the absolute directory path of the project containing a given file.

        An empty string or null result indicates that the file does not belong to
        any project.  This mirrors the server's
        ``workspace/findProjectForFile`` handler.
        """
        result = await self.request(
            "workspace/findProjectForFile", {"filePath": file_path}
        )
        # server returns {"project": name | None}
        if not isinstance(result, dict):
            raise ApiResponseError(
                "workspace/findProjectForFile", f"expected dict, got {type(result).__name__}"
            )
        return result.get("project")

    async def get_workspace_editable_packages(self) -> dict[str, str]:
        """Return workspace editable packages as name → absolute posix path."""
        result = await self.request("workspace/getWorkspaceEditablePackages")
        if not isinstance(result, dict) or "packages" not in result:
            raise ApiResponseError(
                "workspace/getWorkspaceEditablePackages",
                f"missing 'packages' field, got {result!r}",
            )
        return result["packages"]

    async def get_project_raw_config(self, project: str) -> dict:
        """Return the resolved raw config for a project by name."""
        result = await self.request(
            "workspace/getProjectRawConfig", {"project": project}
        )
        if not isinstance(result, dict) or "rawConfig" not in result:
            raise ApiResponseError(
                "workspace/getProjectRawConfig",
                f"missing 'rawConfig' field, got {result!r}",
            )
        return result["rawConfig"]

    async def list_actions(self, project: str | None = None) -> list[dict]:
        """List available actions, optionally filtered by project name."""
        params: dict = {}
        if project is not None:
            params["project"] = project
        result = await self.request("actions/list", params)
        if not isinstance(result, dict) or "actions" not in result:
            raise ApiResponseError(
                "actions/list", f"missing 'actions' field, got {result!r}"
            )
        return result["actions"]

    async def get_payload_schemas(
        self, project: str, action_sources: list[str]
    ) -> dict[str, dict | None]:
        """Return payload schemas for the given actions in a project.

        Delegates to the WM ``actions/getPayloadSchemas`` endpoint.

        Args:
            project: Absolute path to the project directory.
            action_sources: List of action import-path aliases (ADR-0019).

        Returns:
            Mapping of action source → JSON Schema fragment, or ``None``
            for actions whose class could not be imported by the ER.
        """
        result = await self.request(
            "actions/getPayloadSchemas",
            {"project": project, "actionSources": action_sources},
        )
        if not isinstance(result, dict) or "schemas" not in result:
            raise ApiResponseError(
                "actions/getPayloadSchemas",
                f"missing 'schemas' field, got {result!r}",
            )
        return result["schemas"]

    async def get_tree(self, parent_node_id: str | None = None) -> dict:
        """Retrieve the hierarchical action tree from the WM server.

        ``parent_node_id`` is currently ignored by the server but is accepted for
        future compatibility (and mirrors the arguments passed by the IDE
        command).
        The returned value is the raw dictionary returned by the server, which
        at the moment has the shape ``{"nodes": [...]} ``.
        """
        params: dict = {}
        if parent_node_id is not None:
            params["parent_node_id"] = parent_node_id
        result = await self.request("actions/getTree", params)
        return result

    async def set_config_overrides(
        self, overrides: dict
    ) -> None:
        """Set persistent handler config overrides on the server.

        Overrides are stored for the lifetime of the server and applied to all
        subsequent action runs.  Call this before ``add_dir`` if possible so that runners
        always start with the correct config and no update push is required.

        overrides format: {action_name: {handler_name_or_"": {param: value}}}
        The empty-string key "" means the override applies to all handlers of
        that action.
        """
        await self.request("workspace/setConfigOverrides", {"overrides": overrides})

    async def run_batch(
        self,
        action_sources: list[str],
        projects: list[str] | None = None,
        params: dict | None = None,
        params_by_project: dict[str, dict] | None = None,
        options: dict | None = None,
        progress_token: str | None = None,
        partial_result_token: str | int | None = None,
    ) -> dict:
        """Run multiple actions across multiple (or all) projects.

        Results are keyed by project path string, then action source.
        All result keys use camelCase (returnCode, resultByFormat).
        If ``progress_token`` is provided, progress notifications are delivered
        as ``actions/progress`` notifications before this coroutine returns.
        If ``partial_result_token`` is provided, one ``actions/partialResult``
        notification is emitted per completed project in completion order.
        """
        body: dict = {"actionSources": action_sources}
        if projects is not None:
            body["projects"] = projects
        if params:
            body["params"] = params
        if params_by_project:
            body["paramsByProject"] = params_by_project
        if options:
            body["options"] = options
        if progress_token is not None:
            body["progressToken"] = progress_token
        if partial_result_token is not None:
            body["partialResultToken"] = partial_result_token
        return await self.request("actions/runBatch", body)

    async def run_action(
        self,
        action_source: str,
        project: str,
        params: dict | None = None,
        options: dict | None = None,
        progress_token: str | None = None,
        partial_result_token: str | int | None = None,
    ) -> dict:
        """Run an action on a project.

        ``action_source`` is an import-path alias identifying the action (ADR-0019).
        If ``progress_token`` is provided, progress notifications are delivered
        as ``actions/progress`` notifications before this coroutine returns.
        If ``partial_result_token`` is provided, partial results are streamed as
        ``actions/partialResult`` notifications (``progress_token`` may also be
        set simultaneously).
        Pass ``project=""`` to run across all projects that expose the action.
        """
        body: dict = {
            "actionSource": action_source,
            "project": project,
            "options": options,
        }
        if params:
            body["params"] = params
        if progress_token is not None:
            body["progressToken"] = progress_token
        if partial_result_token is not None:
            body["partialResultToken"] = partial_result_token
        return await self.request("actions/run", body)

    async def add_dir(
        self,
        dir_path: pathlib.Path,
        start_runners: bool = True,
        projects: list[str] | None = None,
    ) -> dict:
        """Add a workspace directory. Returns {projects: [...]}.

        When ``start_runners=False`` the server reads configs and collects
        actions without starting any extension runners.  Use this when runner
        environments may not exist yet (e.g. before ``prepare-envs``).

        When ``projects`` is provided, only those projects (by name) will have
        their configs read and runners started — the rest are still discovered
        but not initialised.  Only use this in own-server mode where the server
        lifetime matches a single CLI invocation.
        """
        body: dict = {"dirPath": str(dir_path), "startRunners": start_runners}
        if projects is not None:
            body["projects"] = projects
        return await self.request("workspace/addDir", body)

    async def start_runners(
        self,
        projects: list[str] | None = None,
        python_overrides: dict[str, str] | None = None,
        resolve_presets: bool = True,
    ) -> None:
        """Start extension runners for all (or specified) projects.

        Complements any already-running runners — only missing runners are
        started.  Also resolves presets so ``project.actions`` is up to date.

        ``python_overrides`` maps env_name to an absolute Python executable path,
        overriding the venv-resolved Python for that env.  Used by bootstrap to
        start the dev_workspace runner with the invoking Python (sys.executable)
        before the venv exists.
        """
        params: dict = {}
        if projects is not None:
            params["projects"] = projects
        if python_overrides is not None:
            params["pythonOverrides"] = python_overrides
        if not resolve_presets:
            params["resolvePresets"] = False
        await self.request("workspace/startRunners", params)

    async def list_runners(self) -> list[dict]:
        """List all extension runners and their status."""
        result = await self.request("runners/list")
        if not isinstance(result, dict) or "runners" not in result:
            raise ApiResponseError(
                "runners/list", f"missing 'runners' field, got {result!r}"
            )
        return result["runners"]

    async def check_env(self, project: str, env_name: str) -> bool:
        """Return whether the named environment is valid for a project."""
        result = await self.request(
            "runners/checkEnv", {"project": project, "envName": env_name}
        )
        if not isinstance(result, dict) or "valid" not in result:
            raise ApiResponseError(
                "runners/checkEnv", f"missing 'valid' field, got {result!r}"
            )
        return result["valid"]

    async def remove_env(self, project: str, env_name: str) -> None:
        """Remove the named environment for a project."""
        await self.request(
            "runners/removeEnv", {"project": project, "envName": env_name}
        )

    async def remove_dir(self, dir_path: pathlib.Path) -> None:
        """Remove a workspace directory."""
        await self.request("workspace/removeDir", {"dirPath": str(dir_path)})

    # -- Document notifications -------------------------------------------------

    async def notify_document_opened(
        self, uri: str, version: int | str | None = None, text: str = ""
    ) -> None:
        """Send document opened notification to the server."""
        params = {"uri": uri, "text": text}
        if version is not None:
            params["version"] = version

        self._send_notification("documents/opened", params)

    async def notify_document_closed(self, uri: str) -> None:
        """Send document closed notification to the server."""
        self._send_notification("documents/closed", {"uri": uri})

    async def notify_document_changed(
        self, uri: str, version: int | str, content_changes: list[dict]
    ) -> None:
        """Send document changed notification to the server."""
        params = {
            "uri": uri,
            "version": version,
            "contentChanges": content_changes,
        }
        self._send_notification("documents/changed", params)

    # -- Low-level notification -------------------------------------------------

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._writer is None:
            raise RuntimeError("Not connected to FineCode WM server")

        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }

        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self._writer.write(header + body)
        # Don't await drain for notifications, fire and forget


    # -- Low-level request --------------------------------------------------

    async def request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and wait for the response.

        Raises:
            ApiServerError: the server returned a JSON-RPC error.
            ConnectionError: the connection was closed before a response arrived.
        """
        if self._writer is None:
            raise RuntimeError("Not connected to FineCode WM server")

        self._request_id += 1
        rid = self._request_id
        msg = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        }

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future

        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self._writer.write(header + body)
        await self._writer.drain()

        response = await future

        if "error" in response:
            error = response["error"]
            raise ApiServerError(error["code"], error["message"])

        return response.get("result")

    # -- Background reader --------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read messages from the server and dispatch them."""
        try:
            while self._reader is not None:
                msg = await _read_message(self._reader)
                if msg is None:
                    break

                if "id" in msg:
                    # Response to a pending request.
                    future = self._pending.pop(msg["id"], None)
                    if future is not None and not future.done():
                        future.set_result(msg)
                    else:
                        logger.warning(
                            f"WmClient: received response for unknown id {msg['id']}"
                        )
                else:
                    # Server→client notification.
                    method = msg.get("method")
                    handler = self._notification_handlers.get(method)
                    if handler is not None:
                        asyncio.create_task(handler(msg.get("params")))
                    else:
                        logger.trace(
                            f"WmClient: unhandled notification {method}"
                        )
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info("WmClient: server connection lost")
        except Exception:
            logger.exception("WmClient: error in reader loop")
        finally:
            # Fail any remaining pending requests.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Connection lost"))
            self._pending.clear()
