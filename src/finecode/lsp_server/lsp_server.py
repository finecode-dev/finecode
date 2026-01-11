import asyncio
import collections.abc
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger
from lsprotocol import types
from pygls.workspace import position_codec
from pygls.lsp.server import LanguageServer
from finecode_extension_runner.lsp_server import CustomLanguageServer

from finecode.services import shutdown_service
from finecode.runner import runner_manager
from finecode.lsp_server import global_state, schemas, services
from finecode.lsp_server.endpoints import action_tree as action_tree_endpoints
from finecode.lsp_server.endpoints import code_actions as code_actions_endpoints
from finecode.lsp_server.endpoints import code_lens as code_lens_endpoints
from finecode.lsp_server.endpoints import diagnostics as diagnostics_endpoints
from finecode.lsp_server.endpoints import document_sync as document_sync_endpoints
from finecode.lsp_server.endpoints import formatting as formatting_endpoints
from finecode.lsp_server.endpoints import inlay_hints as inlay_hints_endpoints


def position_from_client_units(
    self, lines: collections.abc.Sequence[str], position: types.Position
) -> types.Position:
    return position


def create_lsp_server() -> CustomLanguageServer:
    # avoid recalculating of positions by pygls
    position_codec.PositionCodec.position_from_client_units = position_from_client_units
    
    
    # handle all requests explicitly because there are different types of requests:
    # project-specific, workspace-wide. Some Workspace-wide support partial responses,
    # some not.
    #
    # use CustomLanguageServer, because the problem with stopping the server with IO
    # communication(stopping waiting on input) is solved in it
    server = CustomLanguageServer("FineCode_Workspace_Manager_Server", "v1")

    register_initialized_feature = server.feature(types.INITIALIZED)
    register_initialized_feature(_on_initialized)

    register_workspace_dirs_feature = server.feature(
        types.WORKSPACE_DID_CHANGE_WORKSPACE_FOLDERS
    )
    register_workspace_dirs_feature(_workspace_did_change_workspace_folders)

    # Formatting
    register_formatting_feature = server.feature(types.TEXT_DOCUMENT_FORMATTING)
    register_formatting_feature(formatting_endpoints.format_document)

    register_range_formatting_feature = server.feature(
        types.TEXT_DOCUMENT_RANGE_FORMATTING
    )
    register_range_formatting_feature(formatting_endpoints.format_range)

    register_ranges_formatting_feature = server.feature(
        types.TEXT_DOCUMENT_RANGES_FORMATTING
    )
    register_ranges_formatting_feature(formatting_endpoints.format_ranges)

    # document sync
    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    register_document_did_open_feature(document_sync_endpoints.document_did_open)

    register_document_did_save_feature = server.feature(types.TEXT_DOCUMENT_DID_SAVE)
    register_document_did_save_feature(document_sync_endpoints.document_did_save)

    register_document_did_change_feature = server.feature(
        types.TEXT_DOCUMENT_DID_CHANGE
    )
    register_document_did_change_feature(document_sync_endpoints.document_did_change)

    register_document_did_close_feature = server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    register_document_did_close_feature(document_sync_endpoints.document_did_close)

    # code actions
    register_document_code_action_feature = server.feature(
        types.TEXT_DOCUMENT_CODE_ACTION
    )
    register_document_code_action_feature(code_actions_endpoints.document_code_action)

    register_code_action_resolve_feature = server.feature(types.CODE_ACTION_RESOLVE)
    register_code_action_resolve_feature(code_actions_endpoints.code_action_resolve)

    # code lens
    register_document_code_lens_feature = server.feature(types.TEXT_DOCUMENT_CODE_LENS)
    register_document_code_lens_feature(code_lens_endpoints.document_code_lens)

    register_code_lens_resolve_feature = server.feature(types.CODE_LENS_RESOLVE)
    register_code_lens_resolve_feature(code_lens_endpoints.code_lens_resolve)

    # diagnostics
    register_text_document_diagnostic_feature = server.feature(
        types.TEXT_DOCUMENT_DIAGNOSTIC
    )
    register_text_document_diagnostic_feature(diagnostics_endpoints.document_diagnostic)

    register_workspace_diagnostic_feature = server.feature(types.WORKSPACE_DIAGNOSTIC)
    register_workspace_diagnostic_feature(diagnostics_endpoints.workspace_diagnostic)

    # inline hints
    register_document_inlay_hint_feature = server.feature(
        types.TEXT_DOCUMENT_INLAY_HINT
    )
    register_document_inlay_hint_feature(inlay_hints_endpoints.document_inlay_hint)

    register_inlay_hint_feature = server.feature(types.INLAY_HINT_RESOLVE)
    register_inlay_hint_feature(inlay_hints_endpoints.inlay_hint_resolve)

    # Finecode
    register_list_actions_cmd = server.command("finecode.getActions")
    register_list_actions_cmd(action_tree_endpoints.list_actions)

    register_list_actions_for_position_cmd = server.command(
        "finecode.getActionsForPosition"
    )
    register_list_actions_for_position_cmd(
        action_tree_endpoints.list_actions_for_position
    )

    register_run_action_on_file_cmd = server.command("finecode.runActionOnFile")
    register_run_action_on_file_cmd(action_tree_endpoints.run_action_on_file)

    # register_run_action_on_project_cmd = server.command("finecode.runActionOnProject")
    # register_run_action_on_project_cmd(action_tree_endpoints.run_action_on_project)

    register_reload_action_cmd = server.command("finecode.reloadAction")
    register_reload_action_cmd(action_tree_endpoints.reload_action)

    register_reset_cmd = server.command("finecode.reset")
    register_reset_cmd(reset)

    register_restart_extension_runner_cmd = server.command(
        "finecode.restartExtensionRunner"
    )
    register_restart_extension_runner_cmd(restart_extension_runner)
    
    register_restart_and_debug_extension_runner_cmd = server.command(
        "finecode.restartAndDebugExtensionRunner"
    )
    register_restart_and_debug_extension_runner_cmd(restart_and_debug_extension_runner)

    register_shutdown_feature = server.feature(types.SHUTDOWN)
    register_shutdown_feature(_on_shutdown)

    return server


