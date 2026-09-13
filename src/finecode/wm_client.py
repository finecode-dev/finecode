"""FineCode WM client — JSON-RPC client for the FineCode WM server.

Connects to the FineCode WM server over TCP using Content-Length framing.
Supports both request/response and server→client notifications via a
background reader loop.

Used by LSP server, MCP server, and potentially CLI.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import dataclasses
import json
import pathlib
import random

from loguru import logger

from finecode.wm_server import wm_lifecycle
from finecode_extension_runner import schema_utils

CONTENT_LENGTH_HEADER = "Content-Length: "


@dataclasses.dataclass(frozen=True)
class ReconnectPolicy:
    """How a client behaves when the WM connection drops (ADR-0074).

    Whether a client may start a WM is configuration, never inferred: a client
    that owns a dedicated server for one command must not resurrect one it or
    its user deliberately stopped (rule 4).

    The default schedule spends at most about 24s over its attempts, jitter
    included, inside the WM's 30s disconnect timeout (ADR-0004) — past that
    there is no server left to reconnect to, and what happens then is
    ``may_start_server``'s business rather than the loop's.

    Attributes:
        may_start_server: Start a WM when none is listening.  Requires ``workdir``.
        workdir: Directory a started WM is rooted at.
        max_attempts: Attempts before the client reports itself disconnected.
        base_delay: Delay before the first attempt, in seconds.
        max_delay: Ceiling the doubling delay stops at, in seconds.
        jitter: Fraction of each delay to randomize by, spreading the reconnects
            of every client of a restarted WM.
    """

    may_start_server: bool = False
    workdir: pathlib.Path | None = None
    max_attempts: int = 7
    base_delay: float = 0.2
    max_delay: float = 5.0
    jitter: float = 0.5


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
    content_length = int(header_str[len(CONTENT_LENGTH_HEADER) :])

    # Blank separator line
    await reader.readline()

    body = await reader.readexactly(content_length)
    return json.loads(body.decode("utf-8"))


class ApiClient:
    """JSON-RPC client using Content-Length framing over TCP.

    After connect(), a background reader loop dispatches incoming messages by
    their JSON-RPC shape:
    - Requests (``id`` *and* ``method``) go to an :meth:`on_request` callback and
      are answered on this connection.
    - Responses (``id``, no ``method``) resolve the matching pending future.
    - Notifications (``method``, no ``id``) go to an :meth:`on_notification`
      callback.

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
        # Server→client *requests* (ADR-0082), kept apart from notifications
        # because the two are answered differently: a notification handler's
        # return value goes nowhere, a request handler's return value is the
        # JSON-RPC result this client owes the server.
        self._request_handlers: dict[
            str, collections.abc.Callable[..., collections.abc.Coroutine]
        ] = {}
        # Strong references to in-flight inbound-request handlers. They run as
        # tasks rather than inline in the read loop: a handler may wait on a
        # person for minutes, and handling it inline would stall every other
        # message on the connection — including the partial results of the very
        # run that is asking.
        self._inbound_request_tasks: set[asyncio.Task] = set()
        self._reader_task: asyncio.Task | None = None
        self.server_info: dict = {}
        self._host: str = "127.0.0.1"
        self._client_id: str | None = None
        self._reconnect_policy: ReconnectPolicy | None = None
        self._on_reattach: (
            collections.abc.Callable[..., collections.abc.Coroutine] | None
        ) = None
        self._on_session_lost: collections.abc.Callable[[bool], None] | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._capabilities: dict = {}
        # Distinguishes a deliberate close from a lost connection: close()
        # cancels the reader, and a reconnect racing its own shutdown would
        # leave a client nobody asked for.
        self._closing = False
        self._reconnecting = False
        self._connected = asyncio.Event()
        self._epoch = 0

    # -- Connection lifecycle -----------------------------------------------

    def configure_reconnect(
        self,
        policy: ReconnectPolicy | None,
        on_reattach: collections.abc.Callable[..., collections.abc.Coroutine]
        | None = None,
        on_session_lost: collections.abc.Callable[[bool], None] | None = None,
    ) -> None:
        """Reconnect when the connection drops, re-establishing the session first.

        ``policy=None`` keeps the session hook without reconnecting, which is
        what a client holding a dedicated server for one command wants: it must
        not resurrect a server it deliberately stopped (ADR-0074 rule 4).

        ``on_reattach`` is awaited with ``first_connect=True`` by :meth:`connect`
        and with ``first_connect=False`` after each reconnect, so the session
        setup has one definition rather than one per path.  It must re-establish
        whatever the WM held on this client's behalf — a client that cannot
        complete it is disconnected, not connected (ADR-0074 rule 3).

        ``on_session_lost`` is the inverse edge, called synchronously the moment
        the session stops existing: with ``True`` while a reconnect is still
        coming, and with ``False`` once the client has given up.  A surface that
        gates its own work on the session needs both — the first to stop
        dispatching into a WM that has never heard of it, the second to stop
        waiting for a re-attach that is never going to happen.  Not called for a
        deliberate :meth:`close`, which is not a lost session.
        """
        self._reconnect_policy = policy
        self._on_reattach = on_reattach
        self._on_session_lost = on_session_lost

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def connection_epoch(self) -> int:
        """Counts re-attached connections.

        A caller waiting for a *replacement* connection cannot wait on
        connectedness alone: the old connection reads as live until its reader
        notices the peer is gone, so waiting would return immediately on a
        socket that is already dead.
        """
        return self._epoch

    async def wait_connected(
        self, timeout: float, *, after_epoch: int | None = None
    ) -> None:
        """Block until the client holds a re-attached connection.

        With ``after_epoch``, block until a *later* connection than that one is
        established.

        Raises:
            TimeoutError: no such connection within *timeout*.
        """

        async def _wait() -> None:
            while True:
                await self._connected.wait()
                if after_epoch is None or self._epoch > after_epoch:
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait(), timeout=timeout)

    async def connect(
        self,
        host: str,
        port: int,
        client_id: str | None = None,
        capabilities: dict | None = None,
    ) -> None:
        """Connect, identify this client and declare what it can do.

        ``capabilities`` is what the WM is allowed to ask of this connection —
        today only ``{"elicitation": {...}}``, which says a person can be put a
        question (ADR-0082 rule 2). It is a property of the *connection*, not of
        the binary: the same CLI declares it when attached to a terminal and
        withholds it in a pipeline. Kept on the client so every reconnect
        re-declares it, since a restarted WM has never heard of this client.
        """
        self._closing = False
        self._host = host
        self._client_id = client_id
        self._capabilities = capabilities or {}
        await self._open(host, port)
        await self._reattach(first_connect=True)

    async def _open(self, host: str, port: int) -> None:
        """Establish the socket and identify this client to the server."""
        self._reader, self._writer = await asyncio.open_connection(host, port)
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info(f"Connected to FineCode API at {host}:{port}")
        try:
            params: dict = {}
            if self._client_id is not None:
                params["clientId"] = self._client_id
            if self._capabilities:
                params["capabilities"] = self._capabilities
            self.server_info = await self.request("client/initialize", params) or {}
            log_path = self.server_info.get("logFilePath")
            if log_path:
                logger.info(f"WM Server log file: {log_path}")
            else:
                logger.info("WM Server returned no log file path")
        except Exception as exception:
            logger.info(f"Failed to initialize with WM Server: {exception}")

    async def _reattach(self, *, first_connect: bool) -> None:
        """Re-establish the session, then report the client connected.

        Raises:
            Exception: whatever the surface's re-attach hook raises. [untranslated]
        """
        if self._on_reattach is not None:
            await self._on_reattach(first_connect=first_connect)
        self._epoch += 1
        self._connected.set()

    def _notify_session_lost(self, *, recoverable: bool) -> None:
        """Tell the surface its session is gone, without letting it break us.

        Called from a ``finally`` and from the reconnect loop, neither of which
        has anywhere to report a hook that raises: a surface whose bookkeeping
        failed must not also cost the client its reconnect.
        """
        if self._on_session_lost is None:
            return
        try:
            self._on_session_lost(recoverable)
        except Exception:
            logger.exception("WmClient: on_session_lost hook failed")

    async def drop_connection(self) -> None:
        """Close the current transport and let the reconnect path take over.

        Not a shutdown: the client is expected to come back. Used when the server
        on the other end is being replaced — it has to see this connection end
        before it can exit, and this client has to stop talking to it.
        """
        await self._drop_socket()

    async def close(self) -> None:
        self._closing = True
        self._connected.clear()
        # Captured before the reader task is cancelled: its `finally` drops the
        # transport, and this path still wants to await it closing.
        writer = self._writer
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None

        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
        self._writer = None
        self._reader = None

        # Fail any pending requests.
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Connection closed"))
        self._pending.clear()

        # Drop any inbound request still being answered. There is no connection
        # left to answer it on, and a handler waiting on a person would
        # otherwise keep this process alive after the client asked to close.
        for task in list(self._inbound_request_tasks):
            task.cancel()
        self._inbound_request_tasks.clear()

    # -- Notifications ------------------------------------------------------

    def on_notification(
        self,
        method: str,
        callback: collections.abc.Callable[..., collections.abc.Coroutine],
    ) -> None:
        """Register an async callback for a server→client notification."""
        self._notification_handlers[method] = callback

    # -- Inbound requests ----------------------------------------------------

    def on_request(
        self,
        method: str,
        callback: collections.abc.Callable[..., collections.abc.Coroutine],
    ) -> None:
        """Register an async callback for a server→client *request*.

        The callback is awaited with the request's ``params`` and whatever it
        returns becomes the JSON-RPC ``result`` sent back to the server. A
        callback that raises is answered with an internal-error response rather
        than being allowed to take down the reader loop: the server is waiting
        on this id and a dead reader would leave it waiting for its whole
        deadline.
        """
        self._request_handlers[method] = callback

    # -- Server methods -----------------------------------------------------

    async def get_info(self) -> dict:
        """Return static info about the WM Server (e.g. log file path)."""
        return await self.request("server/getInfo")

    async def subscribe_logs(self, min_level: str = "INFO") -> None:
        """Subscribe this connection to WM diagnostic logs (``server/logRecords``)."""
        await self.request("server/subscribeLogs", {"minLevel": min_level})

    async def unsubscribe_logs(self) -> None:
        """Unsubscribe this connection from WM diagnostic logs."""
        await self.request("server/unsubscribeLogs", {})

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
                "workspace/findProjectForFile",
                f"expected dict, got {type(result).__name__}",
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
        self,
        project: str,
        action_sources: list[str],
        *,
        start_runners: bool = False,
    ) -> dict[str, schema_utils.PayloadSchema | None]:
        """Return payload schemas for the given actions in a project.

        Delegates to the WM ``actions/getPayloadSchemas`` endpoint.

        Args:
            project: Absolute path to the project directory.
            action_sources: List of action import-path aliases (ADR-0019).
            start_runners: When true, ask the WM to start the handler
                environments before probing so a schema that needs one is
                available. Defaults to false so passive listing never starts
                environments.

        Returns:
            Mapping of action source → JSON Schema fragment, or ``None``
            for actions whose class could not be imported by the ER.
        """
        params: dict = {"project": project, "actionSources": action_sources}
        if start_runners:
            params["startRunners"] = True
        result = await self.request(
            "actions/getPayloadSchemas",
            params,
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
        self, overrides: dict, service_overrides: dict | None = None
    ) -> None:
        """Set persistent handler and service config overrides on the server.

        Overrides are stored for the lifetime of the server and applied to all
        subsequent action runs.  Call this before ``add_dir`` if possible so that runners
        always start with the correct config and no update push is required.

        overrides format: {action_name: {handler_name_or_"": {param: value}}}
        The empty-string key "" means the override applies to all handlers of
        that action.

        service_overrides format: {service_name: {nested param path}}, keyed by
        the service's addressing ``name`` (not its ``interface``).
        """
        params: dict = {"overrides": overrides}
        if service_overrides:
            params["serviceOverrides"] = service_overrides
        await self.request("workspace/setConfigOverrides", params)

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

    async def reload_action(
        self, action_source: str, project: str | None = None
    ) -> dict:
        """Re-import the packages owning an action and its handlers.

        Only those packages are re-imported — a change elsewhere (a shared
        library, any config) needs a runner restart instead.  ``project``
        omitted reloads the action in every project exposing it.

        Raises:
            ApiServerError: if no project has that action.
        """
        body: dict = {"action": action_source}
        if project is not None:
            body["project"] = project
        return await self.request("actions/reload", body)

    async def add_dir(
        self,
        dir_path: pathlib.Path,
        start_runners: bool = True,
        projects: list[str] | None = None,
        initialize_all_handlers: bool = True,
    ) -> dict:
        """Add a workspace directory. Returns {projects: [...]}.

        When ``start_runners=False`` the server reads configs and collects
        actions without starting any extension runners.  Use this when runner
        environments may not exist yet (e.g. before ``prepare-envs``).

        When ``projects`` is provided, only those projects (by name) will have
        their configs read and runners started — the rest are still discovered
        but not initialised.  Only use this in own-server mode where the server
        lifetime matches a single CLI invocation.

        When ``initialize_all_handlers=False`` runners are started without
        eagerly initializing handlers; handlers are initialized on demand.
        """
        body: dict = {"dirPath": str(dir_path), "startRunners": start_runners}
        if projects is not None:
            body["projects"] = projects
        if not initialize_all_handlers:
            body["initializeAllHandlers"] = False
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

    async def prepare_envs(
        self,
        workdir_path: pathlib.Path,
        recreate: bool = False,
        env_names: list[str] | None = None,
        interpreter_names: list[str] | None = None,
        project_names: list[str] | None = None,
        dev_env: str | None = None,
    ) -> None:
        """Prepare all environments for the workspace.

        Raises:
            ApiServerError: if the server returns an error.
        """
        params: dict = {
            "dirPath": str(workdir_path),
            "recreate": recreate,
        }
        if env_names is not None:
            params["envNames"] = env_names
        if interpreter_names is not None:
            params["interpreters"] = interpreter_names
        if project_names is not None:
            params["projectNames"] = project_names
        if dev_env is not None:
            params["devEnv"] = dev_env
        await self.request("workspace/prepareEnvs", params)

    async def list_runners(self) -> list[dict]:
        """List all extension runners and their status."""
        result = await self.request("runners/list")
        if not isinstance(result, dict) or "runners" not in result:
            raise ApiResponseError(
                "runners/list", f"missing 'runners' field, got {result!r}"
            )
        return result["runners"]

    async def reload_config(
        self,
        project: str | None = None,
        *,
        all_projects: bool = False,
        rescan: bool = False,
        kill_in_flight_runs: bool = False,
    ) -> list[dict]:
        """Make the configuration on disk take effect, reporting one result per
        target project.

        Exactly one of ``project`` and ``all_projects`` must be given (ADR-0078).
        ``rescan`` picks up projects created since the server started.  A project
        that could not be recovered carries ``"status": "failed"``, and one with a
        run in flight is refused rather than recovered unless
        ``kill_in_flight_runs`` accepts killing it.

        Raises:
            ApiServerError: if the target is unstated, doubly stated, or matches
                no project.
        """
        body: dict = {}
        if project is not None:
            body["project"] = project
        if all_projects:
            body["allProjects"] = True
        if rescan:
            body["rescan"] = True
        if kill_in_flight_runs:
            body["killInFlightRuns"] = True
        result = await self.request("workspace/reloadConfig", body)
        if not isinstance(result, dict) or "projects" not in result:
            raise ApiResponseError(
                "workspace/reloadConfig", f"missing 'projects' field, got {result!r}"
            )
        return result["projects"]

    async def restart_runner(
        self,
        project: str | None = None,
        *,
        all_projects: bool = False,
        env: str | None = None,
        debug: bool = False,
        kill_in_flight_runs: bool = False,
    ) -> dict:
        """Restart extension runners, reporting one result per (project, env).

        Exactly one of ``project`` and ``all_projects`` must be given — the
        whole workspace is asked for, never defaulted into (ADR-0078).  ``env``
        omitted restarts every environment of each target project.  A runner
        that did not come back up is reported in ``failed`` rather than as an
        error, and a project with a run in flight is reported in ``refused``
        rather than restarted unless ``kill_in_flight_runs`` accepts killing it.

        Raises:
            ApiServerError: if the target is unstated, doubly stated, or matches
                no runner.
        """
        body: dict = {"debug": debug}
        if project is not None:
            body["project"] = project
        if all_projects:
            body["allProjects"] = True
        if env is not None:
            body["env"] = env
        if kill_in_flight_runs:
            body["killInFlightRuns"] = True
        return await self.request("runners/restart", body)

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
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
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

        from finecode import telemetry

        effective_params = dict(params or {})
        tp = telemetry.get_current_traceparent()
        if tp is not None:
            effective_params["_traceparent"] = tp

        msg = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": effective_params,
        }

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future

        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        self._writer.write(header + body)
        await self._writer.drain()

        response = await future

        if "error" in response:
            error = response["error"]
            raise ApiServerError(error["code"], error["message"])

        return response.get("result")

    # -- Inbound request dispatch -------------------------------------------

    def _send_raw(self, msg: dict) -> None:
        """Frame and write one message. No drain: the reader loop cannot block."""
        if self._writer is None:
            raise RuntimeError("Not connected to FineCode WM server")
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        self._writer.write(header + body)

    def _answer_request(self, req_id: int | str, payload: dict) -> None:
        """Write one response for *req_id*, tolerating a connection that died.

        A handler that took long enough for the connection to go away is the
        ordinary case for a question put to a person, and there is nothing left
        to report the write failure to.
        """
        try:
            self._send_raw({"jsonrpc": "2.0", "id": req_id, **payload})
        except (RuntimeError, ConnectionError, OSError):
            logger.debug(
                f"WmClient: could not answer request {req_id}; connection is gone"
            )

    def _dispatch_inbound_request(self, msg: dict) -> None:
        """Answer a server→client request, out of band of the reader loop."""
        req_id = msg["id"]
        method = msg["method"]
        handler = self._request_handlers.get(method)
        if handler is None:
            logger.warning(f"WmClient: unhandled request {method}")
            self._answer_request(
                req_id,
                {"error": {"code": -32601, "message": f"Method not found: {method}"}},
            )
            return

        async def _run() -> None:
            try:
                result = await handler(msg.get("params"))
            # The handler is a surface's own code — a terminal prompt, an MCP
            # round trip — so the reachable exception set is open. Narrowing this
            # would let an unlisted failure kill the reader loop and leave the
            # server waiting out its deadline on a client that is still running.
            except Exception as exception:
                logger.exception(f"WmClient: request handler for {method} failed")
                self._answer_request(
                    req_id,
                    {"error": {"code": -32603, "message": str(exception)}},
                )
            else:
                self._answer_request(req_id, {"result": result})

        task = asyncio.create_task(_run())
        self._inbound_request_tasks.add(task)
        task.add_done_callback(self._inbound_request_tasks.discard)

    # -- Background reader --------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read messages from the server and dispatch them."""
        try:
            while self._reader is not None:
                msg = await _read_message(self._reader)
                if msg is None:
                    break

                # JSON-RPC 2.0 discrimination, in full: `id` alone does not
                # identify a request. `id` *and* `method` is a request the
                # server is waiting on, `id` without `method` is a response to
                # something this client sent, `method` without `id` is a
                # notification. Before the WM could send requests (ADR-0082)
                # this loop tested `id` alone and read every inbound request as
                # a response for an unknown id — silently, and with the server
                # left waiting.
                if "method" in msg and "id" in msg:
                    self._dispatch_inbound_request(msg)
                elif "id" in msg:
                    # Response to a pending request.
                    future = self._pending.pop(msg["id"], None)
                    if future is None:
                        logger.warning(
                            f"WmClient: received response for unknown id {msg['id']}"
                        )
                    elif future.cancelled():
                        logger.debug(
                            f"WmClient: received late response for cancelled request {msg['id']}, discarding"
                        )
                    elif future.done():
                        logger.warning(
                            f"WmClient: received response for already-resolved id {msg['id']}"
                        )
                    else:
                        future.set_result(msg)
                else:
                    # Server→client notification.
                    method = msg.get("method")
                    handler = self._notification_handlers.get(method)
                    if handler is not None:
                        asyncio.create_task(handler(msg.get("params")))
                    else:
                        logger.trace(f"WmClient: unhandled notification {method}")
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info("WmClient: server connection lost")
        except Exception:
            logger.exception("WmClient: error in reader loop")
        finally:
            # Drop the transport before anything else can observe it.  A
            # non-None writer is exactly what `request()` and
            # `_send_notification()` read as "connected", so leaving it behind
            # would let a call made during the reconnect window pass that guard,
            # write into a closed transport, and then wait forever on a future
            # this loop has already stopped serving and the next connection's
            # reader will never see.
            writer = self._writer
            self._writer = None
            self._reader = None
            if writer is not None:
                writer.close()
            # Fail any remaining pending requests.  Never retried: an action may
            # have formatted files, pushed a tag or published an artifact before
            # the connection died, and the client cannot tell how far it got
            # (ADR-0074 rule 2).
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Connection lost"))
            self._pending.clear()
            # Drop any inbound request still being answered, for the same reason
            # `close()` does. The server resolved its side the moment this
            # connection went away, so a question still on a person's screen can
            # no longer be answered — and if it were, `_answer_request` would
            # write an id the server has discarded into whatever transport the
            # reconnect had put in place by then.
            for task in list(self._inbound_request_tasks):
                task.cancel()
            self._inbound_request_tasks.clear()
            self._connected.clear()
            if not self._closing:
                # Recoverable whenever reconnection is configured at all, not
                # only when *this* loop is the one that starts it: a failed
                # attempt lands here too, with the reconnect loop still running.
                self._notify_session_lost(
                    recoverable=self._reconnect_policy is not None
                )
                if not self._reconnecting and self._reconnect_policy is not None:
                    self._reconnect_task = asyncio.create_task(self._reconnect())

    # -- Reconnection (ADR-0074) --------------------------------------------

    async def _reconnect(self) -> None:
        """Re-establish the connection with bounded backoff, then re-attach."""
        policy = self._reconnect_policy
        if policy is None:
            return
        delay = policy.base_delay
        # A failed attempt closes a socket whose reader loop then reaches the
        # same `finally` that started this one; without the flag each failure
        # would leave one more loop behind it.
        self._reconnecting = True
        try:
            await self._reconnect_attempts(policy, delay)
        finally:
            self._reconnecting = False

    async def _reconnect_attempts(self, policy: ReconnectPolicy, delay: float) -> None:
        for attempt in range(1, policy.max_attempts + 1):
            await asyncio.sleep(delay * (1 + random.uniform(0, policy.jitter)))
            if self._closing:
                return

            port = await self._discover_port(policy)
            if port is not None:
                try:
                    await self._open(self._host, port)
                    await self._reattach(first_connect=False)
                except Exception as exception:
                    # Including a re-attach that failed: a client whose session
                    # was not restored is talking to a server that has never
                    # heard of it, so it counts as disconnected (rule 3).
                    logger.warning(
                        f"WmClient: reconnect attempt {attempt} failed: {exception}"
                    )
                    await self._drop_socket()
                else:
                    logger.info(
                        f"WmClient: reconnected to FineCode API on port {port} "
                        f"after {attempt} attempt(s). Requests that were in flight "
                        f"when the connection dropped were not retried."
                    )
                    return

            delay = min(delay * 2, policy.max_delay)

        logger.error(
            f"WmClient: gave up reconnecting to the FineCode WM server after "
            f"{policy.max_attempts} attempts. The client is disconnected."
        )
        # No further attempt is coming, so a surface still holding its work back
        # for the re-attach would hold it forever.
        self._notify_session_lost(recoverable=False)

    async def _discover_port(self, policy: ReconnectPolicy) -> int | None:
        """Re-read the discovery file: a restarted WM listens on a new port
        (ADR-0002), so the address this client last used is not reusable."""
        # Blocking: `running_port` probes the port with a synchronous connect
        # that takes its full 1s timeout when the port is filtered rather than
        # refused, and this runs on the surface's own event loop.
        port = await asyncio.to_thread(wm_lifecycle.running_port)
        if port is not None:
            return port
        if not policy.may_start_server or policy.workdir is None:
            return None
        # Blocking: spawns the server and waits for it to be observable.
        await asyncio.to_thread(wm_lifecycle.ensure_running, policy.workdir)
        return await asyncio.to_thread(wm_lifecycle.running_port)

    async def _drop_socket(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            # Awaited so its `finally` runs now: it must not schedule a second
            # reconnect after this one has finished.
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._reader = None
