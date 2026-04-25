"""Extension Runner server.

Replaces the former pygls-based ``CustomLanguageServer``.  The server is built
directly on :class:`finecode_jsonrpc.JsonRpcServerSession` — no LSP abstraction
layer is used.  The WM↔ER protocol is custom JSON-RPC; the only LSP-derived
method names that are kept are the handshake (``initialize`` / ``initialized``),
text document notifications (``textDocument/did*``), and lifecycle
(``shutdown`` / ``exit``).

All former ``workspace/executeCommand`` routes are replaced by direct method
names: ``actions/run``, ``finecodeRunner/updateConfig``, etc.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import dataclasses
import functools
import io
import json
import pathlib
import sys
import threading
import typing

from cattrs import Converter
from cattrs.gen import make_dict_structure_fn, make_dict_unstructure_fn, override
from loguru import logger

import finecode_jsonrpc as finecode_jsonrpc_module
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_runner import context, er_wal, global_state, schemas, services
from finecode_extension_runner._converter import converter as _converter
from finecode_extension_runner._services import merge_results as merge_results_service
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.impls import project_action_runner as project_action_runner_module

# ---------------------------------------------------------------------------
# Protocol types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Position:
    line: int
    character: int


@dataclasses.dataclass
class Range:
    start: Position
    end: Position


@dataclasses.dataclass
class TextEdit:
    range: Range
    new_text: str


@dataclasses.dataclass
class TextDocumentContentChangePartial:
    range: Range
    text: str


@dataclasses.dataclass
class TextDocumentContentChangeWhole:
    text: str


@dataclasses.dataclass
class TextDocumentId:
    uri: str
    version: int | None = None


@dataclasses.dataclass
class TextDocumentEdit:
    text_document: TextDocumentId
    edits: list[TextEdit]


@dataclasses.dataclass
class WorkspaceEdit:
    document_changes: list[TextDocumentEdit]


@dataclasses.dataclass
class ApplyWorkspaceEditParams:
    edit: WorkspaceEdit


@dataclasses.dataclass
class DidOpenTextDocumentParams:
    text_document: TextDocumentId


@dataclasses.dataclass
class DidCloseTextDocumentParams:
    text_document: TextDocumentId


@dataclasses.dataclass
class DidChangeTextDocumentParams:
    text_document: TextDocumentId
    content_changes: list[TextDocumentContentChangePartial | TextDocumentContentChangeWhole]


# Converter for the protocol types — handles camelCase ↔ snake_case.
_protocol_converter = Converter()

_protocol_converter.register_structure_hook(
    TextDocumentContentChangePartial | TextDocumentContentChangeWhole,
    lambda d, _: (
        _protocol_converter.structure(d, TextDocumentContentChangePartial)
        if "range" in d
        else _protocol_converter.structure(d, TextDocumentContentChangeWhole)
    ),
)

for _cls, _renames in [
    (DidOpenTextDocumentParams, {"text_document": override(rename="textDocument")}),
    (DidCloseTextDocumentParams, {"text_document": override(rename="textDocument")}),
    (
        DidChangeTextDocumentParams,
        {
            "text_document": override(rename="textDocument"),
            "content_changes": override(rename="contentChanges"),
        },
    ),
]:
    _protocol_converter.register_structure_hook(
        _cls, make_dict_structure_fn(_cls, _protocol_converter, **_renames)
    )

_protocol_converter.register_unstructure_hook(
    TextEdit,
    make_dict_unstructure_fn(TextEdit, _protocol_converter, new_text=override(rename="newText")),
)
_protocol_converter.register_unstructure_hook(
    TextDocumentEdit,
    make_dict_unstructure_fn(
        TextDocumentEdit, _protocol_converter, text_document=override(rename="textDocument")
    ),
)
_protocol_converter.register_unstructure_hook(
    WorkspaceEdit,
    make_dict_unstructure_fn(
        WorkspaceEdit, _protocol_converter, document_changes=override(rename="documentChanges")
    ),
)


# ---------------------------------------------------------------------------
# ErServer
# ---------------------------------------------------------------------------


class ErServer:
    """Extension Runner JSON-RPC server.

    Backed by :class:`~finecode_jsonrpc.JsonRpcServerSession` and one of the
    server transports from :mod:`finecode_jsonrpc.server_transport`.
    """

    def __init__(self) -> None:
        self._session = finecode_jsonrpc_module.JsonRpcServerSession()
        self._finecode_async_tasks: list[asyncio.Task] = []
        self._finecode_exit_stack = contextlib.AsyncExitStack()
        self._finecode_file_editor_session: ifileeditor.IFileEditorSession
        self._finecode_file_operation_author = ifileeditor.FileOperationAuthor(
            id="FineCode_Extension_Runner_Server"
        )
        self._stop_event = threading.Event()
        self._tcp_server: asyncio.Server | None = None
        self._runner_context: context.RunnerContext | None = None
        self._wal_writer: er_wal.ErWalWriter | None = None

    # ------------------------------------------------------------------
    # Server → client helpers
    # ------------------------------------------------------------------

    def send_progress_sync(self, token: int | str, value: str) -> None:
        """Send ``$/progress`` notification (thread-safe, fire-and-forget)."""
        self._session._transport.send(  # type: ignore[union-attr]
            {
                "jsonrpc": "2.0",
                "method": "$/progress",
                "params": {"token": token, "value": value},
            }
        )

    async def workspace_apply_edit_async(self, params: ApplyWorkspaceEditParams) -> dict:
        """Send ``workspace/applyEdit`` request to the WM and return result."""
        return await self._session.send_request(
            "workspace/applyEdit", _protocol_converter.unstructure(params)
        )

    async def send_request_to_wm(self, method: str, params: dict) -> typing.Any:
        """Send an arbitrary request to the WM (e.g. ``projects/getRawConfig``)."""
        return await self._session.send_request(method, params)

    def shutdown(self) -> None:
        """Signal the transport read loop to stop."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Start methods
    # ------------------------------------------------------------------

    async def start_io_async(
        self,
        stdin_buf: io.BinaryIO | None = None,
        stdout_buf: io.BinaryIO | None = None,
    ) -> None:
        """Start the server on stdin/stdout."""
        logger.info("Starting ER server on stdio")
        transport = finecode_jsonrpc_module.ServerStdioTransport(
            readable_id="er_server"
        )
        self._session.attach(transport)
        await transport.start(
            stdin_buf=stdin_buf or sys.stdin.buffer,
            stdout_buf=stdout_buf or sys.stdout.buffer,
        )
        # Block until the transport read loop finishes
        while not transport._stop_event.is_set():
            await asyncio.sleep(0.05)
        await self._finecode_exit_stack.aclose()
        logger.debug("ER server stdio loop finished")

    def start_tcp(self, host: str, port: int) -> None:
        """Start the server on a TCP port (blocking)."""
        try:
            asyncio.run(self._run_tcp(host, port))
        except asyncio.CancelledError:
            logger.debug("TCP server was cancelled")

    async def start_tcp_async(self, host: str, port: int) -> None:
        """Start the server on a TCP port from within an existing event loop."""
        await self._run_tcp(host, port)

    async def _run_tcp(self, host: str, port: int) -> None:
        logger.info("Starting ER server on TCP %s:%s", host, port)

        async def _handle_connection(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            logger.debug("TCP client connected")
            transport = finecode_jsonrpc_module.TcpServerTransport(
                reader, writer, readable_id="er_server_tcp"
            )
            self._session.attach(transport)
            await transport.start()
            # Wait until transport is done
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
        finally:
            await self._finecode_exit_stack.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def file_editor_file_change_to_text_edit(
    file_change: ifileeditor.FileChange,
) -> TextEdit:
    if isinstance(file_change, ifileeditor.FileChangeFull):
        # Temporary workaround: replace the whole document via a huge range
        range_start_line = 0
        range_start_char = 0
        range_end_line = 999999
        range_end_char = 999999
    else:
        range_start_line = file_change.range.start.line
        range_start_char = file_change.range.start.character
        range_end_line = file_change.range.end.line
        range_end_char = file_change.range.end.character

    return TextEdit(
        range=Range(
            start=Position(line=range_start_line, character=range_start_char),
            end=Position(line=range_end_line, character=range_end_char),
        ),
        new_text=file_change.text,
    )


def uri_to_path(uri: str) -> pathlib.Path:
    return pathlib.Path(uri.removeprefix("file://"))


def convert_path_keys(
    obj: dict[str | pathlib.Path, typing.Any] | list[typing.Any],
) -> dict[str, typing.Any] | list[typing.Any]:
    if isinstance(obj, dict):
        return {str(k): convert_path_keys(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_path_keys(item) for item in obj]
    return obj



# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _on_initialize(_server: ErServer, params: dict | None) -> dict:
    logger.info(f"initialize: {params}")
    return {
        "capabilities": {
            "textDocumentSync": 1,  # Full sync
        },
        "serverInfo": {
            "name": "FineCode_Extension_Runner_Server",
            "version": "v1",
        },
    }


async def _on_initialized(_server: ErServer, params: dict | None) -> None:
    logger.info(f"initialized: {params}")


async def _on_shutdown(server: ErServer, _params: dict | None) -> None:
    logger.info("Shutdown extension runner")
    if server._wal_writer is not None:
        server._wal_writer.close()
    services.shutdown_all_action_handlers(server._runner_context)

    logger.debug("Stop Finecode async tasks")
    for task in server._finecode_async_tasks:
        if not task.done():
            task.cancel()
    server._finecode_async_tasks = []

    logger.info("Shutdown end")
    server.shutdown()


async def _on_exit(_server: ErServer, _params: dict | None) -> None:
    logger.info("Exit extension runner")
    if _server._wal_writer is not None:
        _server._wal_writer.close()


async def _document_did_open(server: ErServer, params: dict | None) -> None:
    typed = _protocol_converter.structure(params, DidOpenTextDocumentParams)
    logger.info(f"document did open: {typed.text_document.uri}")
    file_path = uri_to_path(uri=typed.text_document.uri)
    await server._finecode_file_editor_session.open_file(file_path=file_path)


async def _document_did_close(server: ErServer, params: dict | None) -> None:
    typed = _protocol_converter.structure(params, DidCloseTextDocumentParams)
    logger.info(f"document did close: {typed.text_document.uri}")
    file_path = uri_to_path(uri=typed.text_document.uri)
    await server._finecode_file_editor_session.close_file(file_path=file_path)


def _change_to_file_editor_change(
    change: TextDocumentContentChangePartial | TextDocumentContentChangeWhole,
) -> ifileeditor.FileChange:
    if isinstance(change, TextDocumentContentChangePartial):
        return ifileeditor.FileChangePartial(
            range=ifileeditor.Range(
                start=ifileeditor.Position(
                    line=change.range.start.line,
                    character=change.range.start.character,
                ),
                end=ifileeditor.Position(
                    line=change.range.end.line,
                    character=change.range.end.character,
                ),
            ),
            text=change.text,
        )
    else:
        return ifileeditor.FileChangeFull(text=change.text)


async def _document_did_change(server: ErServer, params: dict | None) -> None:
    typed = _protocol_converter.structure(params, DidChangeTextDocumentParams)
    logger.info(
        f"document did change: {typed.text_document.uri} {typed.text_document.version}"
    )
    file_path = uri_to_path(uri=typed.text_document.uri)
    for change in typed.content_changes:
        logger.trace(str(change))
        file_editor_change = _change_to_file_editor_change(change)
        await server._finecode_file_editor_session.change_file(
            file_path=file_path, change=file_editor_change
        )


async def get_project_raw_config(
    server: ErServer, project_def_path: str
) -> dict[str, typing.Any]:
    raw_config = await asyncio.wait_for(
        server.send_request_to_wm(
            "projects/getRawConfig", params={"projectDefPath": project_def_path}
        ),
        10,
    )
    return raw_config["config"]


async def update_config(server: ErServer, params: dict | None) -> dict:
    """Handler for ``finecodeRunner/updateConfig``."""
    assert params is not None
    working_dir = pathlib.Path(params["workingDir"])
    project_name: str = params["projectName"]
    project_def_path = pathlib.Path(params["projectDefPath"])
    config: dict = params["config"]

    logger.trace(f"Update config: {working_dir} {project_name} {config}")
    try:
        actions = config["actions"]
        action_handler_configs = config["action_handler_configs"]

        request = schemas.UpdateConfigRequest(
            working_dir=working_dir,
            project_name=project_name,
            project_def_path=project_def_path,
            actions={
                action["name"]: schemas.Action(
                    name=action["name"],
                    handlers=[
                        schemas.ActionHandler(
                            name=handler["name"],
                            source=handler["source"],
                            config=handler["config"],
                        )
                        for handler in action["handlers"]
                    ],
                    source=action["source"],
                    config=action["config"],
                )
                for action in actions
            },
            action_handler_configs=action_handler_configs,
            services=[
                schemas.ServiceDeclaration(
                    interface=svc["interface"],
                    source=svc["source"],
                )
                for svc in config.get("services", [])
            ],
            handlers_to_initialize=config.get("handlers_to_initialize"),
        )

        async def _send_request_to_wm(method: str, req_params: dict):
            return await server.send_request_to_wm(method, req_params)

        response, runner_context = await services.update_config(
            request=request,
            project_raw_config_getter=functools.partial(get_project_raw_config, server),
            send_request_to_wm=_send_request_to_wm,
        )
        runner_context.wal_writer = server._wal_writer
        server._runner_context = runner_context

        file_editor = await runner_context.di_registry.get_instance(ifileeditor.IFileEditor)
        server._finecode_file_editor_session = (
            await server._finecode_exit_stack.enter_async_context(
                file_editor.session(author=server._finecode_file_operation_author)
            )
        )

        async def send_changed_files_to_wm() -> None:
            async with server._finecode_file_editor_session.subscribe_to_changes_of_opened_files() as file_change_events:
                async for file_change_event in file_change_events:
                    if (
                        file_change_event.author
                        != server._finecode_file_operation_author
                    ):
                        edit_params = ApplyWorkspaceEditParams(
                            edit=WorkspaceEdit(
                                document_changes=[
                                    TextDocumentEdit(
                                        text_document=TextDocumentId(
                                            uri=f"file://{file_change_event.file_path.as_posix()}"
                                        ),
                                        edits=[
                                            file_editor_file_change_to_text_edit(
                                                file_change=file_change_event.change
                                            )
                                        ],
                                    ),
                                ]
                            )
                        )
                        await server.workspace_apply_edit_async(edit_params)

        task = asyncio.create_task(send_changed_files_to_wm())
        server._finecode_async_tasks.append(task)

        return response.to_dict()
    except Exception as exc:
        logger.exception(f"Update config error: {exc}")
        raise


async def run_action(server: ErServer, params: dict | None) -> dict:
    """Handler for ``actions/run``."""
    assert params is not None
    action_name: str = params["actionName"]
    action_params: dict = params.get("params") or {}
    options: dict | None = params.get("options")

    logger.trace(f"Run action: {action_name}")
    wal_run_id = (options or {}).get("wal_run_id")
    if not isinstance(wal_run_id, str) or wal_run_id.strip() == "":
        return {"error": "Missing required wal_run_id in run options"}

    meta = (options or {}).get("meta") or {}
    trigger = meta.get("trigger", "unknown")
    dev_env = meta.get("dev_env", "unknown")
    if server._runner_context is None:
        return {"error": "Extension runner not initialized"}
    project_path = server._runner_context.project.dir_path
    er_wal.emit_run_event(
        server._wal_writer,
        event_type=er_wal.ErWalEventType.RUN_ACCEPTED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_path,
        trigger=trigger,
        dev_env=dev_env,
        payload={
            "partial_result_token": (options or {}).get("partial_result_token"),
            "progress_token": (options or {}).get("progress_token"),
        },
    )

    request = schemas.RunActionRequest(action_name=action_name, params=action_params)
    options_schema = _converter.structure(
        options if options is not None else {}, schemas.RunActionOptions
    )
    status: str = "success"

    try:
        response = await services.run_action_raw(
            request=request, options=options_schema, runner_context=server._runner_context
        )
    except Exception as exception:
        if isinstance(exception, services.StopWithResponse):
            status = "stopped"
            response = exception.response
            er_wal.emit_run_event(
                server._wal_writer,
                event_type=er_wal.ErWalEventType.RUN_FAILED,
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=project_path,
                trigger=trigger,
                dev_env=dev_env,
                payload={"error": "stopped"},
            )
        else:
            if isinstance(exception, services.ActionFailedException):
                logger.error(f"Run action failed: {exception.message}")
                error_msg = exception.message
            else:
                logger.error("Unhandled exception in action run:")
                logger.exception(exception)
                error_msg = f"{type(exception)}: {str(exception)}"
            er_wal.emit_run_event(
                server._wal_writer,
                event_type=er_wal.ErWalEventType.RUN_FAILED,
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=project_path,
                trigger=trigger,
                dev_env=dev_env,
                payload={"error": error_msg},
            )
            return {"error": error_msg}

    result_by_format = response.to_dict()["result_by_format"]
    if not result_by_format and options_schema.partial_result_token is not None:
        status = "streamed"
    converted_result_by_format = {
        fmt: convert_path_keys(result) if isinstance(result, dict) else result
        for fmt, result in result_by_format.items()
    }
    er_wal.emit_run_event(
        server._wal_writer,
        event_type=er_wal.ErWalEventType.RUN_COMPLETED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_path,
        trigger=trigger,
        dev_env=dev_env,
        payload={"status": status, "return_code": response.return_code},
    )
    return {
        "status": status,
        "resultByFormat": converted_result_by_format,
        "returnCode": response.return_code,
    }


async def run_handlers(server: ErServer, params: dict | None) -> dict:
    """Handler for ``actions/runHandlers``."""
    assert params is not None
    action_name: str = params["actionName"]
    handler_names: list[str] = params.get("handlerNames", [])
    action_params: dict = params.get("params") or {}
    previous_result: dict | None = params.get("previousResult")
    options: dict | None = params.get("options")

    logger.trace(
        f"Run handlers: action={action_name}, handlers={handler_names}, "
        f"has_previous_result={previous_result is not None}"
    )

    wal_run_id = (options or {}).get("wal_run_id")
    if not isinstance(wal_run_id, str) or wal_run_id.strip() == "":
        return {"error": "Missing required wal_run_id in run options"}

    meta = (options or {}).get("meta") or {}
    trigger = meta.get("trigger", "unknown")
    dev_env = meta.get("dev_env", "unknown")
    if server._runner_context is None:
        return {"error": "Extension runner not initialized"}
    project_path = server._runner_context.project.dir_path
    er_wal.emit_run_event(
        server._wal_writer,
        event_type=er_wal.ErWalEventType.RUN_ACCEPTED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_path,
        trigger=trigger,
        dev_env=dev_env,
        payload={"handler_names": handler_names},
    )

    request = schemas.RunHandlersRequest(
        action_name=action_name,
        handler_names=handler_names,
        params=action_params,
        previous_result=previous_result,
    )
    options_schema = _converter.structure(
        options if options is not None else {}, schemas.RunActionOptions
    )
    status: str = "success"

    try:
        response = await services.run_handlers_raw(
            request=request, options=options_schema, runner_context=server._runner_context
        )
    except Exception as exception:
        if isinstance(exception, services.ActionFailedException):
            logger.error(f"Run handlers failed: {exception.message}")
            error_msg = exception.message
        else:
            logger.error("Unhandled exception in run_handlers:")
            logger.exception(exception)
            error_msg = f"{type(exception)}: {str(exception)}"
        er_wal.emit_run_event(
            server._wal_writer,
            event_type=er_wal.ErWalEventType.RUN_FAILED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=project_path,
            trigger=trigger,
            dev_env=dev_env,
            payload={"error": error_msg},
        )
        return {"error": error_msg}

    result_by_format = response.result_by_format
    if not result_by_format and options_schema.partial_result_token is not None:
        status = "streamed"
    converted_result_by_format = {
        fmt: convert_path_keys(result) if isinstance(result, dict) else result
        for fmt, result in result_by_format.items()
    }
    er_wal.emit_run_event(
        server._wal_writer,
        event_type=er_wal.ErWalEventType.RUN_COMPLETED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_path,
        trigger=trigger,
        dev_env=dev_env,
        payload={"status": status, "return_code": response.return_code},
    )
    return {
        "status": status,
        "result": convert_path_keys(response.result) if response.result else {},
        "resultByFormat": converted_result_by_format,
        "returnCode": response.return_code,
    }


async def reload_action(server: ErServer, params: dict | None) -> dict:
    assert params is not None
    action_name: str = params["actionName"]
    logger.trace(f"Reload action: {action_name}")
    if server._runner_context is None:
        return {}
    services.reload_action(action_name, server._runner_context)
    return {}


async def resolve_package_path(_server: ErServer, params: dict | None) -> dict:
    assert params is not None
    package_name: str = params["packageName"]
    logger.trace(f"Resolve package path: {package_name}")
    result = services.resolve_package_path(package_name)
    logger.trace(f"Resolved {package_name} to {result}")
    return {"packagePath": result}


async def get_payload_schemas_cmd(server: ErServer, _params: dict | None) -> dict:
    logger.trace("Get payload schemas")
    if server._runner_context is None:
        return {}
    return services.get_payload_schemas(server._runner_context)


async def merge_results_cmd(server: ErServer, params: dict | None) -> dict:
    assert params is not None
    action_name: str = params["actionName"]
    results: list = params["results"]
    logger.trace(f"Merge results: action={action_name}, count={len(results)}")
    if server._runner_context is None:
        return {"error": "Extension runner not initialized"}
    try:
        merged = await merge_results_service.merge_results(
            action_name=action_name, results=results, runner_context=server._runner_context
        )
        return {"merged": merged}
    except Exception as exception:
        logger.exception(f"Merge results error: {exception}")
        return {"error": str(exception)}


async def resolve_source(_server: ErServer, params: dict | None) -> dict:
    """Handler for ``actions/resolveSource``.

    Resolves an arbitrary import-path alias to the canonical class path.
    The canonical path is ``cls.__module__ + "." + cls.__qualname__`` and is
    globally unique regardless of how many re-export aliases point to the class.

    Raises ``ValueError`` when the alias cannot be imported or resolved.
    """
    assert params is not None
    source: str = params["source"]
    logger.trace(f"Resolve source: {source}")
    last_dot = source.rfind(".")
    if last_dot == -1:
        raise ValueError(f"Invalid source path (no module separator): {source!r}")
    import importlib
    module_path = source[:last_dot]
    attr_name = source[last_dot + 1:]
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, attr_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"Cannot resolve source '{source}': {exc}") from exc
    canonical = f"{cls.__module__}.{cls.__qualname__}"
    return {"canonicalSource": canonical}


async def resolve_action_meta(server: ErServer, _params: dict | None) -> dict:
    """Handler for ``finecodeRunner/resolveActionMeta``."""
    if server._runner_context is None:
        return {}
    return await services.resolve_action_meta(server._runner_context)


async def get_runner_info(_server: ErServer, _params: dict | None) -> dict:
    log_path = global_state.log_file_path
    return {"logFilePath": str(log_path) if log_path is not None else None}


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_er_server(wal_writer: er_wal.ErWalWriter | None = None) -> ErServer:
    """Create and wire the ER server with all handlers registered."""
    server = ErServer()
    server._wal_writer = wal_writer
    session = server._session

    def _wrap(handler):
        """Wrap a handler that takes (server, params) for use with the session."""
        async def _wrapped(params: dict | None) -> typing.Any:
            return await handler(server, params)
        return _wrapped

    # Lifecycle (requests)
    session.on_request("initialize", _wrap(_on_initialize))
    session.on_request("shutdown", _wrap(_on_shutdown))

    # Lifecycle (notifications)
    session.on_notification("initialized", _wrap(_on_initialized))
    session.on_notification("exit", _wrap(_on_exit))

    # Text document sync (notifications)
    session.on_notification("textDocument/didOpen", _wrap(_document_did_open))
    session.on_notification("textDocument/didClose", _wrap(_document_did_close))
    session.on_notification("textDocument/didChange", _wrap(_document_did_change))

    # Partial results forwarded from WM to ER (for run_action_iter cross-env path)
    async def _on_progress_from_wm(params: dict | None) -> None:
        if params is None:
            return
        token = params.get("token")
        value = params.get("value")
        if token is None or value is None:
            logger.debug(f"$/progress from WM: missing token or value")
            return
        try:
            value_dict = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"$/progress from WM: failed to decode value: {exc}")
            return
        logger.trace(f"$/progress from WM: token={token}, preview={str(value_dict)[:200]}")
        project_action_runner_module.dispatch_partial_result_from_wm(token, value_dict)

    session.on_notification("$/progress", _on_progress_from_wm)

    # ER-specific commands (direct JSON-RPC methods, previously workspace/executeCommand)
    session.on_request("finecodeRunner/updateConfig", _wrap(update_config))
    session.on_request("finecodeRunner/resolveActionMeta", _wrap(resolve_action_meta))
    session.on_request("actions/run", _wrap(run_action))
    session.on_request("actions/runHandlers", _wrap(run_handlers))
    session.on_request("actions/resolveSource", _wrap(resolve_source))
    session.on_request("actions/reload", _wrap(reload_action))
    session.on_request("packages/resolvePath", _wrap(resolve_package_path))
    session.on_request("actions/mergeResults", _wrap(merge_results_cmd))
    session.on_request("actions/getPayloadSchemas", _wrap(get_payload_schemas_cmd))
    session.on_request("finecodeRunner/getInfo", _wrap(get_runner_info))

    def on_process_exit() -> None:
        logger.info("Exit extension runner (atexit)")
        if server._wal_writer is not None:
            server._wal_writer.close()
        services.shutdown_all_action_handlers(server._runner_context)
        services.exit_all_action_handlers(server._runner_context)

    atexit.register(on_process_exit)

    def send_partial_result(
        token: int | str, partial_result: code_action.RunActionResult
    ) -> None:
        partial_result_dict = dataclasses.asdict(partial_result)
        partial_result_json = json.dumps(partial_result_dict)
        logger.trace(
            f"send_partial_result: token={token}, length={len(partial_result_json)}, "
            f"preview={partial_result_json[:200]}"
        )
        server.send_progress_sync(token, partial_result_json)

    run_action_service.set_partial_result_sender(send_partial_result)

    def send_progress(token: int | str, value_dict: dict) -> None:
        value_json = json.dumps(value_dict)
        logger.trace(
            f"send_progress: token={token}, type={value_dict.get('type')}, "
            f"preview={value_json[:200]}"
        )
        server.send_progress_sync(token, value_json)

    run_action_service.set_progress_sender(send_progress)

    return server