# LOG_LEVEL_MAP = {
#     "DEBUG": types.MessageType.Debug,
#     "INFO": types.MessageType.Info,
#     "SUCCESS": types.MessageType.Info,
#     "WARNING": types.MessageType.Warning,
#     "ERROR": types.MessageType.Error,
#     "CRITICAL": types.MessageType.Error,
# }


async def _on_initialized(ls: LanguageServer, params: types.InitializedParams):
    # def pass_log_to_ls_client(log) -> None:
    #     # disabling and enabling logging of pygls package is required to avoid logging
    #     # loop, because there are logs inside of log_trace and window_log_message
    #     # functions
    #     logger.disable("pygls")
    #     if log.record["level"].no < 10:
    #         # trace
    #         ls.log_trace(types.LogTraceParams(message=log.record["message"]))
    #     else:
    #         level = LOG_LEVEL_MAP.get(log.record["level"].name, types.MessageType.Info)
    #         ls.window_log_message(
    #             types.LogMessageParams(type=level, message=log.record["message"])
    #         )
    #     logger.enable("pygls")
    #     # module-specific config should be reapplied after disabling and enabling logger
    #     # for the whole package
    #     # TODO: unify with main
    #     logger.configure(activation=[("pygls.protocol.json_rpc", False)])

    # loguru doesn't support passing partial with ls parameter, use nested function
    # instead
    #
    # Disabled, because it is not thread-safe and it means not compatible with IO thread
    # logger.add(sink=pass_log_to_ls_client)

    logger.info("initialized, adding workspace directories")

    async def apply_workspace_edit(params):
        return await ls.workspace_apply_edit_async(params)

    services.register_workspace_edit_applier(apply_workspace_edit)

    services.register_project_changed_callback(
        partial(action_tree_endpoints.notify_changed_action_node, ls)
    )
    services.register_send_user_message_notification_callback(
        partial(send_user_message_notification, ls)
    )
    services.register_send_user_message_request_callback(
        partial(send_user_message_request, ls)
    )

    def report_progress(token: str | int, value: Any):
        ls.progress(types.ProgressParams(token, value))

    services.register_progress_reporter(report_progress)
    services.register_debug_session_starter(partial(start_debug_session, ls))

    try:
        async with asyncio.TaskGroup() as tg:
            for ws_dir in ls.workspace.folders.values():
                request = schemas.AddWorkspaceDirRequest(
                    dir_path=ws_dir.uri.replace("file://", "")
                )
                tg.create_task(services.add_workspace_dir(request=request))
    except ExceptionGroup as error:
        logger.exception(error)
        raise error

    global_state.server_initialized.set()
    logger.trace("Workspace directories added, end of initialized handler")


