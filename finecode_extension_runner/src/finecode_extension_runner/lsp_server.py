# wrap all endpoint handlers in try/except because pygls only sends errors to client
# and don't log it locally
#
# keep at least until `lsp_server.ServerErrors` is used, because it is hidden under
# `TYPE_CHECKING` and its evaluation in runtime causes crash
from __future__ import annotations

import atexit
import dataclasses
import functools
import json
import pathlib
import time
import typing

import pygls.exceptions as pygls_exceptions
from loguru import logger
from lsprotocol import types
from pygls.lsp import server as lsp_server

from finecode_extension_api import code_action
from finecode_extension_runner import domain, schemas, services
from finecode_extension_runner._services import run_action as run_action_service


class CustomLanguageServer(lsp_server.LanguageServer):
    def report_server_error(self, error: Exception, source: lsp_server.ServerErrors):
        # return logging of error (`lsp_server.LanguageServer` overwrites it)
        super(lsp_server.LanguageServer, self).report_server_error(error, source)
        # send to client
        super().report_server_error(error, source)


def create_lsp_server() -> lsp_server.LanguageServer:
    server = CustomLanguageServer("FineCode_Extension_Runner_Server", "v1")

    register_initialized_feature = server.feature(types.INITIALIZED)
    register_initialized_feature(_on_initialized)

    register_shutdown_feature = server.feature(types.SHUTDOWN)
    register_shutdown_feature(_on_shutdown)

    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    register_document_did_open_feature(_document_did_open)

    register_document_did_close_feature = server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    register_document_did_close_feature(_document_did_close)

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
        # wait for graceful shutdown of all subprocesses if such exist
        time.sleep(2)
        services.exit_all_action_handlers()

    atexit.register(on_process_exit)

    def send_partial_result(
        token: int | str, partial_result: code_action.RunActionResult
    ) -> None:
        partial_result_dict = dataclasses.asdict(partial_result)
        partial_result_json = json.dumps(partial_result_dict)
        logger.debug(f"Send partial result for {token}, length {len(partial_result_json)}")
        server.progress(types.ProgressParams(token=token, value=partial_result_json))

    run_action_service.set_partial_result_sender(send_partial_result)

    return server


def _on_initialized(ls: lsp_server.LanguageServer, params: types.InitializedParams):
    logger.info(f"initialized {params}")


def _on_shutdown(ls: lsp_server.LanguageServer, params):
    logger.info("Shutdown extension runner")
    services.shutdown_all_action_handlers()


def _document_did_open(
    ls: lsp_server.LanguageServer, params: types.DidOpenTextDocumentParams
):
    logger.info(f"document did open: {params.text_document.uri}")
    services.document_did_open(params.text_document.uri)


def _document_did_close(
    ls: lsp_server.LanguageServer, params: types.DidCloseTextDocumentParams
):
    logger.info(f"document did close: {params.text_document.uri}")
    services.document_did_close(params.text_document.uri)


async def document_requester(server: lsp_server.LanguageServer, uri: str):
    try:
        document = await server.protocol.send_request_async(
            "documents/get", params={"uri": uri}
        )
    except pygls_exceptions.JsonRpcInternalError as error:
        if error.message == "Exception: Document is not opened":
            raise domain.TextDocumentNotOpened()
        else:
            raise error

    return domain.TextDocumentInfo(
        uri=document.uri, version=document.version, text=document.text
    )


async def document_saver(server: lsp_server.LanguageServer, uri: str, content: str):
    document = await server.protocol.send_request_async(
        "documents/get", params={"uri": uri}
    )
    document_lines = document.text.split("\n")
    params = types.ApplyWorkspaceEditParams(
        edit=types.WorkspaceEdit(
            # dict seems to be incorrectly unstructured on client(pygls issue?)
            # use document_changes instead of changes
            document_changes=[
                types.TextDocumentEdit(
                    text_document=types.OptionalVersionedTextDocumentIdentifier(
                        uri=uri
                    ),
                    edits=[
                        types.TextEdit(
                            range=types.Range(
                                start=types.Position(line=0, character=0),
                                end=types.Position(
                                    line=len(document_lines),
                                    character=len(document_lines[-1]),
                                ),
                            ),
                            new_text=content,
                        )
                    ],
                )
            ]
        )
    )
    await server.workspace_apply_edit_async(params)


async def get_project_raw_config(
    server: lsp_server.LanguageServer, project_def_path: str
) -> dict[str, typing.Any]:
    try:
        raw_config = await server.protocol.send_request_async(
            "projects/getRawConfig", params={"projectDefPath": project_def_path}
        )
    except pygls_exceptions.JsonRpcInternalError as error:
        raise error

    return json.loads(raw_config.config)


async def update_config(
    ls: lsp_server.LanguageServer,
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
            document_requester=functools.partial(document_requester, ls),
            document_saver=functools.partial(document_saver, ls),
            project_raw_config_getter=functools.partial(get_project_raw_config, ls),
        )
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
    options_schema = schemas.RunActionOptions(**options if options is not None else {})
    status: str = "success"

    try:
        response = await services.run_action(request=request, options=options_schema)
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
