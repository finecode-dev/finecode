"""Workspace Manager LSP server.
"""
from __future__ import annotations

import asyncio
import typing
from pathlib import Path

from loguru import logger
from lsprotocol import converters as lsp_converters
from lsprotocol import types

import finecode_jsonrpc as finecode_jsonrpc_module
from finecode.wm_server import wm_lifecycle
from finecode.wm_client import ApiClient
from finecode.lsp_server import global_state
from finecode.lsp_server.endpoints import action_tree as action_tree_endpoints
from finecode.lsp_server.endpoints import code_actions as code_actions_endpoints
from finecode.lsp_server.endpoints import code_lens as code_lens_endpoints
from finecode.lsp_server.endpoints import diagnostics as diagnostics_endpoints
from finecode.lsp_server.endpoints import document_sync as document_sync_endpoints
from finecode.lsp_server.endpoints import formatting as formatting_endpoints
from finecode.lsp_server.endpoints import inlay_hints as inlay_hints_endpoints

_lsp_converter = lsp_converters.get_converter()

# ---------------------------------------------------------------------------
# LspServer
# ---------------------------------------------------------------------------

_EXECUTE_COMMANDS = [
    "finecode.getActions",
    "finecode.getActionsForPosition",
    "finecode.listProjects",
    "finecode.runBatch",
    "finecode.runAction",
    "finecode.runActionOnFile",
    "finecode.runActionOnProject",
    "finecode.reloadAction",
    "finecode.reset",
    "finecode.restartExtensionRunner",
    "finecode.restartAndDebugExtensionRunner",
]


