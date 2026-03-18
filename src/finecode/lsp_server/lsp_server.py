import asyncio
import collections.abc
from pathlib import Path

from loguru import logger
from lsprotocol import types
from pygls.workspace import position_codec
from pygls.lsp.server import LanguageServer
from finecode_extension_runner.lsp_server import CustomLanguageServer

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

    # Finecode commands exposed to the IDE
    register_list_actions_cmd = server.command("finecode.getActions")
    register_list_actions_cmd(action_tree_endpoints.list_actions)

    register_list_actions_for_position_cmd = server.command(
        "finecode.getActionsForPosition"
    )
    register_list_actions_for_position_cmd(
        action_tree_endpoints.list_actions_for_position
    )

    register_list_projects_cmd = server.command("finecode.listProjects")
    register_list_projects_cmd(action_tree_endpoints.list_projects)

    register_run_batch_cmd = server.command("finecode.runBatch")
    register_run_batch_cmd(action_tree_endpoints.run_batch)

    register_run_action_cmd = server.command("finecode.runAction")
    register_run_action_cmd(action_tree_endpoints.run_action)

    register_run_action_on_file_cmd = server.command("finecode.runActionOnFile")
    register_run_action_on_file_cmd(action_tree_endpoints.run_action_on_file)

    register_run_action_on_project_cmd = server.command("finecode.runActionOnProject")
    register_run_action_on_project_cmd(action_tree_endpoints.run_action_on_project)

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

    register_server_shutdown_feature = server.feature('server/shutdown')
    register_server_shutdown_feature(_lsp_server_shutdown)

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

    # Determine workspace root for WM server startup.
    workdir = Path.cwd()
    if ls.workspace.folders:
        first_folder = next(iter(ls.workspace.folders.values()))
        workdir = Path(first_folder.uri.replace("file://", ""))

    # Ensure the FineCode WM server is running and connect to it.
    # The TCP connection keeps the WM server alive for the LSP lifetime.
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
        ls.window_log_message(
            types.LogMessageParams(
                type=types.MessageType.Info,
                message=f"FineCode LSP Server log: {global_state.lsp_log_file_path}",
            )
        )

    log_path = global_state.wm_client.server_info.get("logFilePath")
    if log_path:
        ls.window_log_message(
            types.LogMessageParams(
                type=types.MessageType.Info,
                message=f"FineCode WM Server log: {log_path}",
            )
        )

    # Register notification handlers for server→client push messages.
    async def on_tree_changed(params: dict) -> None:
        # TODO
        ...
        # node = schemas.ActionTreeNode(**params["node"])
        # await action_tree_endpoints.notify_changed_action_node(ls, node)

    async def on_user_message(params: dict) -> None:
        await send_user_message_notification(ls, params["message"], params["type"])

    global_state.wm_client.on_notification("actions/treeChanged", on_tree_changed)
    global_state.wm_client.on_notification("server/userMessage", on_user_message)

    # forward progress notifications to the LSP progress reporter
    from finecode_extension_api.actions import lint as lint_action
    from pydantic.dataclasses import dataclass as pydantic_dataclass
    from finecode.lsp_server import pygls_types_utils
    from finecode.lsp_server.endpoints.diagnostics import map_lint_message_to_diagnostic

    def _map_lint_to_document_diagnostic_partial(lint_result: lint_action.LintRunResult) -> types.DocumentDiagnosticReportPartialResult:
        related_documents = {}
        for file_path_str, lint_messages in lint_result.messages.items():
            file_report = types.FullDocumentDiagnosticReport(
                items=[
                    map_lint_message_to_diagnostic(lint_message)
                    for lint_message in lint_messages
                ]
            )
            uri = pygls_types_utils.path_to_uri_str(Path(file_path_str))
            related_documents[uri] = file_report
        
        return types.DocumentDiagnosticReportPartialResult(related_documents=related_documents)

    def _map_lint_to_workspace_diagnostic_partial(lint_result: lint_action.LintRunResult) -> types.WorkspaceDiagnosticReportPartialResult:
        items = [
            types.WorkspaceFullDocumentDiagnosticReport(
                uri=pygls_types_utils.path_to_uri_str(Path(file_path_str)),
                items=[
                    map_lint_message_to_diagnostic(lint_message)
                    for lint_message in lint_messages
                ],
            )
            for file_path_str, lint_messages in lint_result.messages.items()
        ]
        return types.WorkspaceDiagnosticReportPartialResult(items=items)

    async def on_partial_result(params: dict) -> None:
        token = params.get("token")
        value = params.get("value")

        if token is None or value is None:
            logger.error("Invalid partial result notification: missing token or value")
            return

        # TODO: remove mapping either after last partial or after final result
        action, endpoint_type = global_state.partial_result_tokens.get(token, (None, None))
        if not action or not endpoint_type:
            logger.error(f"No mapping found for partial result token {token}")
            return

        if action == "lint":
            result_by_format = value.get("resultByFormat") or {}
            json_result = result_by_format.get("json")
            if json_result is None:
                logger.error(f"No json result in partial result for token {token}")
                return
            result_type = pydantic_dataclass(lint_action.LintRunResult)
            lint_result: lint_action.LintRunResult = result_type(**json_result)
            
            if endpoint_type == "document_diagnostic":
                lsp_partial = _map_lint_to_document_diagnostic_partial(lint_result)
            elif endpoint_type == "workspace_diagnostic":
                lsp_partial = _map_lint_to_workspace_diagnostic_partial(lint_result)
            else:
                logger.error(f"Unknown endpoint_type {endpoint_type} for action {action}")
                return
            
            ls.progress(types.ProgressParams(token=token, value=lsp_partial))
        else:
            logger.warning(f"Unsupported action for partial results: {action}")

    global_state.wm_client.on_notification("actions/partialResult", on_partial_result)

    # Add workspace directories via the WM server.
    try:
        async with asyncio.TaskGroup() as tg:
            for ws_dir in ls.workspace.folders.values():
                dir_path = Path(ws_dir.uri.replace("file://", ""))
                tg.create_task(global_state.wm_client.add_dir(dir_path))
    except ExceptionGroup as error:
        logger.exception(error)

    global_state.server_initialized.set()
    logger.trace("Workspace directories added, end of initialized handler")


