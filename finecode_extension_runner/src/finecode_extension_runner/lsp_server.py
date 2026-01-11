# wrap all endpoint handlers in try/except because pygls only sends errors to client
# and don't log it locally
#
# keep at least until `lsp_server.ServerErrors` is used, because it is hidden under
# `TYPE_CHECKING` and its evaluation in runtime causes crash
from __future__ import annotations

import atexit
import collections.abc
import dataclasses
import functools
import json
import pathlib
import typing

import pygls.exceptions as pygls_exceptions
from pygls.workspace import position_codec
from loguru import logger
from lsprotocol import types
from pygls.lsp import server as lsp_server
from pygls.io_ import StdoutWriter, run_async
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor
from pydantic.dataclasses import dataclass as pydantic_dataclass

from finecode_extension_runner import schemas, services
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.di import resolver

import sys
import io
import threading
import contextlib
import asyncio


class StdinAsyncReader:
    """Read from stdin asynchronously."""

    def __init__(self, stdin: io.TextIO, stop_event: threading.Event | None = None):
        self.stdin = stdin
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = stop_event

        self.reader = asyncio.StreamReader()
        self.transport: asyncio.ReadTransport | None = None
        self.initialized = False

    @property
    def loop(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        return self._loop

    async def readline(self) -> bytes:
        if not self.initialized:
            await self.initialize()

        while not self._stop_event.is_set():
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
                if not line:  # EOF
                    break
                return line
            except TimeoutError:
                ...
            except ValueError as exception:
                logger.warning(str(exception))
        return bytes()

    async def readexactly(self, n: int) -> bytes:
        if not self.initialized:
            await self.initialize()

        while not self._stop_event.is_set():
            try:
                line = await asyncio.wait_for(self.reader.read(n), timeout=0.1)
                if not line:  # EOF
                    break
                return line
            except TimeoutError:
                ...
        return bytes()

    async def initialize(self) -> None:
        protocol = asyncio.StreamReaderProtocol(self.reader)
        self.transport, _ = await self.loop.connect_read_pipe(
            lambda: protocol, self.stdin
        )
        self.initialized = True

    def stop(self) -> None:
        if self.transport:
            self.transport.close()


class CustomLanguageServer(lsp_server.LanguageServer):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._finecode_async_tasks: list[asyncio.Task] = []
        self._finecode_exit_stack = contextlib.AsyncExitStack()
        self._finecode_file_editor_session: ifileeditor.IFileEditorSession
        self._finecode_file_operation_author = ifileeditor.FileOperationAuthor(id=self.name)

    def report_server_error(self, error: Exception, source: lsp_server.ServerErrors):
        logger.info(f'->1 {self._stop_event.is_set()}')
        # return logging of error (`lsp_server.LanguageServer` overwrites it)
        super(lsp_server.LanguageServer, self).report_server_error(error, source)
        logger.info(f'->2 {self._stop_event.is_set()}')
        # log traceback of exception for easier analysis
        logger.exception(error)
        # send to client
        if not isinstance(error, ValueError):
            # TODO: check message 'write to closed file'
            super().report_server_error(error, source)
            logger.info(f'->3 {self._stop_event.is_set()}')

    async def start_io_async(
        self, stdin: io.BinaryIO | None = None, stdout: io.BinaryIO | None = None
    ):
        """Starts an asynchronous IO server."""
        # overwrite this method to use custom StdinAsyncReader which handles stop event properly
        logger.info("Starting async IO server")

        self._stop_event = threading.Event()
        reader = StdinAsyncReader(sys.stdin, self._stop_event)
        writer = StdoutWriter(stdout or sys.stdout.buffer)
        self.protocol.set_writer(writer)

        try:
            await run_async(
                stop_event=self._stop_event,
                reader=reader,
                protocol=self.protocol,
                logger=logger,
                error_handler=self.report_server_error,
            )
        except BrokenPipeError:
            logger.error("Connection to the client is lost! Shutting down the server.")
        except (KeyboardInterrupt, SystemExit):
            logger.info("exception handler in json rpc server")
            pass
        finally:
            logger.info(f'->5 {self._stop_event.is_set()}')
            reader.stop()
            self.shutdown()

            # shutdown is synchronous, so close exit stack here
            await self._finecode_exit_stack.aclose()
            logger.debug("Finecode async exit stack closed")

    def start_tcp(self, host: str, port: int) -> None:
        """Starts TCP server."""
        logger.info("Starting TCP server on %s:%s", host, port)

        self._stop_event = stop_event = threading.Event()

        async def lsp_connection(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            logger.debug("Connected to client")
            self.protocol.set_writer(writer)  # type: ignore
            await run_async(
                stop_event=stop_event,
                reader=reader,
                protocol=self.protocol,
                logger=logger,
                error_handler=self.report_server_error,
            )
            logger.debug("Main loop finished")
            self.shutdown()

        async def tcp_server(h: str, p: int):
            self._server = await asyncio.start_server(lsp_connection, h, p)

            addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets)
            logger.info(f"Serving on {addrs}")

            try:
                async with self._server:
                    await self._server.serve_forever()
            finally:
                # shutdown is synchronous, so close exit stack here
                # TODO: test
                await self._finecode_exit_stack.aclose()

        try:
            asyncio.run(tcp_server(host, port))
        except asyncio.CancelledError:
            logger.debug("Server was cancelled")



def file_editor_file_change_to_lsp_text_edit(file_change: ifileeditor.FileChange) -> types.TextEdit:
    if isinstance(file_change, ifileeditor.FileChangeFull):
        # temporary workaround until we extend "applyWorkspaceEdit" from LSP with
        # proper full document replacement without knowing original range
        range_start_line = 0
        range_start_char = 0
        range_end_line = 999999
        range_end_char = 999999
    else:
        range_start_line = file_change.range.start.line
        range_start_char = file_change.range.start.character
        range_end_line = file_change.range.end.line
        range_end_char = file_change.range.end.character
    
    return types.TextEdit(
        range=types.Range(
            start=types.Position(line=range_start_line, character=range_start_char),
            end=types.Position(line=range_end_line, character=range_end_char)
        ),
        new_text=file_change.text
    )


def position_from_client_units(
    self, lines: collections.abc.Sequence[str], position: types.Position
) -> types.Position:
    return position


def create_lsp_server() -> lsp_server.LanguageServer:
    # avoid recalculating of positions by pygls
    position_codec.PositionCodec.position_from_client_units = position_from_client_units
    
    server = CustomLanguageServer("FineCode_Extension_Runner_Server", "v1")

    register_initialized_feature = server.feature(types.INITIALIZED)
    register_initialized_feature(_on_initialized)

    register_shutdown_feature = server.feature(types.SHUTDOWN)
    register_shutdown_feature(_on_shutdown)

    register_exit_feature = server.feature(types.EXIT)
    register_exit_feature(_on_exit)

    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    register_document_did_open_feature(_document_did_open)

    register_document_did_close_feature = server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    register_document_did_close_feature(_document_did_close)
    
    register_document_did_change_feature = server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
    register_document_did_change_feature(_document_did_change)

    register_update_config_feature = server.command("finecodeRunner/updateConfig")
    register_update_config_feature(update_config)

    register_run_task_cmd = server.command("actions/run")
    register_run_task_cmd(run_action)

    register_reload_action_cmd = server.command("actions/reload")
    register_reload_action_cmd(reload_action)

    register_resolve_package_path_cmd = server.command("packages/resolvePath")
    register_resolve_package_path_cmd(resolve_package_path)

    def on_process_exit():
        logger.info("Exit extension runner")
        services.shutdown_all_action_handlers()
        services.exit_all_action_handlers()

    atexit.register(on_process_exit)

    def send_partial_result(
        token: int | str, partial_result: code_action.RunActionResult
    ) -> None:
        partial_result_dict = dataclasses.asdict(partial_result)
        partial_result_json = json.dumps(partial_result_dict)
        logger.debug(
            f"Send partial result for {token}, length {len(partial_result_json)}"
        )
        server.progress(types.ProgressParams(token=token, value=partial_result_json))

    run_action_service.set_partial_result_sender(send_partial_result)
    
    return server


def _on_initialized(ls: CustomLanguageServer, params: types.InitializedParams):
    logger.info(f"initialized {params}")


def _on_shutdown(ls: CustomLanguageServer, params):
    logger.info("Shutdown extension runner")
    services.shutdown_all_action_handlers()
    
    logger.debug("Stop Finecode async tasks")
    for task in ls._finecode_async_tasks:
        if not task.done():
            task.cancel()
    ls._finecode_async_tasks = []

    logger.info("Shutdown end")
    return None


def _on_exit(ls: lsp_server.LanguageServer, params):
    logger.info("Exit extension runner")


def uri_to_path(uri: str) -> pathlib.Path:
    return pathlib.Path(uri.removeprefix('file://'))


async def _document_did_open(
    ls: CustomLanguageServer, params: types.DidOpenTextDocumentParams
):
    logger.info(f"document did open: {params.text_document.uri}")
    # services.document_did_open(params.text_document.uri)
    file_path = uri_to_path(uri=params.text_document.uri)
    
    await ls._finecode_file_editor_session.open_file(file_path=file_path)


async def _document_did_close(
    ls: CustomLanguageServer, params: types.DidCloseTextDocumentParams
):
    logger.info(f"document did close: {params.text_document.uri}")
    file_path = uri_to_path(uri=params.text_document.uri)
    
    await ls._finecode_file_editor_session.close_file(file_path=file_path)


def lsp_document_change_to_file_editor_change(lsp_change: types.TextDocumentContentChangeEvent) -> ifileeditor.FileChange:
    if isinstance(lsp_change, types.TextDocumentContentChangePartial):
        return ifileeditor.FileChangePartial(range=ifileeditor.Range(start=ifileeditor.Position(line=lsp_change.range.start.line, character=lsp_change.range.start.character), end=ifileeditor.Position(line=lsp_change.range.end.line, character=lsp_change.range.end.character)), text=lsp_change.text)
    elif isinstance(lsp_change, types.TextDocumentContentChangeWholeDocument):
        return ifileeditor.FileChangeFull(text=lsp_change.text)
    else:
        logger.error(f"Unexpected type of document change from LSP client: {type(lsp_change)}")


async def _document_did_change(
    ls: CustomLanguageServer, params: types.DidChangeTextDocumentParams
):
    logger.info(f"document did change: {params.text_document.uri} {params.text_document.version}")
    file_path = uri_to_path(uri=params.text_document.uri)

    for change in params.content_changes:
        logger.trace(str(change))
        file_editor_change = lsp_document_change_to_file_editor_change(lsp_change=change)
        await ls._finecode_file_editor_session.change_file(file_path=file_path, change=file_editor_change)


async def get_project_raw_config(
    server: lsp_server.LanguageServer, project_def_path: str
) -> dict[str, typing.Any]:
    try:
        raw_config = await asyncio.wait_for(
            server.protocol.send_request_async(
                "projects/getRawConfig", params={"projectDefPath": project_def_path}
            ),
            10,
        )
    except TimeoutError as error:
        raise error
    except pygls_exceptions.JsonRpcInternalError as error:
        raise error

    return json.loads(raw_config.config)


async def update_config(
    ls: CustomLanguageServer,
    working_dir: pathlib.Path,
    project_name: str,
    project_def_path: pathlib.Path,
    config: dict[str, typing.Any],
):
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
        )
        response = await services.update_config(
            request=request,
            project_raw_config_getter=functools.partial(get_project_raw_config, ls),
        )
        # update_config calls DI bootstrap, we can instantiate file_editor_session first
        # here
        file_editor = await resolver.get_service_instance(ifileeditor.IFileEditor)
        ls._finecode_file_editor_session = await ls._finecode_exit_stack.enter_async_context(file_editor.session(author=ls._finecode_file_operation_author))

        # asyncio event loop is currently available only in handlers, not in server factory,
        # so start task here
        async def send_changed_files_to_lsp_client() -> None:
            async with ls._finecode_file_editor_session.subscribe_to_changes_of_opened_files() as file_change_events:
                async for file_change_event in file_change_events:
                    if file_change_event.author != ls._finecode_file_operation_author:
                        # someone else changed the file, send these changes to LSP client
                        params = types.ApplyWorkspaceEditParams(
                            edit=types.WorkspaceEdit(
                                document_changes=[
                                    types.TextDocumentEdit(
                                    text_document=types.OptionalVersionedTextDocumentIdentifier(uri=f'file://{file_change_event.file_path.as_posix()}'),
                                    edits=[
                                        file_editor_file_change_to_lsp_text_edit(file_change=file_change_event.change)
                                    ]
                                    ),
                                ]
                            )
                        )
                        await ls.workspace_apply_edit_async(params)

        send_changed_files_task = asyncio.create_task(send_changed_files_to_lsp_client())
        ls._finecode_async_tasks.append(send_changed_files_task)

        return response.to_dict()
    except Exception as e:
        logger.exception(f"Update config error: {e}")
        raise e


