import asyncio
import typing
from pygls.lsp.server import LanguageServer
from lsprotocol import types
from loguru import logger

import finecode.workspace_manager.server.schemas as schemas
import finecode.workspace_manager.server.services as services
import finecode.workspace_manager.server.global_state as global_state


def create_lsp_server() -> LanguageServer:
    server = LanguageServer("FineCode_Workspace_Manager_Server", "v1")
    
    register_initialized_feature = server.feature(types.INITIALIZED)
    register_initialized_feature(_on_initialized)
    
    # Formatting
    register_formatting_feature = server.feature(types.TEXT_DOCUMENT_FORMATTING)
    register_formatting_feature(_format_document)
    
    register_range_formatting_feature = server.feature(types.TEXT_DOCUMENT_RANGE_FORMATTING)
    register_range_formatting_feature(_format_range)
    
    register_ranges_formatting_feature = server.feature(types.TEXT_DOCUMENT_RANGES_FORMATTING)
    register_ranges_formatting_feature(_format_ranges)
    
    # linting
    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    register_document_did_open_feature(_document_did_open)
    
    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_SAVE)
    register_document_did_open_feature(_document_did_save)
    
    # Finecode
    register_list_actions_feature = server.feature('finecode/getActions')
    register_list_actions_feature(list_actions)
    
    register_run_action_on_file_cmd = server.command('finecode.runActionOnFile')
    register_run_action_on_file_cmd(run_action_on_file)

    register_run_action_on_project_cmd = server.command('finecode.runActionOnProject')
    register_run_action_on_project_cmd(run_action_on_project)

    return server


async def _on_initialized(ls: LanguageServer, params: types.InitializedParams):
    logger.info(f"initialized {params}")

    add_ws_dir_coros: list[typing.Coroutine] = []
    for ws_dir in ls.workspace.folders.values():
        request = schemas.AddWorkspaceDirRequest(dir_path=ws_dir.uri.replace('file://', ''))
        add_ws_dir_coros.append(services.add_workspace_dir(request=request))

    await asyncio.gather(*add_ws_dir_coros)
    global_state.server_initialized.set()


async def _format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
    """Format the entire document"""
    logger.info(f"format document {params}")
    await global_state.server_initialized.wait()

    doc = ls.workspace.get_text_document(params.text_document.uri)
    action_request = schemas.RunActionRequest(action_node_id='format', apply_on=doc.path, apply_on_text=doc.source)
    response = await services.run_action(action_request)

    if response.result.changed is True:
        return [types.TextEdit(range=types.Range(start=types.Position(0, 0), end=types.Position(len(doc.lines), len(doc.lines[-1]))), new_text=response.result.code)]
    return None


async def _format_range(ls: LanguageServer, params: types.DocumentRangeFormattingParams):
    logger.info(f"format range {params}")
    await global_state.server_initialized.wait()

    return []


async def _format_ranges(ls: LanguageServer, params: types.DocumentRangesFormattingParams):
    logger.info(f"format ranges {params}")
    await global_state.server_initialized.wait()
    
    return []


async def _document_did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
    logger.trace(f"Document did open: {params}")
    await _lint_and_publish_results(ls, params.text_document.uri.replace('file://', ''))


async def _lint_and_publish_results(ls: LanguageServer, path_to_lint: str):
    await global_state.server_initialized.wait()

    # TODO: check whether 'lint' action is available and enabled
    run_action_request = schemas.RunActionRequest(action_node_id='lint', apply_on=path_to_lint, apply_on_text='')
    try:
        response = await services.run_action(run_action_request)
        logger.debug(f'response: {response}')
        # assert isinstance(response, )
        # ls.text_document_publish_diagnostics(types.PublishDiagnosticsParams())
        # TODO: publish 
    except services.ActionNotFound:
        logger.info("No lint action")


async def _document_did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
    logger.trace(f"Document did save: {params}")


# async def _document_did_change(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
#     ...


async def list_actions(ls: LanguageServer, params):
    logger.info(f"list_actions {params}")
    await global_state.server_initialized.wait()
    
    logger.info(f'{params} {type(params)} {len(params)} {params[0]}')
    request = schemas.ListActionsRequest(parent_node_id='') # params.get('parentNodeId', '')
    result = await services.list_actions(request=request)
    return result.to_dict()


async def run_action_on_file(ls: LanguageServer, params):
    logger.info(f"run action on file {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    action_node_id = params_dict['projectPath']

    document_meta = await ls.protocol.send_request_async(method='editor/documentMeta', params={}, msg_id=None)
    if document_meta is None:
        return None

    document_text = await ls.protocol.send_request_async(method='editor/documentText', params={}, msg_id=None)
    if document_text is None:
        return None

    run_action_request = schemas.RunActionRequest(action_node_id=action_node_id, apply_on=document_meta.uri.path, apply_on_text=document_text.text)
    response = await services.run_action(run_action_request)
    logger.debug(f'Response: {response}')

    if action_node_id.endswith(':format') and response.result.changed is True:
        doc = ls.workspace.get_text_document(document_meta.uri.external)
        await ls.workspace_apply_edit_async(types.ApplyWorkspaceEditParams(edit=types.WorkspaceEdit(changes={document_meta.uri.external: [types.TextEdit(range=types.Range(start=types.Position(0, 0), end=types.Position(len(doc.lines), len(doc.lines[-1]))), new_text=response.result.code)]})))

    return response.to_dict()


async def run_action_on_project(ls: LanguageServer, params):
    logger.info(f"run action on project {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    action_node_id = params_dict['projectPath']
    apply_on = action_node_id.split('::')[0]
    run_action_request = schemas.RunActionRequest(action_node_id=action_node_id, apply_on=apply_on, apply_on_text='')
    response = await services.run_action(run_action_request)
    return response.to_dict()