async def _workspace_did_change_workspace_folders(
    ls: LanguageServer, params: types.DidChangeWorkspaceFoldersParams
):
    logger.trace(f"Workspace dirs were changed: {params}")
    await services.handle_changed_ws_dirs(
        added=[
            Path(ws_folder.uri.removeprefix("file://"))
            for ws_folder in params.event.added
        ],
        removed=[
            Path(ws_folder.uri.removeprefix("file://"))
            for ws_folder in params.event.removed
        ],
    )


def _on_shutdown(ls: LanguageServer, params):
    logger.info("on shutdown handler", params)
    shutdown_service.on_shutdown(global_state.ws_context)


async def reset(ls: LanguageServer, params):
    logger.info("Reset WM")
    await global_state.server_initialized.wait()


async def restart_extension_runner(ls: LanguageServer, tree_node, param2):
    logger.info(f"restart extension runner {tree_node}")
    await global_state.server_initialized.wait()

    runner_id = tree_node['projectPath']
    splitted_runner_id = runner_id.split('::')
    runner_working_dir_str = splitted_runner_id[0]
    runner_working_dir_path = Path(runner_working_dir_str)
    env_name = splitted_runner_id[-1]

    await runner_manager.restart_extension_runner(runner_working_dir_path=runner_working_dir_path, env_name=env_name, ws_context=global_state.ws_context)


async def restart_and_debug_extension_runner(ls: LanguageServer, tree_node, params2):
    logger.info(f"restart and debug extension runner {tree_node} {params2}")
    await global_state.server_initialized.wait()

    runner_id = tree_node['projectPath']
    splitted_runner_id = runner_id.split('::')
    runner_working_dir_str = splitted_runner_id[0]
    runner_working_dir_path = Path(runner_working_dir_str)
    env_name = splitted_runner_id[-1]

    logger.info(f'start debugging {runner_working_dir_path} {runner_id} {env_name}')
    await runner_manager.restart_extension_runner(runner_working_dir_path=runner_working_dir_path, env_name=env_name, ws_context=global_state.ws_context, debug=True)


async def send_user_message_notification(
    ls: LanguageServer, message: str, message_type: str
) -> None:
    message_type_pascal = message_type[0] + message_type[1:].lower()
    ls.window_show_message(
        types.ShowMessageParams(
            type=types.MessageType[message_type_pascal], message=message
        )
    )


async def send_user_message_request(
    ls: LanguageServer, message: str, message_type: str
) -> None:
    message_type_pascal = message_type[0] + message_type[1:].lower()
    await ls.window_show_message_request_async(
        types.ShowMessageRequestParams(
            type=types.MessageType[message_type_pascal], message=message
        )
    )


async def start_debug_session(
    ls: LanguageServer, params
) -> None:
    res = await ls.protocol.send_request_async('ide/startDebugging', params)
    logger.info(f"started debugging: {res}")


__all__ = ["create_lsp_server"]