def convert_path_keys(
    obj: dict[str | pathlib.Path, typing.Any] | list[typing.Any],
) -> dict[str, typing.Any] | list[typing.Any]:
    if isinstance(obj, dict):
        return {str(k): convert_path_keys(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_path_keys(item) for item in obj]
    return obj


class CustomJSONEncoder(json.JSONEncoder):
    # add support of serializing pathes to json.dumps
    def default(self, obj):
        if isinstance(obj, (pathlib.Path, pathlib.PosixPath, pathlib.WindowsPath)):
            return str(obj)
        return super().default(obj)


async def run_action(
    ls: lsp_server.LanguageServer,
    action_name: str,
    params: dict[str, typing.Any],
    options: dict[str, typing.Any] | None,
):
    logger.trace(f"Run action: {action_name}")
    request = schemas.RunActionRequest(action_name=action_name, params=params)
    
    # use pydantic dataclass to convert dict to dataclass instance recursively
    # (default dataclass constructor doesn't handle nested items, it stores them just
    # as dict)
    options_type = pydantic_dataclass(schemas.RunActionOptions)
    options_schema = options_type(**options if options is not None else {})
    status: str = "success"

    try:
        response = await services.run_action_raw(request=request, options=options_schema)
    except Exception as exception:
        if isinstance(exception, services.StopWithResponse):
            status = "stopped"
            response = exception.response
        else:
            error_msg = ""
            if isinstance(exception, services.ActionFailedException):
                logger.error(f"Run action failed: {exception.message}")
                error_msg = exception.message
            else:
                logger.error("Unhandled exception in action run:")
                logger.exception(exception)
                error_msg = f"{type(exception)}: {str(exception)}"
            return {"error": error_msg}

    # dict key can be path, but pygls fails to handle slashes in dict keys, use strings
    # representation of result instead until the problem is properly solved
    #
    # custom json encoder converts dict values and `convert_path_keys` is used to
    # convert dict keys
    result_dict = convert_path_keys(response.to_dict()["result"])
    result_str = json.dumps(result_dict, cls=CustomJSONEncoder)
    return {
        "status": status,
        "result": result_str,
        "format": response.format,
        "return_code": response.return_code,
    }


async def reload_action(ls: lsp_server.LanguageServer, action_name: str):
    logger.trace(f"Reload action: {action_name}")
    services.reload_action(action_name)
    return {}


async def resolve_package_path(ls: lsp_server.LanguageServer, package_name: str):
    logger.trace(f"Resolve package path: {package_name}")
    # TODO: handle properly ValueError
    result = services.resolve_package_path(package_name)
    logger.trace(f"Resolved {package_name} to {result}")
    return {"packagePath": result}