class LspServer:
    """Workspace Manager LSP server.

    Backed by :class:`~finecode_jsonrpc.JsonRpcServerSession` and one of the
    server transports from :mod:`finecode_jsonrpc.server_transport`.
    """

    def __init__(self) -> None:
        self._session = finecode_jsonrpc_module.JsonRpcServerSession()
        self._workspace_folders: list[dict] = []  # [{uri, name}, ...]
        self._tcp_server: asyncio.Server | None = None

    # ------------------------------------------------------------------
    # Server → client helpers
    # ------------------------------------------------------------------

    def send_notification_sync(self, method: str, params: dict) -> None:
        """Send a notification to the IDE client (fire-and-forget, thread-safe)."""
        self._session._transport.send(  # type: ignore[union-attr]
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    async def send_request_to_client(
        self, method: str, params: dict
    ) -> typing.Any:
        """Send a request to the IDE client and return the result."""
        return await self._session.send_request(method, params)

    def log_message(self, message: str, msg_type: int) -> None:
        """Send ``window/logMessage`` notification."""
        self.send_notification_sync(
            "window/logMessage", {"type": msg_type, "message": message}
        )

    def show_message(self, message: str, msg_type: int) -> None:
        """Send ``window/showMessage`` notification."""
        self.send_notification_sync(
            "window/showMessage", {"type": msg_type, "message": message}
        )

    def notify_client(self, method: str, params: dict) -> None:
        """Send an arbitrary notification to the IDE client."""
        self.send_notification_sync(method, params)

    def send_progress(self, token: int | str, value: dict) -> None:
        """Send ``$/progress`` notification."""
        self.send_notification_sync("$/progress", {"token": token, "value": value})

    def shutdown(self) -> None:
        if self._tcp_server is not None:
            self._tcp_server.close()

    # ------------------------------------------------------------------
    # Start methods
    # ------------------------------------------------------------------

    async def start_io_async(self) -> None:
        """Start the server on stdin/stdout."""
        logger.info("Starting LSP server on stdio")
        transport = finecode_jsonrpc_module.ServerStdioTransport(
            readable_id="lsp_server"
        )
        self._session.attach(transport)
        await transport.start()
        while not transport._stop_event.is_set():
            await asyncio.sleep(0.05)
        logger.debug("LSP server stdio loop finished")

    async def start_tcp_async(self, host: str, port: int) -> None:
        """Start the server on a TCP port."""
        logger.info("Starting LSP server on TCP %s:%s", host, port)

        async def _handle_connection(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            logger.debug("TCP client connected")
            transport = finecode_jsonrpc_module.TcpServerTransport(
                reader, writer, readable_id="lsp_server_tcp"
            )
            self._session.attach(transport)
            await transport.start()
            while not transport._stop_event.is_set():
                await asyncio.sleep(0.05)
            self.shutdown()
            writer.close()
            if self._tcp_server is not None:
                self._tcp_server.close()

        self._tcp_server = await asyncio.start_server(_handle_connection, host, port)
        addrs = ", ".join(
            str(sock.getsockname()) for sock in self._tcp_server.sockets
        )
        logger.info(f"Serving on {addrs}")
        try:
            async with self._tcp_server:
                await self._tcp_server.serve_forever()
        except asyncio.CancelledError:
            logger.debug("TCP server closed")


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_lsp_server() -> LspServer:
    """Create and wire the LSP server with all handlers registered."""
    server = LspServer()
    session = server._session

    def _wrap(handler):
        """Wrap a handler that takes (server, params) for use with the session."""
        async def _wrapped(params: dict | None) -> typing.Any:
            return await handler(server, params)
        return _wrapped

    # LSP lifecycle
    session.on_request("initialize", _wrap(_on_initialize))
    session.on_request("shutdown", _wrap(_on_shutdown))
    session.on_notification("initialized", _wrap(_on_initialized))
    session.on_notification("exit", _wrap(_on_exit))

    # Workspace
    session.on_notification(
        "workspace/didChangeWorkspaceFolders",
        _wrap(_workspace_did_change_workspace_folders),
    )
    session.on_request("workspace/executeCommand", _wrap(_on_execute_command))
    session.on_request("server/shutdown", _wrap(_lsp_server_shutdown))

    # Text document sync
    session.on_notification("textDocument/didOpen", _wrap(_document_did_open))
    session.on_notification("textDocument/didClose", _wrap(_document_did_close))
    session.on_notification("textDocument/didSave", _wrap(_document_did_save))
    session.on_notification("textDocument/didChange", _wrap(_document_did_change))

    # Formatting
    session.on_request("textDocument/formatting", _wrap(_on_formatting))
    session.on_request("textDocument/rangeFormatting", _wrap(_on_range_formatting))
    session.on_request("textDocument/rangesFormatting", _wrap(_on_ranges_formatting))

    # Code actions
    session.on_request("textDocument/codeAction", _wrap(_on_code_action))
    session.on_request("codeAction/resolve", _wrap(_on_code_action_resolve))

    # Code lens
    session.on_request("textDocument/codeLens", _wrap(_on_code_lens))
    session.on_request("codeLens/resolve", _wrap(_on_code_lens_resolve))

    # Diagnostics
    session.on_request("textDocument/diagnostic", _wrap(_on_document_diagnostic))
    session.on_request("workspace/diagnostic", _wrap(_on_workspace_diagnostic))

    # Inlay hints
    session.on_request("textDocument/inlayHint", _wrap(_on_inlay_hint))
    session.on_request("inlayHint/resolve", _wrap(_on_inlay_hint_resolve))

    return server


# ---------------------------------------------------------------------------
# LSP lifecycle handlers
# ---------------------------------------------------------------------------


async def _on_initialize(server: LspServer, params: dict | None) -> dict:
    logger.info("initialize")
    if params:
        wf = params.get("workspaceFolders") or []
        server._workspace_folders = [
            {"uri": f["uri"], "name": f["name"]} for f in wf
        ]
    return {
        "capabilities": {
            "textDocumentSync": {
                "openClose": True,
                "change": 2,  # Incremental
                "save": True,
            },
            "documentFormattingProvider": True,
            "documentRangeFormattingProvider": True,
            "documentRangesFormattingProvider": True,
            "codeActionProvider": True,
            "codeLensProvider": {"resolveProvider": True},
            "diagnosticProvider": {
                "interFileDependencies": False,
                "workspaceDiagnostics": True,
            },
            "inlayHintProvider": {"resolveProvider": True},
            "executeCommandProvider": {"commands": _EXECUTE_COMMANDS},
            "workspace": {
                "workspaceFolders": {
                    "supported": True,
                    "changeNotifications": True,
                }
            },
        },
        "serverInfo": {
            "name": "FineCode_Workspace_Manager_Server",
            "version": "v1",
        },
    }


async def _on_initialized(server: LspServer, _params: dict | None) -> None:
    logger.info("initialized, adding workspace directories")

    workdir = Path.cwd()
    if server._workspace_folders:
        workdir = Path(
            server._workspace_folders[0]["uri"].replace("file://", "")
        )

    wm_lifecycle.ensure_running(workdir, log_level=global_state.wm_log_level)
    try:
        port = await wm_lifecycle.wait_until_ready()
    except TimeoutError as exc:
        logger.warning(f"FineCode WM server did not start: {exc}")
        port = None

    if port is None:
        logger.error("Cannot connect to FineCode WM server — no port available")
        return

    try:
        global_state.wm_client = ApiClient()
        await global_state.wm_client.connect("127.0.0.1", port, client_id="lsp")
    except (ConnectionRefusedError, OSError) as exc:
        logger.error(f"Could not connect to FineCode WM server: {exc}")
        global_state.wm_client = None
        return

    if global_state.lsp_log_file_path:
        server.log_message(
            f"FineCode LSP Server log: {global_state.lsp_log_file_path}",
            types.MessageType.Info.value,
        )

    log_path = global_state.wm_client.server_info.get("logFilePath")
    if log_path:
        server.log_message(
            f"FineCode WM Server log: {log_path}",
            types.MessageType.Info.value,
        )

    # Register notification handlers for server→client push messages.
    async def on_tree_changed(push_params: dict) -> None:
        node = push_params.get("node")
        if isinstance(node, dict):
            server.notify_client("actionsNodes/changed", node)

    async def on_user_message(push_params: dict) -> None:
        await send_user_message_notification(
            server, push_params["message"], push_params["type"]
        )

    global_state.wm_client.on_notification("actions/treeChanged", on_tree_changed)
    global_state.wm_client.on_notification("server/userMessage", on_user_message)

    # Forward progress notifications to the IDE progress reporter.
    from finecode_extension_api.actions.code_quality import lint_action
    from pydantic.dataclasses import dataclass as pydantic_dataclass
    from finecode.lsp_server import pygls_types_utils
    from finecode.lsp_server.endpoints.diagnostics import map_lint_message_to_diagnostic

    def _map_lint_to_document_diagnostic_partial(
        lint_result: lint_action.LintRunResult,
    ) -> dict:
        related_documents = {}
        for file_path_str, lint_messages in lint_result.messages.items():
            file_report = types.FullDocumentDiagnosticReport(
                items=[map_lint_message_to_diagnostic(m) for m in lint_messages]
            )
            uri = pygls_types_utils.path_to_uri_str(Path(file_path_str))
            related_documents[uri] = file_report
        partial = types.DocumentDiagnosticReportPartialResult(
            related_documents=related_documents
        )
        return _lsp_converter.unstructure(partial)

    def _map_lint_to_workspace_diagnostic_partial(
        lint_result: lint_action.LintRunResult,
    ) -> dict:
        items = [
            types.WorkspaceFullDocumentDiagnosticReport(
                uri=pygls_types_utils.path_to_uri_str(Path(file_path_str)),
                items=[map_lint_message_to_diagnostic(m) for m in lint_messages],
            )
            for file_path_str, lint_messages in lint_result.messages.items()
        ]
        partial = types.WorkspaceDiagnosticReportPartialResult(items=items)
        return _lsp_converter.unstructure(partial)

    async def on_partial_result(push_params: dict) -> None:
        token = push_params.get("token")
        value = push_params.get("value")
        if token is None or value is None:
            logger.error("Invalid partial result notification: missing token or value")
            return

        action, endpoint_type = global_state.partial_result_tokens.get(
            token, (None, None)
        )
        if not action or not endpoint_type:
            logger.error(f"No mapping found for partial result token {token}")
            return

        if action == "finecode_extension_api.actions.LintAction":
            result_by_format = value.get("resultByFormat") or {}
            json_result = result_by_format.get("json")
            if json_result is None:
                logger.error(f"No json result in partial result for token {token}")
                return
            result_type = pydantic_dataclass(lint_action.LintRunResult)
            lint_result: lint_action.LintRunResult = result_type(**json_result)

            if endpoint_type == "document_diagnostic":
                partial_dict = _map_lint_to_document_diagnostic_partial(lint_result)
            elif endpoint_type == "workspace_diagnostic":
                partial_dict = _map_lint_to_workspace_diagnostic_partial(lint_result)
            else:
                logger.error(
                    f"Unknown endpoint_type {endpoint_type} for action {action}"
                )
                return
            server.send_progress(token, partial_dict)
        else:
            logger.warning(f"Unsupported action for partial results: {action}")

    global_state.wm_client.on_notification("actions/partialResult", on_partial_result)

    async def on_progress_notification(push_params: dict) -> None:
        token = push_params.get("token")
        value = push_params.get("value")
        if token is None or value is None:
            logger.error("Invalid progress notification: missing token or value")
            return

        progress_type = value.get("type")
        if progress_type == "begin":
            lsp_value = types.WorkDoneProgressBegin(
                title=value.get("title", ""),
                message=value.get("message"),
                percentage=value.get("percentage"),
                cancellable=value.get("cancellable", False),
            )
        elif progress_type == "report":
            lsp_value = types.WorkDoneProgressReport(
                message=value.get("message"),
                percentage=value.get("percentage"),
            )
        elif progress_type == "end":
            lsp_value = types.WorkDoneProgressEnd(message=value.get("message"))
        else:
            logger.error(f"Unknown progress type: {progress_type}")
            return

        server.send_progress(token, _lsp_converter.unstructure(lsp_value))

    global_state.wm_client.on_notification(
        "actions/progress", on_progress_notification
    )

    # Add workspace directories via the WM server.
    try:
        async with asyncio.TaskGroup() as tg:
            for folder in server._workspace_folders:
                dir_path = Path(folder["uri"].replace("file://", ""))
                tg.create_task(global_state.wm_client.add_dir(dir_path))
    except ExceptionGroup as error:
        logger.exception(error)

    global_state.server_initialized.set()
    logger.trace("Workspace directories added, end of initialized handler")


async def _on_shutdown(_server: LspServer, _params: dict | None) -> None:
    logger.info("on shutdown handler")
    if global_state.wm_client is not None:
        asyncio.ensure_future(global_state.wm_client.close())
        global_state.wm_client = None


async def _on_exit(_server: LspServer, _params: dict | None) -> None:
    logger.info("exit handler")


async def _workspace_did_change_workspace_folders(
    _server: LspServer, params: dict | None
) -> None:
    if params is None:
        return
    typed = _lsp_converter.structure(params, types.DidChangeWorkspaceFoldersParams)
    logger.trace(f"Workspace dirs were changed: {typed}")
    if global_state.wm_client is None:
        logger.warning("WM client not connected, ignoring workspace folder change")
        return

    for ws_folder in typed.event.removed:
        await global_state.wm_client.remove_dir(
            Path(ws_folder.uri.removeprefix("file://"))
        )
    for ws_folder in typed.event.added:
        await global_state.wm_client.add_dir(
            Path(ws_folder.uri.removeprefix("file://"))
        )


async def _lsp_server_shutdown(_server: LspServer, _params: dict | None) -> dict:
    """Handle ``server/shutdown`` — explicitly stop the WM server."""
    logger.info("server/shutdown request received, stopping WM server")
    if global_state.wm_client is not None:
        try:
            await global_state.wm_client.request("server/shutdown", {})
        except Exception:
            logger.warning("WM server did not respond to shutdown request")
        await global_state.wm_client.close()
        global_state.wm_client = None
    return {}


# ---------------------------------------------------------------------------
# workspace/executeCommand dispatcher
# ---------------------------------------------------------------------------


async def _on_execute_command(server: LspServer, params: dict | None) -> typing.Any:
    if params is None:
        return None
    command = params.get("command", "")
    arguments: list = params.get("arguments") or []

    logger.info(f"execute command: {command}")
    await global_state.server_initialized.wait()

    if command == "finecode.getActions":
        return await action_tree_endpoints.list_actions(server, *arguments)
    elif command == "finecode.getActionsForPosition":
        return await action_tree_endpoints.list_actions_for_position(server, *arguments)
    elif command == "finecode.listProjects":
        return await action_tree_endpoints.list_projects(server)
    elif command == "finecode.runBatch":
        return await action_tree_endpoints.run_batch(server, *arguments)
    elif command == "finecode.runAction":
        return await action_tree_endpoints.run_action(server, *arguments)
    elif command == "finecode.runActionOnFile":
        return await action_tree_endpoints.run_action_on_file(server, *arguments)
    elif command == "finecode.runActionOnProject":
        return await action_tree_endpoints.run_action_on_project(server, *arguments)
    elif command == "finecode.reloadAction":
        return await action_tree_endpoints.reload_action(server, *arguments)
    elif command == "finecode.reset":
        return await reset(server, params)
    elif command == "finecode.restartExtensionRunner":
        return await restart_extension_runner(server, *arguments)
    elif command == "finecode.restartAndDebugExtensionRunner":
        return await restart_and_debug_extension_runner(server, *arguments)
    else:
        logger.warning(f"Unknown command: {command}")
        return None


# ---------------------------------------------------------------------------
# Text document sync handlers
# ---------------------------------------------------------------------------


async def _document_did_open(server: LspServer, params: dict | None) -> None:
    typed = _lsp_converter.structure(params, types.DidOpenTextDocumentParams)
    await document_sync_endpoints.document_did_open(server, typed)


async def _document_did_close(server: LspServer, params: dict | None) -> None:
    typed = _lsp_converter.structure(params, types.DidCloseTextDocumentParams)
    await document_sync_endpoints.document_did_close(server, typed)


async def _document_did_save(server: LspServer, params: dict | None) -> None:
    typed = _lsp_converter.structure(params, types.DidSaveTextDocumentParams)
    await document_sync_endpoints.document_did_save(server, typed)


async def _document_did_change(server: LspServer, params: dict | None) -> None:
    typed = _lsp_converter.structure(params, types.DidChangeTextDocumentParams)
    await document_sync_endpoints.document_did_change(server, typed)


# ---------------------------------------------------------------------------
# Formatting handlers
# ---------------------------------------------------------------------------


async def _on_formatting(server: LspServer, params: dict | None) -> list | None:
    typed = _lsp_converter.structure(params, types.DocumentFormattingParams)
    result = await formatting_endpoints.format_document(server, typed)
    return _lsp_converter.unstructure(result) if result is not None else result


async def _on_range_formatting(server: LspServer, params: dict | None) -> list | None:
    typed = _lsp_converter.structure(params, types.DocumentRangeFormattingParams)
    result = await formatting_endpoints.format_range(server, typed)
    return _lsp_converter.unstructure(result) if result else result


async def _on_ranges_formatting(server: LspServer, params: dict | None) -> list | None:
    typed = _lsp_converter.structure(params, types.DocumentRangesFormattingParams)
    result = await formatting_endpoints.format_ranges(server, typed)
    return _lsp_converter.unstructure(result) if result else result


# ---------------------------------------------------------------------------
# Code actions / lens handlers
# ---------------------------------------------------------------------------


async def _on_code_action(server: LspServer, params: dict | None) -> list | None:
    typed = _lsp_converter.structure(params, types.CodeActionParams)
    result = await code_actions_endpoints.document_code_action(server, typed)
    return _lsp_converter.unstructure(result) if result else result


async def _on_code_action_resolve(
    server: LspServer, params: dict | None
) -> dict | None:
    typed = _lsp_converter.structure(params, types.CodeAction)
    result = await code_actions_endpoints.code_action_resolve(server, typed)
    return _lsp_converter.unstructure(result) if result else result


async def _on_code_lens(server: LspServer, params: dict | None) -> list | None:
    typed = _lsp_converter.structure(params, types.CodeLensParams)
    result = await code_lens_endpoints.document_code_lens(server, typed)
    return _lsp_converter.unstructure(result) if result else result


async def _on_code_lens_resolve(
    server: LspServer, params: dict | None
) -> dict | None:
    typed = _lsp_converter.structure(params, types.CodeLens)
    result = await code_lens_endpoints.code_lens_resolve(server, typed)
    return _lsp_converter.unstructure(result) if result else result


# ---------------------------------------------------------------------------
# Diagnostics handlers
# ---------------------------------------------------------------------------


async def _on_document_diagnostic(
    server: LspServer, params: dict | None
) -> dict | None:
    typed = _lsp_converter.structure(params, types.DocumentDiagnosticParams)
    result = await diagnostics_endpoints.document_diagnostic(server, typed)
    return _lsp_converter.unstructure(result) if result is not None else None


async def _on_workspace_diagnostic(
    server: LspServer, params: dict | None
) -> dict | None:
    typed = _lsp_converter.structure(params, types.WorkspaceDiagnosticParams)
    result = await diagnostics_endpoints.workspace_diagnostic(server, typed)
    return _lsp_converter.unstructure(result) if result is not None else None


# ---------------------------------------------------------------------------
# Inlay hints handlers
# ---------------------------------------------------------------------------


async def _on_inlay_hint(server: LspServer, params: dict | None) -> list | None:
    typed = _lsp_converter.structure(params, types.InlayHintParams)
    result = await inlay_hints_endpoints.document_inlay_hint(server, typed)
    return _lsp_converter.unstructure(result) if result else result


async def _on_inlay_hint_resolve(
    server: LspServer, params: dict | None
) -> dict | None:
    typed = _lsp_converter.structure(params, types.InlayHint)
    result = await inlay_hints_endpoints.inlay_hint_resolve(server, typed)
    return _lsp_converter.unstructure(result) if result else result


# ---------------------------------------------------------------------------
# Command implementations (finecode.* commands)
# ---------------------------------------------------------------------------


async def reset(_server: LspServer, _params: dict | None) -> None:
    logger.info("Reset WM")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        logger.error("Reset requested but WM client not connected")
        return

    await global_state.wm_client.request("server/reset", {})


async def restart_extension_runner(
    _server: LspServer, tree_node: dict, _param2: typing.Any = None
) -> None:
    logger.info(f"restart extension runner {tree_node}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        logger.error("Restart runner requested but WM client not connected")
        return

    runner_id = tree_node["projectPath"]
    parts = runner_id.split("::")
    await global_state.wm_client.request(
        "runners/restart",
        {"runnerWorkingDir": parts[0], "envName": parts[-1]},
    )


async def restart_and_debug_extension_runner(
    _server: LspServer, tree_node: dict, _params2: typing.Any = None
) -> None:
    logger.info(f"restart and debug extension runner {tree_node}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        logger.error("Restart+debug runner requested but WM client not connected")
        return

    runner_id = tree_node["projectPath"]
    parts = runner_id.split("::")
    await global_state.wm_client.request(
        "runners/restart",
        {"runnerWorkingDir": parts[0], "envName": parts[-1], "debug": True},
    )


# ---------------------------------------------------------------------------
# Notification / request helpers
# ---------------------------------------------------------------------------


async def send_user_message_notification(
    server: LspServer, message: str, message_type: str
) -> None:
    message_type_pascal = message_type[0] + message_type[1:].lower()
    server.show_message(message, types.MessageType[message_type_pascal].value)


async def send_user_message_request(
    server: LspServer, message: str, message_type: str
) -> None:
    message_type_pascal = message_type[0] + message_type[1:].lower()
    await server.send_request_to_client(
        "window/showMessageRequest",
        {
            "type": types.MessageType[message_type_pascal].value,
            "message": message,
        },
    )


async def start_debug_session(server: LspServer, params: dict) -> None:
    res = await server.send_request_to_client("ide/startDebugging", params)
    logger.info(f"started debugging: {res}")


__all__ = ["create_lsp_server", "LspServer"]