async def _workspace_did_change_workspace_folders(
    ls: LanguageServer, params: types.DidChangeWorkspaceFoldersParams
):
    logger.trace(f"Workspace dirs were changed: {params}")
    if global_state.wm_client is None:
        logger.warning("WM client not connected, ignoring workspace folder change")
        return

    for ws_folder in params.event.removed:
        await global_state.wm_client.remove_dir(
            Path(ws_folder.uri.removeprefix("file://"))
        )

    for ws_folder in params.event.added:
        await global_state.wm_client.add_dir(
            Path(ws_folder.uri.removeprefix("file://"))
        )


def _on_shutdown(ls: LanguageServer, params):
    logger.info("on shutdown handler", params)
    # Close connection to the WM server. If this was the last client,
    # the WM server will auto-stop after a short delay and clean up runners.
    if global_state.wm_client is not None:
        asyncio.ensure_future(global_state.wm_client.close())
        global_state.wm_client = None


async def _lsp_server_shutdown(ls: LanguageServer, params):
    """Handle 'server/shutdown' — explicitly stop the WM server.

    Forwards the shutdown request to the WM server and then closes the
    WM client connection. Used by the IDE when it wants to restart the
    WM server (as opposed to a normal disconnect on deactivation).
    """
    logger.info("server/shutdown request received, stopping WM server")
    if global_state.wm_client is not None:
        try:
            await global_state.wm_client.request("server/shutdown", {})
        except Exception:
            logger.warning("WM server did not respond to shutdown request")
        await global_state.wm_client.close()
        global_state.wm_client = None
    return {}


async def reset(ls: LanguageServer, params):
    logger.info("Reset WM")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        logger.error("Reset requested but WM client not connected")
        return

    await global_state.wm_client.request("server/reset", {})


async def restart_extension_runner(ls: LanguageServer, tree_node, param2):
    logger.info(f"restart extension runner {tree_node}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        logger.error("Restart runner requested but WM client not connected")
        return

    runner_id = tree_node['projectPath']
    splitted_runner_id = runner_id.split('::')
    runner_working_dir_str = splitted_runner_id[0]
    env_name = splitted_runner_id[-1]

    await global_state.wm_client.request(
        "runners/restart",
        {"runnerWorkingDir": runner_working_dir_str, "envName": env_name},
    )


async def restart_and_debug_extension_runner(ls: LanguageServer, tree_node, params2):
    logger.info(f"restart and debug extension runner {tree_node} {params2}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        logger.error("Restart+debug runner requested but WM client not connected")
        return

    runner_id = tree_node['projectPath']
    splitted_runner_id = runner_id.split('::')
    runner_working_dir_str = splitted_runner_id[0]
    env_name = splitted_runner_id[-1]

    await global_state.wm_client.request(
        "runners/restart",
        {"runnerWorkingDir": runner_working_dir_str, "envName": env_name, "debug": True},
    )


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
