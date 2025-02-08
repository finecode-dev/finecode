import asyncio
import typing
from functools import partial
from pathlib import Path

from loguru import logger
from lsprotocol import types
from pygls.lsp.server import LanguageServer

import finecode.workspace_manager.server.endpoints.code_actions as code_actions_endpoints
import finecode.workspace_manager.server.endpoints.code_lens as code_lens_endpoints
import finecode.workspace_manager.server.endpoints.diagnostics as diagnostics_endpoints
import finecode.workspace_manager.server.endpoints.formatting as formatting_endpoints
import finecode.workspace_manager.server.endpoints.inlay_hints as inlay_hints_endpoints
from finecode.workspace_manager.server import global_state, schemas, services


def create_lsp_server() -> LanguageServer:
    # handle all requests explicitly because there are different types of requests: project-specific,
    # workspace-wide. Some Workspace-wide support partial responses, some not.
    server = LanguageServer("FineCode_Workspace_Manager_Server", "v1")

    register_initialized_feature = server.feature(types.INITIALIZED)
    register_initialized_feature(_on_initialized)

    register_workspace_dirs_feature = server.feature(types.WORKSPACE_DID_CHANGE_WORKSPACE_FOLDERS)
    register_workspace_dirs_feature(_workspace_did_change_workspace_folders)

    # Formatting
    register_formatting_feature = server.feature(types.TEXT_DOCUMENT_FORMATTING)
    register_formatting_feature(formatting_endpoints.format_document)

    register_range_formatting_feature = server.feature(types.TEXT_DOCUMENT_RANGE_FORMATTING)
    register_range_formatting_feature(formatting_endpoints.format_range)

    register_ranges_formatting_feature = server.feature(types.TEXT_DOCUMENT_RANGES_FORMATTING)
    register_ranges_formatting_feature(formatting_endpoints.format_ranges)

    # linting
    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    register_document_did_open_feature(_document_did_open)

    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_SAVE)
    register_document_did_open_feature(_document_did_save)

    # code actions
    register_document_code_action_feature = server.feature(types.TEXT_DOCUMENT_CODE_ACTION)
    register_document_code_action_feature(code_actions_endpoints.document_code_action)

    register_code_action_resolve_feature = server.feature(types.CODE_ACTION_RESOLVE)
    register_code_action_resolve_feature(code_actions_endpoints.code_action_resolve)

    # code lens
    register_document_code_lens_feature = server.feature(types.TEXT_DOCUMENT_CODE_LENS)
    register_document_code_lens_feature(code_lens_endpoints.document_code_lens)

    register_code_lens_resolve_feature = server.feature(types.CODE_LENS_RESOLVE)
    register_code_lens_resolve_feature(code_lens_endpoints.code_lens_resolve)

    # diagnostics
    register_text_document_diagnostic_feature = server.feature(types.TEXT_DOCUMENT_DIAGNOSTIC)
    register_text_document_diagnostic_feature(diagnostics_endpoints.document_diagnostic)

    register_workspace_diagnostic_feature = server.feature(types.WORKSPACE_DIAGNOSTIC)
    register_workspace_diagnostic_feature(diagnostics_endpoints.workspace_diagnostic)

    # inline hints
    register_document_inlay_hint_feature = server.feature(types.TEXT_DOCUMENT_INLAY_HINT)
    register_document_inlay_hint_feature(inlay_hints_endpoints.document_inlay_hint)

    register_inlay_hint_feature = server.feature(types.INLAY_HINT_RESOLVE)
    register_inlay_hint_feature(inlay_hints_endpoints.inlay_hint_resolve)

    # Finecode
    register_list_actions_cmd = server.command("finecode.getActions")
    register_list_actions_cmd(list_actions)

    register_list_actions_for_position_cmd = server.command("finecode.getActionsForPosition")
    register_list_actions_for_position_cmd(list_actions_for_position)

    register_run_action_on_file_cmd = server.command("finecode.runActionOnFile")
    register_run_action_on_file_cmd(run_action_on_file)

    register_run_action_on_project_cmd = server.command("finecode.runActionOnProject")
    register_run_action_on_project_cmd(run_action_on_project)

    register_reload_action_cmd = server.command("finecode.reloadAction")
    register_reload_action_cmd(reload_action)

    register_reset_cmd = server.command("finecode.reset")
    register_reset_cmd(reset)

    register_restart_extension_runner_cmd = server.command("finecode.restartExtensionRunner")
    register_restart_extension_runner_cmd(restart_extension_runner)

    register_shutdown_feature = server.feature(types.SHUTDOWN)
    register_shutdown_feature(_on_shutdown)

    return server


LOG_LEVEL_MAP = {
    "DEBUG": types.MessageType.Debug,
    "INFO": types.MessageType.Info,
    "SUCCESS": types.MessageType.Info,
    "WARNING": types.MessageType.Warning,
    "ERROR": types.MessageType.Error,
    "CRITICAL": types.MessageType.Error,
}


