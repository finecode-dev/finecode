# wrap all endpoint handlers in try/except because pygls only sends errors to client
# and don't log it locally
#
# keep at least until `lsp_server.ServerErrors` is used, because it is hidden under
# `TYPE_CHECKING` and its evaluation in runtime causes crash
from __future__ import annotations

import atexit
import json
import time

import pygls.exceptions as pygls_exceptions
from loguru import logger
from lsprotocol import types
from pygls.lsp import server as lsp_server

from finecode.extension_runner import domain, schemas, services
from finecode_extension_api import code_action


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

    async def document_requester(uri: str):
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

    async def document_saver(uri: str, content: str):
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

    def send_partial_result(
        token: int | str, partial_result: code_action.RunActionResult
    ) -> None:
        logger.debug(f"Send partial result for {token}")
        server.progress(
            types.ProgressParams(token=token, value=partial_result.model_dump_json())
        )

    services.document_requester = document_requester
    services.document_saver = document_saver
    services.set_partial_result_sender(send_partial_result)

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


async def update_config(ls: lsp_server.LanguageServer, params):
    logger.trace(f"Update config: {params}")
    try:
        working_dir = params[0]
        project_name = params[1]
        actions = params[2]

        request = schemas.UpdateConfigRequest(
            working_dir=working_dir,
            project_name=project_name,
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
        )
        response = await services.update_config(request=request)
        return response.to_dict()
    except Exception as e:
        logger.exception(f"Update config error: {e}")
        raise e


async def run_action(ls: lsp_server.LanguageServer, params):
    logger.trace(f"Run action: {params[0]}")
    request = schemas.RunActionRequest(action_name=params[0], params=params[1])
    options = schemas.RunActionOptions(**params[2] if params[2] is not None else {})
    # pygls sends uncatched exceptions(e.g. internal errors) to client. Log them as well
    try:
        response = await services.run_action(request=request, options=options)
    except Exception as e:
        logger.exception(f"Run action error: {e}")
        raise e
    # dict key can be path, but pygls fails to handle slashes in dict keys, use strings
    # representation of result instead until the problem is properly solved
    result_str = json.dumps(response.to_dict()["result"])
    return {
        "result": result_str,
        "format": response.format,
        "return_code": response.return_code,
    }


async def reload_action(ls: lsp_server.LanguageServer, params):
    logger.trace(f"Reload action: {params}")
    services.reload_action(params[0])
    return {}


async def resolve_package_path(ls: lsp_server.LanguageServer, params):
    logger.trace(f"Resolve package path: {params}")
    result = services.resolve_package_path(params[0])
    return {"packagePath": result}