async def _on_initialized(ls: LanguageServer, params: types.InitializedParams):
    def pass_log_to_ls_client(log) -> None:
        # disabling and enabling logging of pygls package is required to avoid logging loop,
        # because there are logs inside of log_trace and window_log_message functions
        logger.disable("pygls")
        if log.record["level"].no < 10:
            # trace
            ls.log_trace(types.LogTraceParams(message=log.record["message"]))
        else:
            level = LOG_LEVEL_MAP.get(log.record["level"].name, types.MessageType.Info)
            ls.window_log_message(
                types.LogMessageParams(type=level, message=log.record["message"])
            )
        logger.enable("pygls")

    # loguru doesn't support passing partial with ls parameter, use nested function instead
    logger.add(sink=pass_log_to_ls_client)

    logger.info(f"initialized, adding workspace directories")

    services.register_project_changed_callback(partial(notify_changed_action_node, ls))
    services.register_send_user_message_notification_callback(
        partial(send_user_message_notification, ls)
    )
    services.register_send_user_message_request_callback(partial(send_user_message_request, ls))

    try:
        async with asyncio.TaskGroup() as tg:
            for ws_dir in ls.workspace.folders.values():
                request = schemas.AddWorkspaceDirRequest(dir_path=ws_dir.uri.replace("file://", ""))
                tg.create_task(services.add_workspace_dir(request=request))
    except Exception as error:
        logger.exception(error)
        raise error

    global_state.server_initialized.set()
    logger.trace("Workspace directories added, end of initialized handler")


async def _workspace_did_change_workspace_folders(
    ls: LanguageServer, params: types.DidChangeWorkspaceFoldersParams
):
    logger.trace(f"Workspace dirs were changed: {params}")
    await services.handle_changed_ws_dirs(
        added=[Path(ws_folder.uri.lstrip("file://")) for ws_folder in params.event.added],
        removed=[Path(ws_folder.uri.lstrip("file://")) for ws_folder in params.event.removed],
    )


async def _document_did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
    logger.trace(f"Document did open: {params.text_document.uri}")


#     await _lint_and_publish_results(ls, params.text_document.uri.replace("file://", ""))


async def _document_did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
    logger.trace(f"Document did save: {params}")
    # await _lint_and_publish_results(ls, params.text_document.uri.replace("file://", ""))


# async def _document_did_change(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
#     ...


async def _on_shutdown(ls: LanguageServer, params):
    logger.info("on shutdown handler", params)
    await services.on_shutdown()


async def list_actions(ls: LanguageServer, params):
    logger.info(f"list_actions {params}")
    await global_state.server_initialized.wait()

    parent_node_id = params[0]
    request = schemas.ListActionsRequest(parent_node_id=parent_node_id)
    result = await services.list_actions(request=request)
    return result.to_dict()


async def list_actions_for_position(ls: LanguageServer, params):
    logger.info(f"list_actions for position {params}")
    await global_state.server_initialized.wait()

    position = params[0]
    # TODO
    request = schemas.ListActionsRequest(parent_node_id="")
    result = await services.list_actions(request=request)
    return result.to_dict()


async def run_action_on_file(ls: LanguageServer, params):
    logger.info(f"run action on file {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]

    document_meta = await ls.protocol.send_request_async(
        method="editor/documentMeta", params={}, msg_id=None
    )
    if document_meta is None:
        return None

    document_text = await ls.protocol.send_request_async(
        method="editor/documentText", params={}, msg_id=None
    )
    if document_text is None:
        return None

    run_action_request = schemas.RunActionRequest(
        action_node_id=action_node_id,
        apply_on=document_meta.uri.path,
        apply_on_text=document_text.text,
    )
    response = await services.run_action(run_action_request)
    logger.debug(f"Response: {response}")

    if action_node_id.endswith(":format") and response.result.get("changed", False) is True:
        doc = ls.workspace.get_text_document(document_meta.uri.external)
        await ls.workspace_apply_edit_async(
            types.ApplyWorkspaceEditParams(
                edit=types.WorkspaceEdit(
                    changes={
                        document_meta.uri.external: [
                            types.TextEdit(
                                range=types.Range(
                                    start=types.Position(0, 0),
                                    end=types.Position(len(doc.lines), len(doc.lines[-1])),
                                ),
                                new_text=response.result["code"],
                            )
                        ]
                    }
                )
            )
        )

    return response.to_dict()


async def run_action_on_project(ls: LanguageServer, params):
    logger.info(f"run action on project {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]
    apply_on = action_node_id.split("::")[0]
    run_action_request = schemas.RunActionRequest(
        action_node_id=action_node_id, apply_on=apply_on, apply_on_text=""
    )
    response = await services.run_action(run_action_request)
    return response.to_dict()


async def reload_action(ls: LanguageServer, params):
    logger.info(f"reload action {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]
    await services.reload_action(action_node_id)

    return {}


async def reset(ls: LanguageServer, params):
    logger.info("Reset WM")
    await global_state.server_initialized.wait()
    ...


async def restart_extension_runner(ls: LanguageServer, params):
    logger.info(f"restart extension runner {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    runner_working_dir_str = params_dict["projectPath"]
    runner_working_dir_path = Path(runner_working_dir_str)

    await services.restart_extension_runner(runner_working_dir_path)


async def notify_changed_action_node(ls: LanguageServer, action: schemas.ActionTreeNode) -> None:
    # lsp client requests have no timeout, add own one
    # try:
    ls.protocol.notify(method="actionsNodes/changed", params=action.to_dict())
    # except TimeoutError:
    #     logger.error(f"Failed to notify about changed action node")
    #     raise Exception()  # TODO


def send_user_message_notification(ls: LanguageServer, message: str, message_type: str) -> None:
    ls.window_show_message(types.ShowMessageParams(type=message_type, message=message))


async def send_user_message_request(ls: LanguageServer, message: str, message_type: str) -> None:
    await ls.window_show_message_request_async(
        types.ShowMessageRequestParams(type=message_type, message=message)
    )


__all__ = ["create_lsp_server"]
